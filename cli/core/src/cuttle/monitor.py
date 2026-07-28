"""`cuttle`'s Head-box health monitor (Phase 8 loose end: see docs/TODO.md).

Disk space, process-liveness, and rogue GPU/CPU usage — compute happening outside Slurm, the
telemetry/CONTEXT.md "rogue-actor" concept, applied here to the Head's own co-located Arm rather than
a donor's. Deliberately Head-only: an Arm is a machine we don't own, so watching one the way we watch
our own box is a separate, deliberate call for whenever Phase 2 gets to Arm telemetry — building this
generic now would decide that call by accident.

The three checks live in one standalone script (`_SCRIPT`, installed at `config.MONITOR_SCRIPT`) run
by a systemd timer every `config.MONITOR_INTERVAL_MIN` minutes — one implementation of "is the Head
healthy," shared by nothing else because nothing else needs it. It takes no cuttle import, so the
timer's oneshot service runs it with a bare system python3, as the `ccc` account (the same Mantle
that already runs the Head API/Karma tick): nothing here needs root — disk usage, `systemctl
is-active`, `nvidia-smi`, and `squeue` are all readable unprivileged.

Each check emails independently and re-alerts at most once every `config.MONITOR_COOLDOWN_S`, tracked
per check in `config.MONITOR_STATE` — a stuck disk alert can't suppress a later rogue-usage one. SMTP
credentials live in `config.MONITOR_CONF` (ccc:ccc, 0600), filled in by `prompt_smtp()` during `cuttle
setup --head` — skippable; the checks still run and print to stderr (visible in `journalctl -u
ccc-monitor`), they just can't email until it's filled in.
"""

import json

from . import config, ui
from .system import run

# Standalone (no cuttle import — see module docstring). Runs as ccc, on a timer. Configuration comes
# in over argv (checks.json, monitor.yaml, state.json) rather than being baked in at install time, so
# there is nothing here for `cuttle` itself to template/escape.
_SCRIPT = r'''"""Head-box health checks: disk, process-liveness, rogue GPU/CPU usage outside Slurm.
Installed by cuttle at this path; not meant to be run by hand. See cuttle/monitor.py for why this
exists standalone. Usage: ccc-monitor.py <checks.json> <monitor.yaml> <state.json>"""
import json, os, smtplib, subprocess, sys, time
from email.message import EmailMessage
from pathlib import Path


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return default


def _load_yaml_flat(path):
    """Just enough YAML for `key: value` lines — avoids a PyYAML dependency in this standalone
    script, which otherwise needs nothing outside the stdlib."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return None
    conf = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        conf[key.strip()] = value.strip().strip("'\"")
    return conf or None


def _send_email(conf, subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = conf["from_addr"]
    msg["To"] = conf["to_addr"]
    msg.set_content(body)
    with smtplib.SMTP(conf["smtp_host"], int(conf.get("smtp_port", 587))) as s:
        s.starttls()
        s.login(conf["smtp_user"], conf["smtp_password"])
        s.send_message(msg)


def alert(state, conf_path, key, subject, body):
    """At most one email per `key` every state["_cooldown_s"]."""
    now = time.time()
    if now - state.get(key, 0) < state.get("_cooldown_s", 86400):
        return
    conf = _load_yaml_flat(conf_path)
    if conf:
        try:
            _send_email(conf, subject, body)
        except (OSError, smtplib.SMTPException) as e:
            print(f"ccc-monitor: {key} fired but email failed: {e}", file=sys.stderr)
    else:
        print(f"ccc-monitor: {key} fired, no SMTP conf at {conf_path}", file=sys.stderr)
    state[key] = now


def check_disk(state, conf_path, checks):
    du = os.statvfs(checks["disk_path"])
    pct = (1 - du.f_bavail / du.f_blocks) * 100
    if pct >= checks["disk_pct"]:
        alert(state, conf_path, "disk", "CCC Head: disk usage high",
              f"{checks['disk_path']} is {pct:.1f}% full (threshold {checks['disk_pct']}%).")


def check_services(state, conf_path, checks):
    down = [s for s in checks["services"]
            if _run(["systemctl", "is-active", "--quiet", s]).returncode != 0]
    if down:
        alert(state, conf_path, "services", "CCC Head: service(s) down", "Down: " + ", ".join(down))


def _gpu_util():
    r = _run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"])
    if r.returncode != 0:
        return 0.0
    vals = [float(x) for x in r.stdout.split() if x.strip()]
    return max(vals) if vals else 0.0


def _cpu_util():
    return os.getloadavg()[0] / (os.cpu_count() or 1) * 100


def _head_has_running_job():
    r = _run(["squeue", "-h", "-w", os.uname().nodename, "-t", "RUNNING"])
    return r.returncode == 0 and r.stdout.strip() != ""


def check_rogue(state, conf_path, checks):
    gpu, cpu = _gpu_util(), _cpu_util()
    threshold = checks["rogue_util_pct"]
    rogue_now = not _head_has_running_job() and (gpu > threshold or cpu > threshold)
    streak = state.get("_rogue_streak", 0) + 1 if rogue_now else 0
    state["_rogue_streak"] = streak
    if streak >= checks["rogue_streak"]:
        alert(state, conf_path, "rogue", "CCC Head: unexplained GPU/CPU usage",
              f"GPU {gpu:.0f}% / CPU {cpu:.0f}% busy with no Slurm job running on this node, "
              f"sustained over {checks['rogue_streak']} checks.")


def main(checks_path, conf_path, state_path):
    checks = json.loads(Path(checks_path).read_text())
    state = _load_json(state_path, {})
    state["_cooldown_s"] = checks["cooldown_s"]
    check_disk(state, conf_path, checks)
    check_services(state, conf_path, checks)
    check_rogue(state, conf_path, checks)
    Path(state_path).write_text(json.dumps(state))


if __name__ == "__main__":
    main(*sys.argv[1:4])
'''

_TIMER_UNIT = f"""\
[Unit]
Description=CCC Head-box health monitor timer

[Timer]
OnUnitActiveSec={config.MONITOR_INTERVAL_MIN}min
OnBootSec={config.MONITOR_INTERVAL_MIN}min
Persistent=true

[Install]
WantedBy=timers.target
"""

_SERVICE_UNIT = f"""\
[Unit]
Description=CCC Head-box health check (disk, services, rogue GPU/CPU usage)

[Service]
Type=oneshot
User={config.HEAD_API_USER}
ExecStart=/usr/bin/python3 {config.MONITOR_SCRIPT} {config.MONITOR_CHECKS} {config.MONITOR_CONF} \
{config.MONITOR_STATE}
"""


def _write_ccc_file(path, content, write_unit) -> None:
    """Like `write_unit`, but ccc-owned/0600 instead of root — these are Mantle secrets/state, not
    system config that other services need to read."""
    write_unit(path, content, "600")
    run(["chown", f"{config.HEAD_API_USER}:{config.HEAD_API_USER}", str(path)], sudo=True)


def _read_existing_conf() -> dict:
    """Root-owned-by-mode-0600, ccc-owned by user — needs sudo to read back, same idiom as
    mesh._state_prefs(). Used only so `prompt_smtp()` can default to what's already configured on a
    re-run, instead of making the operator retype every field."""
    if not config.MONITOR_CONF.exists():
        return {}
    r = run(["cat", str(config.MONITOR_CONF)], sudo=True, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return {}
    conf = {}
    for line in r.stdout.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            conf[k.strip()] = v.strip()
    return conf


def install(write_unit) -> None:
    """Install the standalone script + checks.json + the timer. Idempotent — re-running `setup`
    regenerates checks.json (thresholds may have changed) but never touches monitor.yaml (the
    operator's SMTP creds) or monitor_state.json (in-flight cooldowns). Needs config.HEAD_STATE_DIR to
    already exist (ccc-owned) — call after provision_head_auth()."""
    write_unit(config.MONITOR_SCRIPT, _SCRIPT, "755")
    checks = {
        "disk_path": config.MONITOR_DISK_PATH,
        "disk_pct": config.MONITOR_DISK_PCT,
        "rogue_util_pct": config.MONITOR_ROGUE_UTIL_PCT,
        "rogue_streak": config.MONITOR_ROGUE_STREAK,
        "cooldown_s": config.MONITOR_COOLDOWN_S,
        "services": config.MONITOR_SERVICES,
    }
    _write_ccc_file(config.MONITOR_CHECKS, json.dumps(checks), write_unit)
    write_unit(config.MONITOR_SERVICE_UNIT, _SERVICE_UNIT, "644")
    write_unit(config.MONITOR_TIMER_UNIT, _TIMER_UNIT, "644")
    run(["systemctl", "daemon-reload"], sudo=True)
    run(["systemctl", "enable", "--now", f"{config.MONITOR_SERVICE}.timer"], sudo=True)


def prompt_smtp(write_unit) -> None:
    """Ask for SMTP host/port/user/password + from/to, write `config.MONITOR_CONF` (ccc:ccc, 0600).

    Runs after `install()` (needs HEAD_STATE_DIR + the ccc account to exist), unlike the W&B prompt
    which runs up front — this one simply can't move earlier. Blank host skips entirely: alerting
    stays unconfigured, the checks still run and log to `journalctl -u ccc-monitor`. On a re-run,
    every field defaults to what's already stored, so hitting Enter through all of them changes
    nothing; a blank password specifically means "keep the existing one" (a masked prompt has no
    visible current value to default to).
    """
    existing = _read_existing_conf()
    have = "configured" if existing else "unconfigured"
    print(f"SMTP alerting for Head-box monitoring (currently {have}; blank host = leave unconfigured)")
    host = ui.ask_text("SMTP host", default=existing.get("smtp_host", ""))
    if not host:
        print("cuttle: monitor alerting left unconfigured — checks still run, see "
              "`journalctl -u ccc-monitor`")
        return
    port = ui.ask_int("SMTP port", default=existing.get("smtp_port", "587"))
    user = ui.ask_text("SMTP username", default=existing.get("smtp_user", ""))
    password = ui.ask_password("SMTP password (blank = keep existing)")
    if not password:
        password = existing.get("smtp_password", "")
    from_addr = ui.ask_text("Alert 'from' address", default=existing.get("from_addr", user))
    to_addr = ui.ask_text("Alert 'to' address (your own inbox)", default=existing.get("to_addr", ""))
    conf = (f"smtp_host: {host}\nsmtp_port: {port}\nsmtp_user: {user}\n"
            f"smtp_password: {password}\nfrom_addr: {from_addr}\nto_addr: {to_addr}\n")
    _write_ccc_file(config.MONITOR_CONF, conf, write_unit)
    print(f"cuttle: SMTP alerting configured → {config.MONITOR_CONF}")
