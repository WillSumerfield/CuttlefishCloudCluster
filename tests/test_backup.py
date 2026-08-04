"""Backup/restore for head.db + arms.json + Headscale's db.sqlite.

The actual snapshot/restore logic lives in `cuttle.backup._SCRIPT`, a standalone string installed
at `config.BACKUP_SCRIPT` (no cuttle import — must run unattended under a bare `python3` from the
nightly systemd timer). These tests `exec` that string into its own module namespace, exactly as
the installed file would run, with `shutil.chown` stubbed out (no root, and no `ccc`/`munge`/
`headscale` accounts to chown to, in this sandbox).
"""

import json
import sqlite3
import types

import pytest

from cuttle import backup


@pytest.fixture
def script():
    """The installed script's module, minus the privilege-only chown step."""
    mod = types.ModuleType("ccc_backup_under_test")
    exec(compile(backup._SCRIPT, "<ccc-backup.py>", "exec"), mod.__dict__)
    mod.shutil.chown = lambda *a, **k: None  # no root in the test sandbox; see module docstring
    return mod


def _sqlite_db(path, table, row):
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(f"CREATE TABLE {table} (v TEXT)")
    c.execute(f"INSERT INTO {table} VALUES (?)", (row,))
    c.commit()
    c.close()


@pytest.fixture
def stores(tmp_path):
    """A fake head.db + arms.json + headscale db.sqlite, WAL-mode like the real ones."""
    head_db, headscale_db, arms_json = tmp_path / "head.db", tmp_path / "headscale.sqlite", tmp_path / "arms.json"
    _sqlite_db(head_db, "users", "admin")
    _sqlite_db(headscale_db, "nodes", "arm1")
    arms_json.write_text(json.dumps({"arm1": {"owner": "admin"}}))
    return head_db, arms_json, headscale_db


class TestCreate:
    """A live WAL db with an open connection is exactly what create() must capture correctly — a
    plain `cp` can grab a torn snapshot mid-checkpoint, which is the whole reason for the
    online-backup API."""

    def test_a_backup_bundles_all_three_stores(self, script, stores, tmp_path):
        head_db, arms_json, headscale_db = stores
        backup_dir = tmp_path / "backups"
        script.create(str(head_db), str(arms_json), str(headscale_db), str(backup_dir), 3)
        archives = list(backup_dir.glob("ccc-backup-*.tar.gz"))
        assert len(archives) == 1
        assert archives[0].stat().st_mode & 0o777 == 0o600  # the bundle holds live secrets

    def test_a_second_call_the_same_hour_overrides_not_accumulates(self, script, stores, tmp_path):
        """Hour-precision names are deliberate: a manual run right after the nightly timer, or run
        twice by hand, shares a name and overrides rather than piling up."""
        head_db, arms_json, headscale_db = stores
        backup_dir = tmp_path / "backups"
        script.create(str(head_db), str(arms_json), str(headscale_db), str(backup_dir), 3)
        script.create(str(head_db), str(arms_json), str(headscale_db), str(backup_dir), 3)
        assert len(list(backup_dir.glob("ccc-backup-*.tar.gz"))) == 1

    def test_retention_prunes_to_the_newest_n(self, script, stores, tmp_path):
        head_db, arms_json, headscale_db = stores
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        # Seed 5 older, distinct-day tarballs directly (a real run adds at most one per hour; a tight
        # test loop can't produce 5 distinct names since create() is hour-precision on purpose).
        old_names = [f"ccc-backup-2026072{day}T00Z.tar.gz" for day in range(5)]  # 20260720..24
        for name in old_names:
            (backup_dir / name).write_bytes(b"")
        script.create(str(head_db), str(arms_json), str(headscale_db), str(backup_dir), 3)
        kept = {p.name for p in backup_dir.glob("ccc-backup-*.tar.gz")}
        assert len(kept) == 3
        assert kept.isdisjoint(old_names[:3])  # the 3 oldest (day 20-22) are gone
        assert kept.issuperset(old_names[3:])  # the 2 newest seeded (day 23-24) survive alongside today's


class TestRestore:
    """What actually gets written back — the point of the feature, and the thing a bug here loses
    silently until the day it's needed."""

    def test_a_restore_repopulates_all_three_stores_intact(self, script, stores, tmp_path):
        head_db, arms_json, headscale_db = stores
        backup_dir = tmp_path / "backups"
        script.create(str(head_db), str(arms_json), str(headscale_db), str(backup_dir), 3)
        archive = next(backup_dir.glob("ccc-backup-*.tar.gz"))

        restored = tmp_path / "restored"
        restored.mkdir()
        new_head, new_arms, new_headscale = restored / "head.db", restored / "arms.json", restored / "headscale.sqlite"
        script.restore(str(archive), str(new_head), str(new_arms), str(new_headscale))

        assert sqlite3.connect(new_head).execute("select v from users").fetchone() == ("admin",)
        assert sqlite3.connect(new_headscale).execute("select v from nodes").fetchone() == ("arm1",)
        assert json.loads(new_arms.read_text()) == {"arm1": {"owner": "admin"}}

    def test_a_restore_leaves_no_stale_wal_sidecar(self, script, stores, tmp_path):
        """A leftover -wal from the file a restore just overwrote would replay against the NEW db the
        moment its service reopens it — silently reintroducing data the restore was meant to undo."""
        head_db, arms_json, headscale_db = stores
        backup_dir = tmp_path / "backups"
        script.create(str(head_db), str(arms_json), str(headscale_db), str(backup_dir), 3)
        archive = next(backup_dir.glob("ccc-backup-*.tar.gz"))

        restored = tmp_path / "restored"
        restored.mkdir()
        new_head = restored / "head.db"
        (restored / "arms.json").touch()
        (restored / "headscale.sqlite").touch()
        (restored / "head.db-wal").write_text("stale wal from before the restore")

        script.restore(str(archive), str(new_head), str(restored / "arms.json"), str(restored / "headscale.sqlite"))

        assert not (restored / "head.db-wal").exists()
