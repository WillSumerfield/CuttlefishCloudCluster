"""**Siphon** — the one channel a Spawn sends its results home through.

Mesh membership alone is not authorization: the mesh is a flat host network, so a shared `[spawns]`
rsync module would let any payload on any Arm overwrite any Spawn's `exit` marker. A Siphon replaces it
with a module that exists only for one Spawn:

    [out-<tok>]   writable, rooted at that Spawn's own spawns/<jid>

`auth users` alone isn't enough: rsyncd auth is per-module, so isolation has to come from the module
ROOT, not just a password — and the unguessable token stops a Spawn from naming another's module, since
`hosts allow` can't tell two Spawns on the same Arm apart.

**Minted before, bound after.** The module name is a submit-time token, not the job id, because the
token is baked into the job script before `sbatch` returns one. `open()` writes only the secrets file,
named for the token; `bind()` writes `[out-<tok>]` once the id is known and renames the pair to
`<jid>.*` — that rename IS the bookkeeping, no DB column needed. A secrets file still named for its token
is one whose sbatch never returned; `sweep()` reaps it by age.

**Closing is not Settlement.** A Siphon closes a grace window after its Spawn finished, not at
Settlement. (The close condition is re-decided with Dives.)

**`rsyncd.d/` is Head-API-writable, a real edge.** A compromised Head API could write its own module
there (e.g. `uid = root`), but a compromised Head API already owns head.db/head.key/every Spawn's results, so
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

from . import db

log = logging.getLogger("ccc.api")

# Included by the root-owned /etc/ccc-rsyncd.conf via `&include`, which reads every *.conf in here and
# re-reads them on EVERY connection — so opening/closing a Siphon needs no reload, no signal, no root.
# Read from the environment (the same var spawns.HEAD_STATE_DIR reads), not off spawns itself.
RSYNCD_DIR = Path(os.environ.get("CCC_HEAD_STATE", "/var/lib/ccc/head")) / "rsyncd.d"

# A token whose sbatch never returned. `bind()` follows `open()` within milliseconds, so anything still
# named for its token an hour later is debris from a submission that raised between the two.
_ORPHAN_MINUTES = 60

# How long a finished Spawn's Siphon stays open.
_CLOSE_GRACE_MINUTES = 10

_TS = "%Y-%m-%dT%H:%M:%SZ"

_JID_RE = re.compile(r"^\d+$")

# uid/gid/hosts allow/etc are inherited from the parent conf's &include'd globals — only what differs
# per module (root, writability) is stated here.
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
    that cannot send its results home. `os.replace` semantics mean a reader sees either the old file or
    the new one, never a partial."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.chmod(mode)
    tmp.replace(path)


def open() -> tuple[str, str]:
    """Mint a Siphon for a Spawn about to be submitted. Returns `(token, secret)`.

    Called BEFORE `sbatch`; only the secrets file is written here since the out module's root is
    `spawns/<jid>` and there is no jid yet.

    The token is the module name AND the rsync username; the secret goes into the job's
    `RSYNC_PASSWORD`. Both secrets-grade: the token is what an attacker would need to name another
    Spawn's module, and `hosts allow` can't tell two Spawns on one Arm apart."""
    RSYNCD_DIR.mkdir(parents=True, exist_ok=True)
    tok = uuid4().hex
    secret = secrets.token_urlsafe(32)
    _write(_secrets_file(tok), f"{tok}:{secret}\n", 0o600)
    return tok, secret


def bind(tok: str, job_id: int) -> None:
    """Write `[out-<tok>]` now that the Spawn has an id, and move the secret to that id.

    The move IS the bookkeeping: `close()` unlinks by job id, no token→jid mapping needed anywhere.
    New files first, old one dropped after — the conf never points at a secrets file that is gone."""
    from . import spawns
    name = str(job_id)
    sec = _secrets_file(name)
    _write(sec, _secrets_file(tok).read_text(), 0o600)
    _write(_conf(name), _OUT_MOD.format(tok=tok, path=spawns.SPAWNS_DIR / name, secrets=sec), 0o644)
    _secrets_file(tok).unlink(missing_ok=True)


def discard(tok: str) -> None:
    """Drop a Siphon whose Spawn never made it to a job id (sbatch raised). Nothing else will."""
    _conf(tok).unlink(missing_ok=True)
    _secrets_file(tok).unlink(missing_ok=True)


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

    A finished Spawn's Siphon closes once `_CLOSE_GRACE_MINUTES` have passed since it finished."""
    now = now or _now()
    if not RSYNCD_DIR.is_dir():
        return 0
    closed = 0
    cutoff = now - timedelta(minutes=_CLOSE_GRACE_MINUTES)
    for sec in RSYNCD_DIR.glob("*.secrets"):
        name = sec.stem
        if not _JID_RE.match(name):
            # Never bound. Its Spawn does not exist and never will.
            if datetime.fromtimestamp(sec.stat().st_mtime, timezone.utc) < now - timedelta(
                    minutes=_ORPHAN_MINUTES):
                discard(name)
                closed += 1
            continue
        job_id = int(name)
        row = db.get_spawn(conn, job_id)
        if row is None or row["finished_at"] is None:
            continue  # still queued or running — this is the Siphon doing its job
        if datetime.strptime(row["finished_at"], _TS).replace(tzinfo=timezone.utc) < cutoff:
            close(job_id)
            closed += 1
    return closed
