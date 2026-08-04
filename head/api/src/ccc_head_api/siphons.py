"""**Siphon** — the one channel a Spawn draws its project in and sends its results home through.

Mesh membership alone is not authorization: the mesh is a flat host network, so a shared `[project]` /
`[spawns]` rsync module would let any payload on any Arm read every other User's code or overwrite any
Spawn's `exit` marker. A Siphon replaces both with a pair of modules that exist only for one Spawn:

    [proj-<tok>]  read-only, rooted at that Spawn's own projects/<uuid>
    [out-<tok>]   writable, rooted at that Spawn's own spawns/<jid>

`auth users` alone isn't enough: rsyncd auth is per-module, so isolation has to come from the module
ROOT, not just a password — and the unguessable token stops a Spawn from naming another's module, since
`hosts allow` can't tell two Spawns on the same Arm apart.

**The file is written twice.** The module name is a submit-time token, not the job id, because
`[proj-…]` must exist before `sbatch` returns one. `open()` writes `[proj-<tok>]` alone; `bind()` adds
`[out-<tok>]` once the id is known and renames the file to `<jid>.conf` — that rename IS the bookkeeping,
no DB column needed. A conf still named for its token is one whose sbatch never returned; `sweep()` reaps
it by age.

**Closing is not Settlement.** `recover_scratch` copies home *after* `stage_out` delivers `exit`, so the
Head may settle Karma and drop the Spawn from the queue while up to `MAX_SPAWN_BYTES` is still in
flight. A Siphon closes when that copy's `recovery.size` marker lands, or the registration window closes
without one — whichever comes first.

**`rsyncd.d/` is Head-API-writable, a real edge.** A compromised Head API could write its own module
there (e.g. `uid = root`), but a compromised Head API already owns head.db/head.key/every upload, so
that's out of this phase's threat model. The `[bench]` module stays in the root-owned /etc conf instead,
where the Head API can't reach it at all.
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

# Included by the root-owned /etc/ccc-rsyncd.conf via `&include`, which reads every *.conf in here and
# re-reads them on EVERY connection — so opening/closing a Siphon needs no reload, no signal, no root.
# Read from the environment rather than off spawns.HEAD_STATE_DIR: this module is imported BY spawns.
RSYNCD_DIR = Path(os.environ.get("CCC_HEAD_STATE", "/var/lib/ccc/head")) / "rsyncd.d"

# A conf whose sbatch never returned. `bind()` follows `open()` within milliseconds, so anything still
# named for its token an hour later is debris from a submission that raised between the two.
_ORPHAN_MINUTES = 60

_JID_RE = re.compile(r"^\d+$")

# uid/gid/hosts allow/etc are inherited from the parent conf's &include'd globals — only what differs
# per module (root, writability) is stated here.
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

    Called BEFORE `sbatch`; only `[proj-<tok>]` is written here since the out module's root is
    `spawns/<jid>` and there is no jid yet.

    The token is the module name AND the rsync username; the secret goes into the job's
    `RSYNC_PASSWORD`. Both secrets-grade: the token is what an attacker would need to name another
    Spawn's module, and `hosts allow` can't tell two Spawns on one Arm apart."""
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

    The move IS the bookkeeping: `close()` unlinks by job id, no token→jid mapping needed anywhere.

    New files first, old ones dropped after — never a rename in place. rsyncd re-reads the directory on
    every connection: unlinking first would leave a window with no `[proj-<tok>]`, and moving the
    secrets file first would leave the old conf pointing at a path that's gone (an auth failure, not a
    missing file). This does leave `[proj-<tok>]` defined in two files briefly — rsync 3.2.7 serves that
    without complaint."""
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

    Reading it back (rather than threading it through the call) means the rewritten file can't disagree
    with what's already on disk — which is the failure that would silently point a Spawn at another
    User's upload."""
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

    Called at stage-in (ccc_head_api.projects), the moment a Spawn stops having business reading the
    Head's copy of its own upload — the copy is deleted in the same breath. The out module stays: the
    Spawn hasn't written anything yet.

    Rewritten from `_OUT_MOD` template rather than by editing the text, so it can't drift from what
    `bind()` wrote. Token is read back off disk for the same reason as `_uuid_of`."""
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

    Driven off the DIRECTORY, not the Spawn table — self-healing: a restarted Head or a skipped tick
    pass leaves files behind, and the next pass finds them since they're what's iterated. A Spawn still
    running is simply skipped and comes round again.

    Two closing conditions: the copy landed, or the window for it to land has passed. A Spawn over
    `recovery.MAX_SPAWN_BYTES` never sends a marker and closes on the window — correct, since it has no
    copy coming."""
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
