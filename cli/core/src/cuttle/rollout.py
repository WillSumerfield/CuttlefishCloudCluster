"""The Head pushes its `cuttle` build to every Arm.

For each admitted Arm whose build differs from the Head's (`dist.py`):

1. a Slurm maintenance reservation on it, from now: running Dives carry on, no new ones start;
2. a root job inside that reservation, which Slurm starts once the Arm is free. It fetches the wheel
   from the Enroll API, installs it as the Arm's owner, and runs `cuttle update` as root against their
   ~/.ccc (`config.ARM_UPDATER_RECORD`). The update's last step reports the new build to the Head;
3. once it has, the reservation goes and the Arm takes Dives again.

A failed job is retried, up to `config.ROLLOUT_ATTEMPTS` jobs in all. Then the Arm stays reserved, its
status says why, and the Admin gets an email (the monitor's SMTP settings).

The logic is one standalone script (`_SCRIPT`, root, no cuttle import), run at the end of the Head's
`cuttle update`, by `cuttle arm update`, and by a timer every few minutes — which is what notices a
job finished, retries it, and reopens the Arm.
"""

import json

from . import config
from .system import run

_SCRIPT = r'''"""Push the Head's cuttle build to every out-of-date Arm. Installed by cuttle; see cuttle/rollout.py.
Usage: ccc-arm-rollout.py <arms.json> <build.json> <state.json> <monitor.yaml> <enroll url> [arm ...]
Arms named after the url start over (attempts reset)."""
import json, os, smtplib, subprocess, sys, tempfile
from email.message import EmailMessage
from pathlib import Path

PREFIX = "@PREFIX@"
ATTEMPTS = @ATTEMPTS@
UPDATER = "@UPDATER@"

# The job, run on the Arm as root. Python, so it can talk JSON to the Enroll API without extra tools.
JOB = """#!/usr/bin/python3
import json, os, pwd, shutil, subprocess, sys, tempfile, urllib.request
HEAD, NAME, TARGET = %r, %r, %r
PIN = "@PUBKEY@"   # the Head's rollout-signing pubkey, pinned on this Arm at authorize (F3)

def fail(msg):
    body = json.dumps({"build": TARGET, "error": msg}).encode()
    req = urllib.request.Request(HEAD + "/arms/" + NAME + "/update-failed", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15)
    finally:
        print(msg, file=sys.stderr)
        sys.exit(1)

meta = json.load(urllib.request.urlopen(HEAD + "/cuttle", timeout=15))
try:
    rec = json.load(open(%r))
except OSError:
    w = "/tmp/" + meta["wheel"]
    fail("this Arm was set up before updates from the Head, so the Head doesn't know where its "
         "cuttle lives. Its owner must install the new cuttle and update once, on the Arm:\\n"
         "  curl -fo " + w + " " + HEAD + "/cuttle/wheel && uv tool install --force " + w +
         " && cuttle update")
pw = pwd.getpwnam(rec["user"])
# In the owner's home, not /tmp: Slurm gives this job a private /tmp only root can enter.
d = tempfile.mkdtemp(prefix=".ccc-update-", dir=rec["home"])
wheel = os.path.join(d, meta["wheel"])
with urllib.request.urlopen(HEAD + "/cuttle/wheel", timeout=300) as r, open(wheel, "wb") as f:
    f.write(r.read())
# F3: verify the wheel's ed25519 signature against the pinned Head pubkey BEFORE anything runs it. The
# signature is channel-independent, so a Head impersonator (or tampered mesh route) cannot forge an
# install even over the same plain-HTTP enroll channel. Missing pin or bad signature: refuse, don't run.
sig = wheel + ".sig"
with urllib.request.urlopen(HEAD + "/cuttle/wheel.sig", timeout=60) as r, open(sig, "wb") as f:
    f.write(r.read())
if not os.path.exists(PIN):
    shutil.rmtree(d, ignore_errors=True)
    fail("this Arm has no pinned rollout-signing key, so it cannot verify the Head's cuttle. Its owner "
         "must re-authorize it -- on the Head run\\n  cuttle arm authorize " + NAME +
         " --user <owner>\\nthen re-run `cuttle setup --arm --token <token>` on the Arm.")
v = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", PIN, "-rawin", "-in", wheel,
                    "-sigfile", sig], capture_output=True, text=True)
if v.returncode:
    shutil.rmtree(d, ignore_errors=True)
    fail("the Head's cuttle wheel FAILED signature verification -- refusing to install it. This is "
         "either a corrupt download or an impostor Head. (" + (v.stdout + v.stderr).strip()[-500:] + ")")
os.chown(d, pw.pw_uid, pw.pw_gid)
os.chown(wheel, pw.pw_uid, pw.pw_gid)
env = {"HOME": rec["home"], "PATH": rec["path"], "USER": rec["user"], "LOGNAME": rec["user"]}
r = subprocess.run(["runuser", "-u", rec["user"], "--", rec["uv"], "tool", "install", "--force", wheel],
                   env=env, capture_output=True, text=True)
shutil.rmtree(d, ignore_errors=True)
if r.returncode:
    fail("installing the new cuttle failed:\\n" + (r.stdout + r.stderr)[-3000:])
# Outside this job's cgroup and namespace: the update restarts slurmd, and must outlive this job.
r = subprocess.run(["systemd-run", "--wait", "--pipe", "--collect", "--quiet",
                    "--setenv=HOME=" + rec["home"], "--setenv=CCC_HOME=" + rec["ccc_home"],
                    "--setenv=PATH=" + rec["path"], rec["cuttle"], "--no-input", "update"],
                   stdin=subprocess.DEVNULL, capture_output=True, text=True)
subprocess.run(["chown", "-R", "%%d:%%d" %% (pw.pw_uid, pw.pw_gid), rec["ccc_home"]])
if r.returncode:
    fail("cuttle update failed:\\n" + (r.stdout + r.stderr)[-3000:])
"""


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return default


def save(path, data):
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.chmod(0o644)
    tmp.replace(path)


def email(conf_path, subject, body):
    conf = {}
    try:
        for line in Path(conf_path).read_text().splitlines():
            if ":" in line and not line.strip().startswith("#"):
                k, _, v = line.partition(":")
                conf[k.strip()] = v.strip().strip("'\"")
    except FileNotFoundError:
        pass
    if not conf.get("smtp_host"):
        print(f"(no SMTP set up) {subject}\n{body}", file=sys.stderr)
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, conf["from_addr"], conf["to_addr"]
    msg.set_content(body)
    try:
        with smtplib.SMTP(conf["smtp_host"], int(conf.get("smtp_port", 587))) as s:
            s.starttls()
            s.login(conf["smtp_user"], conf["smtp_password"])
            s.send_message(msg)
    except Exception as e:
        print(f"could not email: {e}", file=sys.stderr)


def job_state(name):
    """PENDING / RUNNING / ... while Slurm knows the Arm's update job, else None."""
    r = sh("squeue", "-h", "-u", "root", "-n", PREFIX + name, "-o", "%T")
    states = r.stdout.split()
    return states[0] if states else None


def reserve(name):
    sh("scontrol", "create", "reservation", f"ReservationName={PREFIX}{name}", "StartTime=now",
       "Duration=infinite", f"Nodes={name}", "Users=root", "Flags=maint,ignore_jobs")


def release(name):
    sh("scontrol", "delete", f"ReservationName={PREFIX}{name}")


def submit(name, head, target):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(JOB % (head, name, target, UPDATER))
    r = sh("sbatch", "--parsable", f"--reservation={PREFIX}{name}", "-w", name, "--exclusive",
           "-J", PREFIX + name, "-o", "/var/log/ccc-arm-update.log", f.name)
    os.unlink(f.name)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def main(reg_path, meta_path, state_path, conf_path, head, *restart):
    meta = load(meta_path, None)
    if not meta:
        return
    target = meta["build"]
    reg, state = load(reg_path, {}), load(state_path, {})
    for name in restart:
        state.pop(name, None)
    for name, e in sorted(reg.items()):
        if e.get("head") or not e.get("admitted"):
            continue
        s = state.get(name)
        if e.get("build") == target:
            if s:
                release(name)
                state.pop(name)
            continue
        if not s or s.get("target") != target:
            s = state[name] = {"target": target, "attempts": 0, "status": "waiting", "error": None}
            reserve(name)
        live = job_state(name)
        if live:
            s["status"] = "updating" if live == "RUNNING" else "waiting"
            continue
        if s["attempts"] >= ATTEMPTS:
            if s["status"] != "failed":
                err = (e.get("update_error") or {})
                s["status"] = "failed"
                s["error"] = (err.get("error") if err.get("build") == target else None) or \
                    "the update job ended without reporting (see /var/log/ccc-arm-update.log on the Arm)"
                email(conf_path, f"CCC: updating Arm {name} failed",
                      f"{name} could not update to cuttle build {target} after {ATTEMPTS} tries. "
                      f"It takes no new Dives until it does.\n\n{s['error']}\n\n"
                      f"Retry on the Head with: cuttle arm update {name}")
            continue
        ok, out = submit(name, head, target)
        s["attempts"] += 1
        s["status"] = "waiting" if ok else "failed"
        if not ok:
            s["error"] = "sbatch refused the update job: " + out
    for name in [n for n in state if n not in reg or reg[n].get("head")]:
        release(name)
        state.pop(name)
    save(state_path, state)
    for name, s in sorted(state.items()):
        print(f"{name}\t{s['status']}\t{s.get('error') or ''}".rstrip())


if __name__ == "__main__":
    main(*sys.argv[1:])
'''.replace("@PREFIX@", config.ROLLOUT_PREFIX).replace(
    "@ATTEMPTS@", str(config.ROLLOUT_ATTEMPTS)).replace(
    "@UPDATER@", str(config.ARM_UPDATER_RECORD)).replace(
    "@PUBKEY@", str(config.ROLLOUT_PUBKEY_ARM))

_TIMER_UNIT = f"""\
[Unit]
Description=CCC: push the Head's cuttle build to Arms

[Timer]
OnBootSec=5min
OnUnitActiveSec={config.MONITOR_INTERVAL_MIN}min

[Install]
WantedBy=timers.target
"""


def _argv(mesh_ip: str) -> list[str]:
    from . import headapi
    return ["/usr/bin/python3", str(config.ROLLOUT_SCRIPT), str(config.ARMS_REGISTRY),
            str(config.CUTTLE_DIST_META), str(config.ROLLOUT_STATE), str(config.MONITOR_CONF),
            headapi.enroll_url(mesh_ip)]


def install(write_unit, mesh_ip: str) -> None:
    write_unit(config.ROLLOUT_SCRIPT, _SCRIPT, "755")
    write_unit(config.ROLLOUT_SERVICE_UNIT, f"""\
[Unit]
Description=CCC: push the Head's cuttle build to Arms
After=slurmctld.service

[Service]
Type=oneshot
ExecStart={' '.join(_argv(mesh_ip))}
""", "644")
    write_unit(config.ROLLOUT_TIMER_UNIT, _TIMER_UNIT, "644")
    run(["systemctl", "daemon-reload"], sudo=True)
    run(["systemctl", "enable", "--now", f"{config.ROLLOUT_SERVICE}.timer"], sudo=True)


def push(mesh_ip: str, restart: list[str] | None = None) -> list[tuple[str, str, str]]:
    """One pass now. Returns (arm, status, error) for every Arm still being updated."""
    r = run([*_argv(mesh_ip), *(restart or [])], sudo=True, capture_output=True, text=True)
    rows = []
    for line in r.stdout.splitlines():
        name, status, *err = line.split("\t")
        rows.append((name, status, err[0] if err else ""))
    return rows


def status() -> dict:
    try:
        return json.loads(config.ROLLOUT_STATE.read_text())
    except FileNotFoundError:
        return {}

