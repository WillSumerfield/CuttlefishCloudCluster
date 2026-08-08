"""**Guises** — a per-User Unix identity, and the end of every Spawn running as the Mantle.

The failures guarded here are the quiet ones. A uid handed out twice does not raise; it silently gives
one User another's leftover files on every Arm they ever ran on. A `sudo -u` that resolves to the wrong
account does not raise either; it either fails a Spawn nobody can explain, or acts on a job that is not
ours. Both are exactly what Guises exist to prevent, so both are pinned here.

See temp/spawn-isolation.md.
"""

import stat
from types import SimpleNamespace

import pytest

from ccc_head_api import db, guises
from cuttle import config, setup


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    db.ensure_schema()
    with db.connect() as c:
        yield c


def _user(conn, name: str, is_admin: bool = False) -> int:
    db.add_user(conn, name, is_admin=is_admin)
    return db.get_user(conn, name)["id"]


class TestTheName:
    def test_it_is_formed_from_the_uid_alone(self):
        """The Slurm prolog creates this account on a donor's box with `SLURM_JOB_UID` as its only
        input — no network, no head.db. A name derived from anything else could not be formed there."""
        assert guises.name(60002) == "ccc-u60002"
        assert guises.name(guises.CLUSTER_UID) == "ccc-u60001"

    def test_it_cannot_collide_with_a_ccc_username(self):
        """A User called `alice` and a User called `ccc-u60002` both get uid-derived names, so no
        username regex is needed and no donor's own account can be shadowed."""
        assert guises.name(60002).startswith(guises.PREFIX)


class TestAllocation:
    def test_every_user_gets_one_at_creation(self, conn):
        alice = _user(conn, "alice")
        assert db.guise_uid(conn, alice) == guises.UID_BASE

    def test_an_admin_gets_one_too(self, conn):
        """"A compromised laptop is realistic" is not a claim about non-admins. An Admin's payload must
        not be able to write head.db either — so an Admin is a User here like any other, even though
        they are outside the Karma economy everywhere else."""
        admin = _user(conn, "root-ish", is_admin=True)
        assert db.guise_uid(conn, admin) is not None

    def test_uids_are_monotonic(self, conn):
        uids = [db.guise_uid(conn, _user(conn, n)) for n in ("a", "b", "c")]
        assert uids == sorted(uids) == list(range(guises.UID_BASE, guises.UID_BASE + 3))

    def test_a_deleted_users_uid_is_never_reissued(self, conn):
        """The whole reason this is its own table. `remove_user` HARD-deletes, so an allocator keyed
        off the users table would hand the next User a dead one's uid — and with it every file they
        left behind on every Arm. That is precisely the failure a per-User uid exists to prevent."""
        gone = db.guise_uid(conn, _user(conn, "departed"))
        db.remove_user(conn, "departed")
        fresh = db.guise_uid(conn, _user(conn, "newcomer"))
        assert fresh != gone and fresh > gone

    def test_a_removed_user_leaves_the_uid_retired(self, conn):
        """The tombstone: the row survives the User, keyed by uid, with user_id NULL."""
        uid = db.guise_uid(conn, _user(conn, "departed"))
        db.remove_user(conn, "departed")
        row = conn.execute("SELECT * FROM guises WHERE uid=?", (uid,)).fetchone()
        assert row is not None and row["user_id"] is None

    def test_allocation_is_idempotent(self, conn):
        """`setup --head` re-runs over an already-provisioned Head, and every submit asks again."""
        alice = _user(conn, "alice")
        assert db.allocate_guise(conn, alice) == db.allocate_guise(conn, alice)

    def test_the_reserved_cluster_uid_is_never_allocated_to_a_user(self, conn):
        """60001 belongs to the enrollment benchmark — the one Spawn no User submits."""
        uids = [db.guise_uid(conn, _user(conn, n)) for n in ("a", "b", "c")]
        assert guises.CLUSTER_UID not in uids
        assert all(u > guises.CLUSTER_UID for u in uids)

    def test_the_mantles_uid_is_below_the_block(self, conn):
        """60000 is the Mantle itself. Allocating it would hand a User the account that owns head.db."""
        assert guises.UID_BASE > 60000

    def test_existing_users_are_backfilled_on_upgrade(self, monkeypatch, tmp_path):
        """A deployed Head has Users who predate this table. The migration allocates for all of them,
        in `id` order, so nobody is left without one and no separate backfill step exists to forget."""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        with db.connect() as c:
            # A v9 database: the ladder up to (not including) guises, then Users, then the last step.
            for step in db._MIGRATIONS[:9]:
                c.executescript(step)
            c.execute("PRAGMA user_version=9")
            for name in ("alice", "bob"):
                c.execute("INSERT INTO users (username, pw_hash) VALUES (?, 'x')", (name,))
            c.commit()
            db.init_schema(c)
            rows = {r["username"]: r["uid"] for r in db.live_guises(c)}
        assert rows == {"alice": guises.UID_BASE, "bob": guises.UID_BASE + 1}

    def test_live_guises_excludes_tombstones(self, conn):
        """It is what the Head provisions ACCOUNTS for — a retired uid has nobody to make one for."""
        _user(conn, "alice")
        db.remove_user(conn, "alice")
        _user(conn, "bob")
        assert [r["username"] for r in db.live_guises(conn)] == ["bob"]


class TestActingAsTheOwner:
    """Every Slurm operation CCC performs on a Spawn is one the job's OWNER may perform with no
    privilege. That was free while the Head owned every job; now it has to ask sudo to become them."""

    def test_it_becomes_the_guise(self):
        assert guises.as_owner(["scancel", "5"], "ccc-u60002") == \
            ["sudo", "-n", "-u", "ccc-u60002", "scancel", "5"]

    def test_it_never_prompts(self):
        """This fires inside a request handler and inside the Karma tick — neither has a tty, so a
        sudo that asks for a password would HANG rather than fail."""
        assert "-n" in guises.as_owner(["scancel", "5"], "ccc-u60002")

    def test_a_mantle_owned_spawn_runs_plain(self):
        """The transitional path: a Spawn queued before Guises existed is owned by `ccc`, and the
        Head is still `ccc`, so it needs no sudo at all. Same for the enrollment benchmark,
        which predates this too."""
        assert guises.as_owner(["scancel", "5"], "ccc") == ["scancel", "5"]

    def test_an_unknown_owner_runs_plain(self):
        """squeue has already forgotten the job. The command is a no-op either way — but guessing an
        account to become would be worse than not trying."""
        assert guises.as_owner(["scancel", "5"], None) == ["scancel", "5"]

    def test_it_does_not_mutate_the_caller_argv(self):
        argv = ["scancel", "5"]
        guises.as_owner(argv, "ccc-u60002")
        assert argv == ["scancel", "5"]


class TestTheSlurmOpsBecomeTheOwner:
    """Every op the Head performs on a live Spawn. Each is zero-privilege *for the owner* and refused
    for anyone else, so getting the account wrong does not fail loudly — it fails as a Spawn that
    cannot be cancelled, or a Karma ranking that silently stops reaching the queue."""

    @pytest.fixture
    def ran(self, monkeypatch):
        from ccc_head_api import slurm
        calls = []
        monkeypatch.setattr(slurm.subprocess, "run",
                            lambda argv, **kw: calls.append(list(argv)) or
                            type("R", (), {"stdout": "ccc-u60002\n", "returncode": 0})())
        return calls

    def test_the_tick_renices_as_the_owner(self, ran):
        from ccc_head_api import slurm
        slurm.set_nice(7, 100, "ccc-u60002")
        assert ran[0][:4] == ["sudo", "-n", "-u", "ccc-u60002"]

    def test_cancel_becomes_the_owner(self, ran):
        from ccc_head_api import slurm
        slurm.cancel(7)
        assert ran[-1] == ["sudo", "-n", "-u", "ccc-u60002", "scancel", "7"]

    def test_signal_becomes_the_owner(self, ran):
        """A Retract's TERM nudge and the tick's force-kill both come through here."""
        from ccc_head_api import slurm
        slurm.signal(7, "TERM")
        assert ran[-1] == ["sudo", "-n", "-u", "ccc-u60002", "scancel", "--signal=TERM", "7"]

    def test_a_departing_donor_gets_their_box_back(self, monkeypatch):
        """`teardown --arm` on a donor's machine. The accounts are named after uids, so the GECOS tag
        is the ONLY thing distinguishing them from the box's own users — which is the whole reason it
        is stamped. `userdel` alone, never `--remove`: a Guise is `useradd -M`, so there is no home to
        delete and nothing of the donor's can be caught in the blast radius."""
        from cuttle import config, teardown
        passwd = (f"root:x:0:0:root:/root:/bin/bash\n"
                  f"willsumerfield:x:1000:1000:Will:/home/will:/bin/bash\n"
                  f"ccc:x:60000:60000:CCC Mantle:/nonexistent:/usr/sbin/nologin\n"
                  f"ccc-u60002:x:60002:60002:{config.GUISE_GECOS}:/nonexistent:/usr/sbin/nologin\n"
                  f"ccc-u60007:x:60007:60007:{config.GUISE_GECOS}:/nonexistent:/usr/sbin/nologin\n"
                  # Same prefix, NOT ours — a donor really could have named an account this.
                  f"ccc-u1:x:1500:1500:someone's own:/home/x:/bin/bash\n")
        calls = []
        monkeypatch.setattr(teardown, "run", lambda argv, **kw: calls.append(list(argv)) or
                            type("R", (), {"stdout": passwd, "returncode": 0})())
        teardown.remove_guises()
        deleted = [c[1] for c in calls if c[0] == "userdel"]
        assert deleted == ["ccc-u60002", "ccc-u60007"]
        assert not any("--remove" in c or "-r" in c for c in calls)

    def test_the_owner_comes_from_squeue_not_the_ledger(self, ran):
        """squeue is what `sudo -u` has to MATCH, so it's ground truth by construction. It also gets
        three cases right the Attribution row gets wrong: a Spawn queued before Guises existed
        (row present, owned by the Mantle), the enrollment benchmark (no row at all), and a job
        that has already ended."""
        from ccc_head_api import slurm
        slurm.cancel(7)
        assert ran[0][:3] == ["squeue", "-h", "-j"]


class TestTheStoreIsNotReadableByAGuise:
    """A Guise must not be able to read `head.db`.

    sqlite creates the db at 0644 by default, and HEAD_STATE_DIR is deliberately 0711 (traverse,
    never list) on the assumption the file itself is 0600 — docstrings claimed that mode but
    nothing enforced it. Without this, any Guise could read every password hash, session token,
    and admin flag on the cluster."""

    def test_the_store_is_0600_from_the_moment_it_exists(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.connect()
        assert stat.S_IMODE((tmp_path / "head.db").stat().st_mode) == 0o600

    def test_the_wal_sidecars_are_restricted_too(self, monkeypatch, tmp_path):
        """They hold the writes the store has not checkpointed yet — a session token minted a moment
        ago is in the WAL, not in head.db. sqlite creates them on the first WRITE rather than at
        `connect`, so the open that restricts them is always a later one; on a live Head there is
        always another (every API request, and the tick every 30 seconds)."""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        held = db.connect()  # stands in for the API service's long-lived connection
        try:
            db.init_schema(held)  # a write: the sidecars appear now, at sqlite's own 0644
            db.connect()          # any later open restricts them
            sidecars = sorted(p for p in tmp_path.iterdir() if p.name.startswith("head.db-"))
            assert [p.name for p in sidecars] == ["head.db-shm", "head.db-wal"]
            for p in sidecars:
                assert stat.S_IMODE(p.stat().st_mode) == 0o600, p.name
        finally:
            held.close()


class TestTheTlsKeysStayMantleOnly:
    """`head.key` is the private half of the cert the Head serves; `ccc-ca.key` signs it. A Guise that
    can read either can impersonate the Head — the second one to *every* name in the cluster, and to
    every box that installed the CA, which is worse.

    The generating branch always chmod's them correctly, but the CA branch runs once in a Head's life
    (a new CA un-trusts the whole fleet) and teardown clears it only under `--all`. So the modes have
    to be re-asserted on the keep path too, or a box that drifts stays drifted forever.
    """

    def _install(self, monkeypatch, tmp_path, *, leaf_sans: str | None,
                 chains: bool = True) -> list[list[str]]:
        """Run install_tls with every `run` captured. `leaf_sans` None means no leaf on disk yet;
        otherwise it is what `openssl x509 -ext subjectAltName` will be made to report."""
        calls: list[list[str]] = []

        def fake_run(argv, **kw):
            calls.append(argv)
            if argv[:2] == ["openssl", "x509"]:
                return SimpleNamespace(stdout=leaf_sans or "", returncode=0)
            if argv[:2] == ["openssl", "verify"]:
                return SimpleNamespace(stdout="", returncode=0 if chains else 2)
            return SimpleNamespace(stdout="", returncode=0)

        monkeypatch.setattr(setup, "run", fake_run)
        monkeypatch.setattr(setup, "sh", lambda script, **kw: calls.append(["sh", script]))
        monkeypatch.setattr(config, "CCC_TLS_DIR", tmp_path)
        monkeypatch.setattr(config, "HEAD_CERT", tmp_path / "head.crt")
        monkeypatch.setattr(config, "HEAD_KEY", tmp_path / "head.key")
        monkeypatch.setattr(config, "HEAD_CA_CERT", tmp_path / "ccc-ca.crt")
        monkeypatch.setattr(config, "HEAD_CA_KEY", tmp_path / "ccc-ca.key")
        monkeypatch.setattr(setup.system, "hostname", lambda: "head")
        monkeypatch.setattr(setup.system, "primary_ip", lambda: "192.168.1.2")
        (tmp_path / "ccc-ca.crt").write_text("the authority every box in this cluster trusts")
        if leaf_sans is not None:
            (tmp_path / "head.crt").write_text("a serving cert")
        setup.install_tls("100.99.0.1")
        return calls

    def _covering_sans(self) -> str:
        # What openssl actually prints, IP SANs spelled its way — the exact shape _cert_sans has to
        # normalise, so a covering cert here proves the normalisation as well as the keep path.
        return ("X509v3 Subject Alternative Name:\n"
                f"    DNS:{config.DOMAIN}, DNS:head, DNS:localhost, "
                "IP Address:192.168.1.2, IP Address:100.99.0.1, IP Address:127.0.0.1\n")

    def test_a_rerun_re_asserts_the_modes_on_a_cert_it_keeps(self, monkeypatch, tmp_path):
        calls = self._install(monkeypatch, tmp_path, leaf_sans=self._covering_sans())
        assert not [c for c in calls if c[0] == "sh"], "a covering cert must not be reissued"
        assert not [c for c in calls if c[:2] == ["openssl", "req"]], "the CA must never be re-minted"
        for key in ("head.key", "ccc-ca.key"):
            assert ["chmod", "0600", str(tmp_path / key)] in calls
            assert ["chown", f"{config.HEAD_API_USER}:{config.HEAD_API_USER}",
                    str(tmp_path / key)] in calls

    def test_freshly_minted_keys_get_them_too(self, monkeypatch, tmp_path):
        calls = self._install(monkeypatch, tmp_path, leaf_sans=None)
        assert [c for c in calls if c[0] == "sh"], "no leaf was issued"
        for name in ("head.key", "ccc-ca.key"):
            assert ["chmod", "0600", str(tmp_path / name)] in calls
        for name in ("head.crt", "ccc-ca.crt"):
            assert ["chmod", "0644", str(tmp_path / name)] in calls


class TestTheLeafIsReissuedWhenItStopsCovering:
    """The leaf is cheap and the CA is not: clients pin the CA, so reissuing under it costs them
    nothing. That is what lets an already-deployed Head gain the Cluster name without a teardown — and
    what self-heals a changed LAN IP, which used to leave TLS quietly broken with no path back."""

    def _install(self, monkeypatch, tmp_path, **kw):
        return TestTheTlsKeysStayMantleOnly()._install(monkeypatch, tmp_path, **kw)

    def test_a_cert_missing_the_cluster_name_is_reissued(self, monkeypatch, tmp_path):
        # Exactly what a Head set up before this change has: correct in every way but the new name.
        before = ("X509v3 Subject Alternative Name:\n"
                  "    DNS:head, DNS:localhost, IP Address:192.168.1.2, "
                  "IP Address:100.99.0.1, IP Address:127.0.0.1\n")
        calls = self._install(monkeypatch, tmp_path, leaf_sans=before)
        issued = [c for c in calls if c[0] == "sh"]
        assert issued, "a cert that does not cover the Cluster name must be reissued"
        assert f"DNS:{config.DOMAIN}" in issued[0][1]
        assert not [c for c in calls if c[:2] == ["openssl", "req"]], "the CA must survive a reissue"

    def test_a_cert_the_ca_did_not_sign_is_reissued(self, monkeypatch, tmp_path):
        # SANs are perfect, but it is the old self-signed cert — nothing that trusts the CA accepts it.
        calls = self._install(monkeypatch, tmp_path,
                              leaf_sans=TestTheTlsKeysStayMantleOnly()._covering_sans(),
                              chains=False)
        assert [c for c in calls if c[0] == "sh"], "a cert outside the CA's chain must be reissued"
