"""**Siphon** (temp/spawn-isolation.md Phase 2) — the one channel a Spawn draws its project in and
sends its results home through, and the end of `rsync://HEAD/project/` being every User's upload.

Before this, being on the **Mesh** was the whole of the authorization: two modules, `[project]`
(read-only, rooted at *every* uploaded project) and `[spawns]` (writable, rooted at *every* Spawn's
results). Any payload on any Arm — the mesh is a flat host network, and the job script's own rsync
lines told it the URL — could read every other User's code and overwrite any Spawn's `exit` marker.
No uid change and no namespace closes that: it is a network service, and the Guise a Spawn runs as is
exactly who is allowed to talk to it.

A Siphon replaces both with a pair of modules that exist only for one Spawn:

    [proj-<tok>]  read-only, rooted at that Spawn's own projects/<uuid>
    [out-<tok>]   writable, rooted at that Spawn's own spawns/<jid>

`auth users` alone would not do this. rsyncd authentication is per-module, so a shared `[spawns]` with
a password still hands out the whole tree to anyone holding it — the isolation has to come from the
module ROOT, and a per-Spawn root means a per-Spawn module. The secret on top of it is what stops a
Spawn from simply naming another Spawn's module: the token is unguessable, but `hosts allow` cannot
tell two Spawns on the same Arm apart, so unguessable is not on its own enough.

## Why the file is written twice

The module name is a **submit-time token**, not the job id, because `[proj-…]` has to exist before
`sbatch` — the job stages in within milliseconds of starting, and sbatch returning the id is the same
instant the job becomes runnable. So `open()` writes the conf with `[proj-<tok>]` alone (rooted at the
uuid, which submit already knows), and `bind()` rewrites it with `[out-<tok>]` added once the id is
known. Nothing can reach the out module in that window: a job must clear the Prolog, stage in, and run
its payload before it stages anything back.

`bind()` also renames the file to `<jid>.conf`, which is the whole of the bookkeeping — no DB column,
no lookup, and an operator reading the directory sees exactly which Spawns hold a grant. A conf still
named for its token is by definition one whose sbatch never returned, and `sweep()` reaps it by age.

## Why closing is not Settlement

The two are different moments and coupling them would silently break the Recovery Window:
`recover_scratch` sends its copy *after* `stage_out` has already delivered the `exit` file, so the
Head has settled the Spawn's Karma and dropped it from the queue while up to `MAX_SPAWN_BYTES` may
still be in flight. A Siphon therefore closes when that copy's `recovery.size` marker lands, or when
the registration window closes without one, whichever comes first — the same signal
`recovery.register_new` already watches for, in the same tick pass.

## The directory is ccc-writable, and that is a real edge

`rsyncd.d/` has to be writable by the Head API, which is what mints Siphons — so a compromised Head
API could write a module of its own choosing there, including one that sets `uid = root` and serves
the whole filesystem. That is outside this phase's threat model (a compromised Head API already owns
head.db, head.key, and every upload) and closing it would need the NOPASSWD root grant this project
refuses. The `[bench]` module and its secret therefore stay in the root-owned /etc conf, where the
Head API cannot reach them at all.
"""

import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from . import db, recovery

log = logging.getLogger("ccc.api")

# Included by the root-owned /etc/ccc-rsyncd.conf via `&include`, which reads every *.conf in here
# (non-recursive, alpha order) and re-reads them on EVERY connection — so opening and closing a Siphon
# needs no reload, no signal, and no root. The `.secrets` files sit right beside their modules and are
# ignored by the parser, which only ever looks at *.conf and *.inc.
#
# Read from the environment the same way spawns.HEAD_STATE_DIR is, rather than off that module — this
# one is imported BY spawns, so reaching back for it would be a cycle.
RSYNCD_DIR = Path(os.environ.get("CCC_HEAD_STATE", "/var/lib/ccc/head")) / "rsyncd.d"

# A conf whose sbatch never returned. `bind()` follows `open()` within milliseconds, so anything still
# named for its token an hour later is debris from a submission that raised between the two.
_ORPHAN_MINUTES = 60

_JID_RE = re.compile(r"^\d+$")

# `uid`/`gid`/`use chroot`/`hosts allow`/`strict modes`/`list` are all inherited from the parent conf's
# globals — an &include'd file starts parsing with the parent's defaults, so the mesh-only `hosts allow`
# and the drop to `ccc` apply here without this file having to know either of them. What is stated is
# only what differs per module: the root, and whether it may be written to.
_PROJ_MOD = """\
[proj-{tok}]
    path = {path}
    auth users = {tok}
    secrets file = {secrets}
"""

_OUT_MOD = """\
[out-{tok}]
    path = {path}
    read only = false
    auth users = {tok}
    secrets file = {secrets}
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _conf(name: str) -> Path:
    return RSYNCD_DIR / f"{name}.conf"


def _secrets_file(name: str) -> Path:
    return RSYNCD_DIR / f"{name}.secrets"


def _write(path: Path, text: str, mode: int) -> None:
    """Write via a temp file in the same directory and rename over the target.

    rsyncd re-reads this directory on every connection, so a plain write is a window in which a
    connecting Spawn can parse a half-written module — which reads as "module unknown", i.e. a Spawn
    that cannot fetch its own code. `os.replace` semantics mean a reader sees either the old file or
    the new one, never a partial."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.chmod(mode)
    tmp.replace(path)


def open(uuid: str) -> tuple[str, str]:
    """Mint a Siphon for a Spawn about to be submitted. Returns `(token, secret)`.

    Called BEFORE `sbatch`, and that ordering is the reason the token exists at all — see the module
    docstring. Only `[proj-<tok>]` is written here, since the out module's root is `spawns/<jid>` and
    there is no jid yet.

    The token is the module name AND the rsync username; the secret is what goes into the job's
    `RSYNC_PASSWORD`. Both are `secrets`-grade: the token is what an attacker would have to guess to
    even name another Spawn's module, and `hosts allow` cannot tell two Spawns on one Arm apart."""
    RSYNCD_DIR.mkdir(parents=True, exist_ok=True)
    tok = uuid4().hex
    secret = secrets.token_urlsafe(32)
    from . import spawns
    _write(_secrets_file(tok), f"{tok}:{secret}\n", 0o600)
    _write(_conf(tok), _PROJ_MOD.format(tok=tok, path=spawns.PROJECTS_DIR / uuid,
                                        secrets=_secrets_file(tok)), 0o644)
    return tok, secret


def bind(tok: str, job_id: int) -> None:
    """Add `[out-<tok>]` now that the Spawn has an id, and move the pair to that id.

    The move IS the bookkeeping: `close()` unlinks by job id and needs no token→jid mapping anywhere.

    New files first, old ones dropped after — never a rename in place. rsyncd re-reads this directory
    on every connection, so any ordering that unlinks first leaves a window where `[proj-<tok>]` does
    not exist, and any ordering that moves the secrets file first leaves the old conf pointing at a
    path that is gone (which rsyncd reports as an auth failure, not a missing file). This way every
    conf on disk always names a secrets file that exists. It does leave `[proj-<tok>]` defined in two
    files for the duration, which rsync 3.2.7 serves without complaint — probed, since a Spawn CAN in
    principle start the instant sbatch returns."""
    from . import spawns
    name = str(job_id)
    sec = _secrets_file(name)
    _write(sec, _secrets_file(tok).read_text(), 0o600)
    _write(_conf(name),
           _PROJ_MOD.format(tok=tok, path=spawns.PROJECTS_DIR / _uuid_of(tok), secrets=sec)
           + _OUT_MOD.format(tok=tok, path=spawns.SPAWNS_DIR / name, secrets=sec), 0o644)
    _conf(tok).unlink(missing_ok=True)
    _secrets_file(tok).unlink(missing_ok=True)


def _uuid_of(tok: str) -> str:
    """The project uuid `open()` wrote into the proj module, read back rather than passed down again.

    `bind()` is called from the one place `open()` was, so threading the uuid through would work — but
    reading it back means the rewritten file cannot disagree with the one already on disk, which is the
    failure that would silently point a Spawn at another User's upload."""
    for line in _conf(tok).read_text().splitlines():
        if line.strip().startswith("path ="):
            return line.split("=", 1)[1].strip().rsplit("/", 1)[1]
    raise ValueError(f"siphon {tok} has no path line")


def discard(tok: str) -> None:
    """Drop a Siphon whose Spawn never made it to a job id (sbatch raised). Nothing else will."""
    _conf(tok).unlink(missing_ok=True)
    _secrets_file(tok).unlink(missing_ok=True)


def close_proj(job_id: int) -> None:
    """Revoke the `[proj-…]` half alone, leaving `[out-…]` open. Idempotent.

    Called at stage-in (ccc_head_api.projects), which is the moment a Spawn stops having any business
    reading the Head's copy of its own upload — the copy is deleted in the same breath, and a module
    rooted at a path that no longer exists is one rsyncd reports as an auth failure rather than a
    missing file. The out module has to stay: the Spawn has not written anything yet.

    Rewritten from `_OUT_MOD`, not by editing the file's text, so the surviving module is generated by
    the same template `bind()` wrote it with and cannot drift from it. The token is read back off disk
    for the reason `_uuid_of` is — it is the one copy that cannot disagree with itself."""
    name = str(job_id)
    conf = _conf(name)
    try:
        text = conf.read_text()
    except OSError:
        return  # already closed outright, or never bound
    if "[proj-" not in text:
        return  # this half is already gone
    tok = text.split("[proj-", 1)[1].split("]", 1)[0]
    from . import spawns
    _write(conf, _OUT_MOD.format(tok=tok, path=spawns.SPAWNS_DIR / name,
                                 secrets=_secrets_file(name)), 0o644)


def close(job_id: int) -> None:
    """Revoke one Spawn's Siphon. Unlinking the conf is the revocation — the next connection re-reads
    the directory and the modules are simply not there any more."""
    _conf(str(job_id)).unlink(missing_ok=True)
    _secrets_file(str(job_id)).unlink(missing_ok=True)


def sweep(conn, now: datetime | None = None) -> int:
    """Close every Siphon whose Spawn has finished transferring, and reap orphans. Returns how many.

    Driven off the DIRECTORY rather than off the Spawn table, which is what makes it self-healing: a
    Head that restarted mid-submission, or a tick pass that was skipped, leaves files behind, and the
    next pass finds them because they are the thing being iterated. A Spawn still running is simply
    not closed and comes round again next pass.

    Two closing conditions, per temp/spawn-isolation.md Phase 2 — the copy landed, or the window for it
    to land has passed. A Spawn over `recovery.MAX_SPAWN_BYTES` never sends a marker at all and closes
    on the window, which is correct: it has no copy coming."""
    now = now or _now()
    if not RSYNCD_DIR.is_dir():
        return 0
    from . import spawns
    closed = 0
    cutoff = now - timedelta(minutes=recovery._REGISTRATION_WINDOW_MINUTES)
    for conf in RSYNCD_DIR.glob("*.conf"):
        name = conf.stem
        if not _JID_RE.match(name):
            # Never bound. Its Spawn does not exist and never will.
            if datetime.fromtimestamp(conf.stat().st_mtime, timezone.utc) < now - timedelta(
                    minutes=_ORPHAN_MINUTES):
                discard(name)
                closed += 1
            continue
        job_id = int(name)
        row = db.get_spawn(conn, job_id)
        if row is None or row["finished_at"] is None:
            continue  # still queued or running — this is the Siphon doing its job
        if (spawns.SPAWNS_DIR / name / "recovery.size").exists() or recovery._parse(
                row["finished_at"]) < cutoff:
            close(job_id)
            closed += 1
    return closed
