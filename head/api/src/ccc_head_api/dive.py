"""**Dives**, Head API side: request, attach, end, and the tick's close.

A Dive is one GPU session over a main folder + dep folders of the User's Tidepool. This process runs
as `ccc` and cannot read Tidepool files, so every step that touches them is the root helper
(`ccc-tidepool`, cuttle/ccc_tidepool.py) through sudoers:

1. `dive-prep <user> <tok>` — paths checked, real staging sizes, dotfiles bundled, `cuttle.toml` read.
2. here: settings = cuttle.toml overridden by the request's args; caps (25 GB, 1 GB dotfiles), Karma
   hold, single-writer on the main folder.
3. `dive-snap` — the User's system layer committed + pushed (only that layer uploads).
4. `dive-open` — cuttle.toml written back, the per-Dive rsync modules opened.
5. sbatch as the User's Guise; the job is one `ccc-launch`, which copies the folders onto whichever
   node Slurm picked — the Head included — and runs the Dive's container there (ccc_launch.py).

Attach: the User's own keys + the Tidepool's host key (staged into the Dive), so `ssh <name>-<id>`
pins the same host key as `ssh <name>`. An Arm Dive is reached through a Head-side relay (Users are
not Mesh members); a Head Dive's container publishes the same port on the Head itself, no relay.

End: the tick sees the job leave the queue (final mirror done inside KillWait) and runs `dive-close`:
lock lifted, modules gone, snapshot manifest deleted.
"""

import base64
import json
import logging
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import tomllib
from uuid import uuid4

from fastapi import HTTPException

from . import db, guises, karma, slurm, tidepool as tp_mod
from . import features as features_mod

log = logging.getLogger("ccc.api")

HELPER = tp_mod.HELPER
LAUNCH = "/usr/local/sbin/ccc-launch"     # cuttle.config.CCC_LAUNCH_BIN
MESH_IP = os.environ.get("CCC_MESH_IP", "")
CAP_BYTES = 25 * 2**30
MAX_MINUTES = 14 * 24 * 60                # cuttle.config.DIVE_MAX_MINUTES
TOML_KEYS = ("deps", "gpus", "vram", "time", "features", "env")

# The relay's listen range (cuttle.config.TIDEPOOL_PORT_BASE/RANGE — opened once in ufw at setup).
# Below Linux's ephemeral range (32768+): these are LISTENED on, by the relay here and the Dive's
# sshd on an Arm, so they must never race the kernel's outbound port allocator.
PORT_BASE = 31000
PORT_RANGE = 200


# --- settings: cuttle.toml + args -------------------------------------------------

_TIME_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$")


def parse_time(v) -> int:
    """Minutes, from `90`, `"90m"`, `"4h"`, `"2d"`, `"1d12h"`."""
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s.isdigit():
        return int(s)
    m = _TIME_RE.match(s)
    if not s or not m:
        raise ValueError(f"time {v!r} is not like 90m, 4h, 2d or 1d12h")
    d, h, mi = (int(x or 0) for x in m.groups())
    return d * 1440 + h * 60 + mi


def load_toml(text: str | None) -> dict:
    if not text:
        return {}
    try:
        t = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"cuttle.toml: {e}") from None
    return {k: t[k] for k in TOML_KEYS if k in t}


def saved_settings(user: dict, main: str) -> dict:
    read = helper("dive-settings", user["username"], main)
    with db.connect() as conn:
        for d in db.open_dives(conn, user["id"]):
            if d["main"] == read["main"]:
                raise HTTPException(409, f"~/{d['main']} is held by Dive {d['job_id']} — end it first")
    try:
        saved = load_toml(read["toml"])
        for key, value in saved.items():
            if key in ("gpus", "vram"):
                valid = type(value) is int and value >= 1
            elif key == "time":
                valid = type(value) in (int, str) and 1 <= parse_time(value) <= MAX_MINUTES
            elif key in ("deps", "features"):
                valid = isinstance(value, list) and all(isinstance(v, str) for v in value)
            else:
                valid = isinstance(value, str)
            if not valid:
                raise ValueError(f"cuttle.toml: invalid {key}")
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return {"main": read["main"], "settings": saved}


def _val(v) -> str:
    return json.dumps(v)   # str / int / list[str]: JSON spelling is valid TOML here


def dump_toml(d: dict) -> str:
    lines = ["# cuttle dive settings. `cuttle dive` here reuses them; flags override and rewrite this."]
    lines += [f"{k} = {_val(d[k])}" for k in TOML_KEYS if d.get(k) not in (None, [], "")]
    return "\n".join(lines) + "\n"


def merge(toml: dict, args: dict) -> dict:
    """The request's args win; anything not given comes from cuttle.toml."""
    return {**toml, **{k: v for k, v in args.items() if v is not None}}


# --- the helper -------------------------------------------------------------------

def helper(*args: str, stdin: dict | None = None) -> dict | None:
    """sudo -n ccc-tidepool …; a refusal (exit 2) is the User's to read (422), anything else is ours."""
    r = subprocess.run(["sudo", "-n", HELPER, *args], input=json.dumps(stdin) if stdin is not None else "",
                       capture_output=True, text=True)
    if r.returncode == 2:
        raise HTTPException(422, r.stderr.strip().removeprefix("ccc-tidepool: "))
    if r.returncode != 0:
        log.warning("ccc-tidepool %s failed: %s", " ".join(args), r.stderr.strip())
        raise HTTPException(502, f"the Head could not {args[0].removeprefix('dive-')} this Dive — ask an "
                                 f"admin to check the Head API log")
    return json.loads(r.stdout) if r.stdout.strip() else None


# --- the job --------------------------------------------------------------------

# Inside the User's snapshot on an Arm, as container root under catatonit: sshd answering with the
# Tidepool's own host key and the User's own keys; the environment built in the background beside its
# dependency file; zsh picks it up. sshd builds a fresh environment per session, so anything a shell
# needs goes in /etc/zsh, not the container env.
DIVE_INIT = r"""
set -eu
u="$CCC_USER"; h="/home/$u"
mkdir -p /run/sshd
cat > /etc/ssh/ccc-dive.conf <<'EOF'
Port 22
HostKey /etc/ccc/dive/ssh_host_ed25519_key
AuthorizedKeysFile .ssh/authorized_keys /etc/ccc/dive/authorized_keys
StrictModes no
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
UsePAM no
X11Forwarding no
AllowAgentForwarding yes
AllowTcpForwarding yes
AllowStreamLocalForwarding yes
StreamLocalBindUnlink yes
AcceptEnv CCC_FWD
PrintMotd no
SetEnv LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8
ClientAliveInterval 60
PidFile /run/sshd.pid
Subsystem sftp internal-sftp
EOF
venv=""
if [ -n "${CCC_DIVE_ENV:-}" ]; then
  f="$h/$CCC_DIVE_MAIN/$CCC_DIVE_ENV"
  venv="$(dirname "$f")/.venv"
fi
cat >> /etc/zsh/zshenv <<EOF
export UV_CACHE_DIR=/ccc-cache/uv MAMBA_ROOT_PREFIX=/ccc-cache/mamba PIP_CACHE_DIR=/ccc-cache/pip
export CCC_JOB_ID=$CCC_JOB_ID CCC_DIVE_VENV="$venv"
export PATH=/usr/local/lib/ccc/cuttle/path:\$PATH
EOF
cat >> /etc/zsh/zshrc <<'EOF'
case "$(cat /run/ccc-env.status 2>/dev/null)" in
  building) print -P "%F{yellow}ccc: environment still building — tail -f /run/ccc-env.log%f" ;;
  failed) print -P "%F{red}ccc: environment build failed — see /run/ccc-env.log%f" ;;
esac
if [ -d "$CCC_DIVE_VENV/conda-meta" ]; then
  eval "$(micromamba shell hook -s zsh)" && micromamba activate "$CCC_DIVE_VENV"
elif [ -f "$CCC_DIVE_VENV/bin/activate" ]; then
  source "$CCC_DIVE_VENV/bin/activate"
fi
EOF
if [ -n "$venv" ]; then
  echo building > /run/ccc-env.status
  ( cd "$(dirname "$f")" && runuser -u "$u" -- env HOME="$h" UV_CACHE_DIR=/ccc-cache/uv \
      MAMBA_ROOT_PREFIX=/ccc-cache/mamba PIP_CACHE_DIR=/ccc-cache/pip sh -c '
      case "$(basename "$1")" in
        pyproject.toml|uv.lock) uv sync ;;
        *.txt) uv venv -q .venv && VIRTUAL_ENV="$PWD/.venv" uv pip install -r "$1" ;;
        *.yml|*.yaml) micromamba create -y -p .venv -f "$1" ;;
        *) echo "ccc: cannot build $1 — name a pyproject.toml, requirements .txt or environment.yml"; exit 1 ;;
      esac' _ "$f" > /run/ccc-env.log 2>&1 \
    && echo ok > /run/ccc-env.status || echo failed > /run/ccc-env.status ) &
fi
exec /usr/sbin/sshd -D -e -f /etc/ssh/ccc-dive.conf -o "AllowUsers=$u"
"""

JOB_SCRIPT = """\
#!/bin/bash
#SBATCH --job-name=ccc-dive
#SBATCH --gres=gpu:{gpus}
#SBATCH --time={minutes}
#SBATCH --chdir=/tmp
#SBATCH --output=/tmp/ccc-%j-{tag}.slurm
#SBATCH --error=/tmp/ccc-%j-{tag}.slurm
{constraint}{exclude}{spec_line}
exec sudo -n {launch} "$SLURM_JOB_ID"
"""


def spec_line(spec: dict) -> str:
    """cuttle.templates.launch_spec_line — what ccc-launch reads back from slurmctld's copy."""
    return "# ccc-launch-spec: " + base64.b64encode(json.dumps(spec, sort_keys=True).encode()).decode()


def render_job(spec: dict, gpus: int, minutes: int, constraint: str, exclude: str) -> str:
    return JOB_SCRIPT.format(
        gpus=gpus, minutes=minutes, tag=uuid4().hex[:8], launch=LAUNCH, spec_line=spec_line(spec),
        constraint=f"#SBATCH --constraint={constraint}\n" if constraint else "",
        exclude=f"#SBATCH --exclude={exclude}\n" if exclude else "")


def make_spec(user: str, tok: str, secret: str, image: str, main: str, deps: list[str], port: int,
              env: str | None, features: list[str]) -> dict:
    spec = {"image": image, "argv": ["/bin/sh", "-c", DIVE_INIT], "network": True,
            "env": {"CCC_USER": user, "CCC_DIVE_MAIN": main, "CCC_DIVE_ENV": env or ""},
            "dive": {"token": tok, "user": user, "secret": secret, "head": MESH_IP, "main": main,
                     "deps": deps, "port": port}}
    # ccc-launch resolves each name against this Arm's own /var/lib/ccc/features/<name>/profile.json
    # (cuttle.features) and applies its binds + env. Only when some were asked for, so an ordinary
    # Dive's spec is byte-for-byte what it was before Features existed.
    if features:
        spec["features"] = features
    return spec


# --- request --------------------------------------------------------------------

def reserve_port(conn) -> int:
    for _ in range(10):
        used = db.used_dive_ports(conn)
        port = next((p for p in range(PORT_BASE, PORT_BASE + PORT_RANGE) if p not in used), None)
        if port is None:
            raise HTTPException(503, f"no free Dive port in {PORT_BASE}-{PORT_BASE + PORT_RANGE - 1} — "
                                     f"too many Dives at once")
        if db.reserve_dive_port(conn, port):
            return port
    raise HTTPException(503, "could not reserve a Dive port — try again")


def _gb(b: int) -> str:
    return f"{b / 2**30:.1f} GB"


def check_sizes(prep: dict) -> None:
    if prep["dotfiles"] > prep["dotfile_cap"]:
        big = ", ".join(f"{d['path']} ({_gb(d['bytes'])})" for d in prep["dotfiles_biggest"])
        raise HTTPException(422, f"your dotfiles are {_gb(prep['dotfiles'])} — over the "
                                 f"{_gb(prep['dotfile_cap'])} cap; biggest: {big}")
    parts = {**{f"~/{f}": b for f, b in prep["folders"].items()},
             "system layer": prep["layer"], "dotfiles": prep["dotfiles"]}
    total = sum(parts.values())
    if total > CAP_BYTES:
        each = ", ".join(f"{k} {_gb(v)}" for k, v in sorted(parts.items(), key=lambda kv: -kv[1]))
        raise HTTPException(422, f"this Dive is {_gb(total)} — over the {_gb(CAP_BYTES)} cap ({each}). "
                                 f"Trim with a .cuttleignore, or move data out of the staged folders")


def settings(toml: dict, args: dict) -> dict:
    """Validated Dive settings from cuttle.toml + args."""
    s = merge(toml, args)
    if s.get("time") is None:
        raise HTTPException(422, "a Dive needs --time (e.g. 4h, 2d; at most 14d) — or `time` in "
                                 "cuttle.toml")
    try:
        minutes = parse_time(s["time"])
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    if not 1 <= minutes <= MAX_MINUTES:
        raise HTTPException(422, f"--time must be between 1m and 14d (got {minutes} minutes)")
    if s.get("vram") is None:
        raise HTTPException(422, "a Dive needs --vram: total GB of VRAM across its GPUs (e.g. 16) — or "
                                 "`vram` in cuttle.toml")
    gpus, vram = int(s.get("gpus") or 1), int(s["vram"])
    if gpus < 1:
        raise HTTPException(422, "a Dive holds at least one GPU")
    if vram < 1:
        raise HTTPException(422, "--vram must be at least 1 (GB)")
    feats = sorted(set(s.get("features") or []))
    env = s.get("env") or None
    if env is not None and (env.startswith("/") or ".." in env.split("/")):
        raise HTTPException(422, "env must be a dependency file inside the main folder (e.g. pyproject.toml)")
    if (need := features_mod.profiled(feats)) and not env:
        raise HTTPException(422, f"{', '.join(need)} needs an environment — name a dependency file "
                                 f"with --env")
    return {**s, "minutes": minutes, "gpus": gpus, "vram": vram, "features": feats, "env": env}


def constraint_for(s: dict) -> str:
    c = slurm.vram_constraint(s["vram"], s["gpus"])
    if s["features"]:
        if not slurm.arms_with_features(s["features"]):
            known = ", ".join(slurm.all_feature_names()) or "(none)"
            raise HTTPException(422, f"no Arm has every requested feature ({', '.join(s['features'])}) "
                                     f"— Arm Features in the cluster: {known}")
        f = slurm.feature_constraint(s["features"])
        c = f"({c})&{f}" if c else f
    return c


def request(user: dict, main: str, deps: list[str] | None, args: dict) -> dict:
    """Everything POST /dives does past auth. Returns what the CLI prints."""
    if not MESH_IP:
        raise HTTPException(503, "the Head does not know its mesh address (CCC_MESH_IP) — re-run "
                                 "`cuttle setup --head`")
    name, tok = user["username"], uuid4().hex
    prep = helper("dive-prep", name, tok, stdin={"main": main, "deps": deps or []})
    port = job_id = None
    try:
        main, toml = prep["main"], load_toml(prep["toml"])
        s = settings(toml, {**args, "deps": deps})
        deps = prep["deps"] if deps is not None else list(s.get("deps") or [])
        if deps != prep["deps"]:   # deps came from cuttle.toml: prep again so sizes + checks cover them
            prep = helper("dive-prep", name, tok, stdin={"main": main, "deps": deps})
        check_sizes(prep)
        notes = []
        with db.connect() as conn:
            for d in db.open_dives(conn, user["id"]):
                if d["main"] == main:
                    raise HTTPException(409, f"~/{main} is held by Dive {d['job_id']} — end it first "
                                             f"(`cuttle dive end {d['job_id']}`)")
                if d["main"] in deps:
                    notes.append(f"~/{d['main']} is live in Dive {d['job_id']} — you get what it last "
                                 f"mirrored to the Head")
            exclude = ",".join(sorted(db.retracted_nodes(conn)))
            constraint = constraint_for(s)
            need = {"vram_gb": s["vram"], "gpus": s["gpus"]}
            hold = karma.quote_hold(conn, slurm.arms(), need, s["minutes"], s["gpus"], user["is_admin"])
            guise = guises.name(db.allocate_guise(conn, user["id"]))
            port = reserve_port(conn)
        new = {k: v for k, v in merge(toml, {**args, "deps": deps}).items()
               if k in TOML_KEYS and v not in (None, [], "")}
        image = helper("dive-snap", name, tok)["image"]
        secret = secrets.token_urlsafe(32)
        with db.connect() as conn:   # the Dive's ambient session (`cuttle` inside), gone at its close
            session = db.create_session(conn, user["id"], scope="dive", dive=tok)
        helper("dive-open", name, tok, stdin={
            "main": main, "deps": deps, "secret": secret, "image": image, "session": session,
            "toml": dump_toml(new) if load_toml(dump_toml(new)) != toml else None})
        spec = make_spec(name, tok, secret, image, main, deps, port, s["env"], s["features"])
        body = render_job(spec, s["gpus"], s["minutes"], constraint, exclude)
        try:
            r = subprocess.run(guises.as_owner(["sbatch", "--parsable", "--no-requeue"], guise),
                               input=body, cwd="/", capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            said = " ".join((e.stderr or e.stdout or "").split()) or f"exit {e.returncode}"
            raise HTTPException(422, f"sbatch refused this Dive: {said}") from None
        job_id = int(r.stdout.strip().split(";")[0])
        with db.connect() as conn:
            db.create_dive(conn, job_id, user["id"], tok, main, deps, port, minutes=s["minutes"],
                           gpus=s["gpus"], vram_gb=s["vram"], hold=hold)
    except BaseException:
        if job_id is not None:
            try:
                slurm.cancel(job_id)
            except Exception:
                log.exception("could not cancel unrecorded Dive %s", job_id)
        try:
            helper("dive-close", tok)
        except Exception:
            log.exception("could not close Dive %s's helper state", tok)
        with db.connect() as conn:
            db.delete_dive_session(conn, tok)
            if port is not None:
                db.release_dive_port(conn, port)
        raise
    log.info("dive id=%s user=%s main=%s deps=%s gpus=%s minutes=%s", job_id, name, main, deps,
             s["gpus"], s["minutes"])
    return {"id": job_id, "main": main, "deps": deps, "minutes": s["minutes"], "gpus": s["gpus"],
            "vram": s["vram"], "features": s["features"], "env": s["env"], "notes": notes,
            "size_gb": round(sum(prep["folders"].values()) / 2**30 + prep["layer"] / 2**30
                             + prep["dotfiles"] / 2**30, 2)}


# --- attach -----------------------------------------------------------------------

def where(node: str | None, arms: list[dict] | None = None) -> str | None:
    """A node as Users name it: the Head is the cluster name, an Arm its node name."""
    heads = {a["name"] for a in (slurm.arms() if arms is None else arms) if a.get("head")}
    return tp_mod.cluster_name() if node and node in heads else node


def queue(limit: int | None = None) -> list[dict]:
    """The live queue — every pending/running job in the CCC partition, with who started it (Slurm
    names a Guise). `limit` caps to the front (running + next-to-run first)."""
    rows = slurm.dives()[:limit] if limit else slurm.dives()
    ids = [r["id"] for r in rows if r["id"] is not None]
    with db.connect() as conn:
        starters, mains = db.dive_starters(conn, ids), db.dive_mains(conn, ids)
    for r in rows:
        r["started_by"], r["main"] = starters.get(r["id"]), mains.get(r["id"])
    return rows


def rows(user_id: int | None = None) -> list[dict]:
    """Open Dives with Slurm's view of each (state, node, times)."""
    live = {j["id"]: j for j in slurm.dives()}
    arms = slurm.arms() if live else []
    with db.connect() as conn:
        out = []
        for d in db.open_dives(conn, user_id):
            j = live.get(d["job_id"]) or {}
            out.append({"id": d["job_id"], "main": d["main"], "deps": json.loads(d["deps"]),
                        "state": j.get("state") or "ending", "node": where(j.get("node"), arms),
                        "gpus": d["gpus"], "minutes": d["minutes"], "start_time": j.get("start_time"),
                        "time_limit": j.get("time_limit"), "user_id": d["user_id"]})
    return out


def outcome(r) -> str:
    """How a finished Dive ended: `ended` (someone ended it), `timed out` (ran its --time), or `lost`
    (its job died first — the Arm dropped, the node rebooted). Slurm keeps no accounting, so the last
    two are told apart by run length alone."""
    if r["cancelled"]:
        return "ended"
    if r["started_at"] and r["minutes"] and _secs(r["finished_at"]) - _secs(r["started_at"]) >= \
            r["minutes"] * 60 - 60:
        return "timed out"
    return "lost"


def _secs(ts: str) -> float:
    return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))


def archive(r) -> dict:
    """A history row as served: the archive columns plus its `outcome` (None while unfinished)."""
    out = {k: r[k] for k in r.keys() if k not in ("cancelled", "started_at")}
    out["deps"] = json.loads(r["deps"])
    out["outcome"] = outcome(r) if r["finished_at"] else None
    return out


def history(days: int, limit: int | None) -> list[dict]:
    """Every Dive started within `days` that the live queue has forgotten, newest first. Everyone's:
    what ran on the shared cluster is not private."""
    live = [r["id"] for r in slurm.dives() if r["id"] is not None]
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
    with db.connect() as conn:
        return [archive(r) for r in db.dive_history(conn, since, live, limit)]


def detail(user: dict, job_id: int) -> dict:
    """One Dive, whole: state + archive for anyone; for its owner also what `ssh <name>-<id>` needs
    (`ready` = an sshd answered; polling it brings an Arm Dive's relay up)."""
    with db.connect() as conn:
        row, d = db.dive_detail(conn, job_id), db.get_dive(conn, job_id)
    if row is None:
        raise HTTPException(404, f"no Dive {job_id}")
    out = archive(row)
    state = slurm.state(job_id)
    if state is not None:
        out["state"] = state.lower()
    else:
        out["state"] = out["outcome"] or "ending"
    out["done"] = state is None and d["closed"] == 1
    if d["user_id"] != user["id"]:
        return out
    out |= {"user": user["username"], "host_key": tp_mod.host_key(user["username"]),
            "alias": f"{tp_mod.cluster_name()}-{job_id}", "ready": False, "port": None}
    if state != "RUNNING":
        return out
    node = next((s["node"] for s in slurm.dives() if s["id"] == job_id), None)
    arm = next((a for a in slurm.arms() if a["name"] == node), None) if node else None
    if arm is None or not arm.get("mesh_ip"):
        return out
    if arm.get("head"):
        up = True   # the container publishes the port here itself (a relay would take the same one)
    else:
        with db.connect() as conn:
            up = ensure_relay(conn, job_id, arm["mesh_ip"], node)
    return out | {"ready": up and _banner(d["port"]), "node": where(node, [arm]), "port": d["port"]}


def end(user: dict, job_id: int) -> None:
    """scancel as the User's Guise: Slurm TERMs the job, the shell stops, the final mirror runs inside
    KillWait, and the tick closes it once the job is gone. Only your own: ending someone else's is
    the operator's, on the Head (`cuttle dive end` takes the 403 as its cue)."""
    with db.connect() as conn:
        d = db.get_dive(conn, job_id)
        if d is None:
            raise HTTPException(404, f"no Dive {job_id}")
        if d["user_id"] != user["id"]:
            raise HTTPException(403, "you can only end your own Dives")
        slurm.cancel(job_id)
        db.mark_cancelled(conn, job_id)


# --- relay --------------------------------------------------------------------------
# Users are not Mesh members, so an Arm Dive's sshd (on the Arm's mesh address) is reached through
# a raw TCP forwarder here (relay.py), its own process, one per running Arm Dive.

_LISTEN_TIMEOUT = 5.0


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _accepting(port: int, timeout: float = _LISTEN_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


def _banner(port: int, timeout: float = 2.0) -> bool:
    """An sshd answered through the relay — not just the relay listening (stage-in can take minutes)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.settimeout(timeout)
            return s.recv(4) == b"SSH-"
    except OSError:
        return False


def ensure_relay(conn, job_id: int, node_mesh_ip: str, node: str) -> bool:
    """Idempotent: polled from GET /dives/{id}; starts the relay if it is not running."""
    row = db.get_dive(conn, job_id)
    if row is None or row["closed"]:
        return False
    if _alive(row["relay_pid"]):
        return True
    port = row["port"]
    proc = subprocess.Popen([sys.executable, "-m", "ccc_head_api.relay", str(port), node_mesh_ip, str(port)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    db.set_dive_relay(conn, job_id, proc.pid, node)
    log.info("dive relay job=%s port=%s -> %s pid=%s", job_id, port, node_mesh_ip, proc.pid)
    return _accepting(port)


def _kill(pid: int | None) -> None:
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def reap_dead(conn, live_job_ids: set[int]) -> None:
    """Every tick: a Dive whose job has left the queue (its final mirror is done — the job only
    leaves once ccc-launch has exited) is closed: relay down, helper dive-close, port released.
    A helper failure leaves it open, to retry next tick."""
    for row in db.open_dives(conn):
        if row["job_id"] in live_job_ids:
            continue
        _kill(row["relay_pid"])
        r = subprocess.run(["sudo", "-n", HELPER, "dive-close", row["token"]], capture_output=True,
                           text=True)
        if r.returncode != 0:
            log.warning("dive-close %s (job %s) failed: %s", row["token"], row["job_id"], r.stderr.strip())
            continue
        db.delete_dive_session(conn, row["token"])
        db.close_dive(conn, row["job_id"])
