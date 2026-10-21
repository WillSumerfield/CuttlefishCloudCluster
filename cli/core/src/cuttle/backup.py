"""`cuttle backup` — head.db + arms.json + Headscale's own db.sqlite, the Head's entire durable
state, bundled into one timestamped tarball.

The actual work — SQLite's online-backup API for the two live WAL dbs (safe against the Head API and
Karma tick writing concurrently; no downtime), the tarball, and the retention prune — lives in one
standalone script (`_SCRIPT`, installed at `config.BACKUP_SCRIPT`) shared by `create()` and the
nightly systemd timer: one implementation of "how to snapshot these three files," no cuttle import
so the timer's oneshot service runs it with nothing but a system python3.

Restore is the disruptive half: every service that can write one of the three files (the Enroll API
writes arms.json; the Head API + Karma tick write head.db; Headscale writes its own db) is stopped,
the archive's copies replace them at the right owner/perms, and everything restarts. All-or-nothing —
there is no selective single-store restore.

**`secret.key` is deliberately NOT in the archive.** It decrypts the Tide Pool private keys head.db
holds (ccc_head_api/atrest.py), and leaving it out is the entire value of encrypting them: the tarball
stays strictly *less* sensitive than the Head itself, which is the one thing at-rest encryption can buy
on a box that must decrypt unattended. This costs nothing to a restore — a Tide Pool row belongs to a
Spawn that was in the queue when the snapshot was taken and is long gone by the time anyone restores it.
"""

from pathlib import Path

from . import config
from .system import run

# Standalone (no cuttle import — see module docstring). Always runs as root: the only account that
# can read all three of head.db (ccc), arms.json (munge), and Headscale's db.sqlite (headscale).
_SCRIPT = r'''"""Snapshot/restore head.db + arms.json + Headscale's db.sqlite. Installed by cuttle at
this path; not meant to be run by hand. See cuttle/backup.py for why this exists standalone."""
import shutil, sqlite3, sys, tarfile, tempfile
from datetime import datetime, timezone
from pathlib import Path


def _sqlite_backup(src, dst):
    # SQLite's own online-backup API: safe to read a live WAL db with active writers, unlike a plain
    # file copy which can grab a torn snapshot mid-checkpoint.
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(str(dst))
    s.backup(d)
    d.close()
    s.close()


def create(head_db, arms_json, headscale_db, backup_dir, retain):
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    # Hour precision: a second `create()` within the same hour (a manual run right after the nightly
    # timer, an operator running it twice) shares a name on purpose and overrides the earlier one.
    name = "ccc-backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%HZ") + ".tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        _sqlite_backup(head_db, tmp / "head.db")
        _sqlite_backup(headscale_db, tmp / "headscale.sqlite")
        shutil.copy(arms_json, tmp / "arms.json")
        # Three files, and secret.key is pointedly not one of them — see cuttle/backup.py's docstring.
        with tarfile.open(tmp / name, "w:gz") as tf:
            for f in ("head.db", "arms.json", "headscale.sqlite"):
                tf.add(tmp / f, arcname=f)
        dest = backup_dir / name
        shutil.copy(tmp / name, dest)
        dest.chmod(0o600)
    retain = int(retain)
    stale = sorted(backup_dir.glob("ccc-backup-*.tar.gz"))[:-retain] if retain > 0 else []
    for f in stale:
        f.unlink()
    print(dest)


def restore(archive, head_db, arms_json, headscale_db):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with tarfile.open(archive) as tf:
            tf.extractall(tmp, filter="data")  # our own archive, but pin the safe filter regardless
        for name, dest, owner, group, mode in (
            ("head.db", head_db, "ccc", "ccc", 0o600),
            ("arms.json", arms_json, "munge", "munge", 0o644),
            ("headscale.sqlite", headscale_db, "headscale", "headscale", 0o644),
        ):
            dest = Path(dest)
            shutil.copy(tmp / name, dest)
            shutil.chown(dest, owner, group)
            dest.chmod(mode)
            # Stale WAL/SHM sidecars from the OLD db would otherwise replay against the restored
            # file the moment its service reopens it.
            for suffix in ("-wal", "-shm"):
                sidecar = dest.with_name(dest.name + suffix)
                if sidecar.exists():
                    sidecar.unlink()


if __name__ == "__main__":
    globals()[sys.argv[1]](*sys.argv[2:])
'''

_TIMER_UNIT = """\
[Unit]
Description=CCC nightly backup timer (head.db + arms.json + Headscale db)

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
"""

_SERVICE_UNIT = f"""\
[Unit]
Description=CCC backup (head.db + arms.json + Headscale db)

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {config.BACKUP_SCRIPT} create {config.HEAD_DB} {config.ARMS_REGISTRY} \
{config.HEADSCALE_DB} {config.BACKUP_DIR} {config.BACKUP_RETAIN}
"""

# The services that can write any of the three files. Stopped and restarted together — none depends
# on another's unit, so one order serves for both directions.
_WRITERS = [config.ENROLL_API_SERVICE, config.HEAD_API_SERVICE, config.KARMA_SERVICE, "headscale"]


def _ensure_script(write_unit) -> None:
    """(Re)install the standalone script if missing or stale — self-heals without a `setup` re-run."""
    write_unit(config.BACKUP_SCRIPT, _SCRIPT, "755")


def create(write_unit) -> Path:
    """Snapshot head.db + arms.json + Headscale's db.sqlite into one tarball, pruned to the newest
    `config.BACKUP_RETAIN`. No downtime — the Head API, Karma tick, and Headscale all keep running."""
    _ensure_script(write_unit)
    r = run(["python3", str(config.BACKUP_SCRIPT), "create", str(config.HEAD_DB),
             str(config.ARMS_REGISTRY), str(config.HEADSCALE_DB), str(config.BACKUP_DIR),
             str(config.BACKUP_RETAIN)],
            sudo=True, capture_output=True, text=True)
    return Path(r.stdout.strip())


def restore(archive: Path, write_unit) -> None:
    """Stop every service that can write head.db/arms.json/Headscale's db, replace all three from
    `archive`, restart. All-or-nothing — overwrites everything created since the backup."""
    _ensure_script(write_unit)
    run(["systemctl", "stop", *_WRITERS], sudo=True)
    run(["python3", str(config.BACKUP_SCRIPT), "restore", str(archive), str(config.HEAD_DB),
         str(config.ARMS_REGISTRY), str(config.HEADSCALE_DB)], sudo=True)
    run(["systemctl", "start", *_WRITERS], sudo=True)


def list_backups() -> list[Path]:
    """Newest-first. Needs sudo — BACKUP_DIR is root:root 0700 (the tarballs hold live secrets)."""
    r = run(["find", str(config.BACKUP_DIR), "-maxdepth", "1", "-name", "ccc-backup-*.tar.gz"],
            sudo=True, capture_output=True, text=True, check=False)
    return sorted((Path(p) for p in r.stdout.split()), reverse=True)


def install_timer(write_unit) -> None:
    """Nightly `create()`, belt-and-suspenders alongside the manual `cuttle backup create`."""
    _ensure_script(write_unit)
    write_unit(config.BACKUP_SERVICE_UNIT, _SERVICE_UNIT, "644")
    write_unit(config.BACKUP_TIMER_UNIT, _TIMER_UNIT, "644")
    run(["systemctl", "daemon-reload"], sudo=True)
    run(["systemctl", "enable", "--now", f"{config.BACKUP_SERVICE}.timer"], sudo=True)
