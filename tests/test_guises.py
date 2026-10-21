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
        monkeypatch.setattr(teardown.time, "sleep", lambda s: None)
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


class TestAGuiseIsAlwaysInTheSharedGroup:
    """GUISE_GROUP is the Runas list on the Head's one sudoers rule
    (headapi._SUDOERS_CCC_SLURM: `ccc ALL=(%ccc-guises) NOPASSWD: sbatch, scancel, scontrol`). A Guise
    outside it cannot be sudo'd into at all, so every Spawn that User owns dies at submission with
    "sudo: a password is required" — naming a command that is not the problem, on a box where every
    other User works fine.
    """

    def calls(self, monkeypatch, uid: int = 60002) -> list[list[str]]:
        from cuttle import auth
        seen = []
        monkeypatch.setattr(auth, "run", lambda argv, **kw: seen.append(list(argv)) or
                            type("R", (), {"returncode": 0, "stdout": ""})())
        auth.provision_guise(uid)
        return seen

    def test_a_new_account_is_created_in_it(self, monkeypatch):
        useradd = [c for c in self.calls(monkeypatch) if c[0] == "useradd"][0]
        assert useradd[useradd.index("--groups") + 1] == config.GUISE_GROUP

    def test_the_membership_is_re_asserted_even_so(self, monkeypatch):
        """`useradd --groups` applies only when useradd CREATES the account. On a re-run — or on an
        account that outlived a teardown because a Spawn was still running as it — useradd does
        nothing and exits non-zero, which provision_guise deliberately tolerates. Without a separate
        usermod the Guise never rejoins the group that a fresh teardown+setup just re-made."""
        usermod = [c for c in self.calls(monkeypatch) if c[0] == "usermod"]
        assert usermod == [["usermod", "--append", "--groups", config.GUISE_GROUP, "ccc-u60002"]]

    def test_it_appends_rather_than_replacing(self, monkeypatch):
        """`--append`, so this never becomes the thing that strips a Guise of some other membership a
        future version of CCC (or a donor's own box) gave it."""
        usermod = [c for c in self.calls(monkeypatch) if c[0] == "usermod"][0]
        assert "--append" in usermod

    def test_it_comes_after_the_group_exists(self, monkeypatch):
        calls = self.calls(monkeypatch)
        names = [c[0] for c in calls]
        assert names.index("groupadd") < names.index("usermod")

    def test_a_failure_here_stops_setup(self, monkeypatch):
        """The one call on this path that is NOT tolerated. Every other step is check=False because
        "already there" is the normal case; a Guise that cannot be put in the group is not a re-run,
        it is a User who will be unable to run anything, and the failure has to land here rather than
        at their first Spawn."""
        from cuttle import auth
        import subprocess as sp

        def fake(argv, **kw):
            if argv[0] == "usermod":
                raise sp.CalledProcessError(1, argv)
            return type("R", (), {"returncode": 0, "stdout": ""})()

        monkeypatch.setattr(auth, "run", fake)
        with pytest.raises(sp.CalledProcessError):
            auth.provision_guise(60002)

    def test_the_cluster_guise_gets_one_too(self, monkeypatch):
        """The enrollment benchmark's Guise belongs to no User but is submitted the same way, so it
        needs the same grant."""
        usermod = [c for c in self.calls(monkeypatch, config.GUISE_CLUSTER_UID) if c[0] == "usermod"]
        assert usermod[0][-1] == config.guise_name(config.GUISE_CLUSTER_UID)


class TestATeardownLeavesNoSpawnRunning:
    """Stopping slurmd does NOT end a running batch step — its process tree detaches and survives, so
    a Tide Pool's sshd was still listening on its port hours after the cluster was torn down. That
    orphan then blocked `userdel` on its **Guise**, whose failure is swallowed, and the next setup
    found the account already there and left it out of the re-created GUISE_GROUP. One silent failure
    at teardown surfacing days later as one User who cannot submit.
    """

    def teardown_calls(self, monkeypatch, passwd: str = "", killed: bool = True) -> list[list[str]]:
        from cuttle import teardown
        calls = []
        monkeypatch.setattr(teardown, "run", lambda argv, **kw: calls.append(list(argv)) or
                            type("R", (), {"stdout": passwd,
                                           "returncode": 0 if killed else 1})())
        monkeypatch.setattr(teardown.time, "sleep", lambda s: None)
        return calls

    def _passwd(self, *names: str) -> str:
        return "".join(f"{n}:x:60002:60002:{config.GUISE_GECOS}:/nonexistent:/usr/sbin/nologin\n"
                       for n in names)

    def test_the_queue_is_emptied_before_the_controller_stops(self, monkeypatch):
        """scancel asks slurmctld, so once stop_daemons has run there is nothing left to ask."""
        from cuttle import teardown
        calls = self.teardown_calls(monkeypatch)
        teardown.cancel_spawns()
        assert calls == [["scancel", "--all"]]

    def test_stragglers_are_signalled_before_userdel(self, monkeypatch):
        """`userdel` refuses an account with a live process, and every call here is check=False — so
        the account survives the teardown in silence unless nothing is running as it."""
        from cuttle import teardown
        calls = self.teardown_calls(monkeypatch, self._passwd("ccc-u60002"))
        teardown.remove_guises()
        names = [c[0] for c in calls]
        assert names.index("pkill") < names.index("userdel")

    def test_term_comes_before_kill(self, monkeypatch):
        """apptainer's own children (squashfuse, fuse-overlayfs) need a moment to unmount the Pod;
        killed outright they leave mounts behind under /var/lib/apptainer."""
        from cuttle import teardown
        calls = self.teardown_calls(monkeypatch, self._passwd("ccc-u60002"))
        teardown.end_guise_processes()
        assert [c[1] for c in calls if c[0] == "pkill"] == ["-TERM", "-KILL"]

    def test_every_guise_is_swept_not_just_the_first(self, monkeypatch):
        from cuttle import teardown
        calls = self.teardown_calls(monkeypatch, self._passwd("ccc-u60002", "ccc-u60007"))
        teardown.end_guise_processes()
        assert [c[3] for c in calls if c[0] == "pkill" and c[1] == "-TERM"] == \
            ["ccc-u60002", "ccc-u60007"]

    def test_a_clean_box_does_not_wait(self, monkeypatch):
        """pkill exits non-zero when it signalled nothing, which is the normal case — a cleanly
        stopped cluster. An unconditional grace period would spend it on every teardown."""
        from cuttle import teardown
        slept = []
        calls = self.teardown_calls(monkeypatch, self._passwd("ccc-u60002"), killed=False)
        monkeypatch.setattr(teardown.time, "sleep", lambda s: slept.append(s))
        teardown.end_guise_processes()
        assert slept == []
        assert not any(c[1] == "-KILL" for c in calls if c[0] == "pkill")

    def test_a_box_with_no_guises_signals_nothing(self, monkeypatch):
        from cuttle import teardown
        calls = self.teardown_calls(monkeypatch, "root:x:0:0:root:/root:/bin/bash\n")
        teardown.end_guise_processes()
        assert not any(c[0] == "pkill" for c in calls)

    def test_the_guise_sweep_is_local_not_cluster_wide(self, monkeypatch):
        """This is the whole of an Arm teardown's job-ending, and it must stay that way: scancel asks
        the CONTROLLER, so a donor taking their own box back would end Spawns running on everyone
        else's machines too. It ends the ones running here; the Head marks the node down like any
        other that went away."""
        from cuttle import teardown
        calls = self.teardown_calls(monkeypatch, self._passwd("ccc-u60002"))
        teardown.remove_guises()
        assert any(c[0] == "pkill" for c in calls)
        assert not any(c[0] == "scancel" for c in calls)


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


class TestTheOperatorNeedNotHaveAGroupOfTheirOwn:
    """A user-private group (`whs24:whs24`) is an Ubuntu default, not a rule. An operator whose
    account comes from a directory — LDAP on a university box, say — has a shared primary group
    (`ldapusers`) and NO group of their own name, so `install -g <username>` is `invalid group` and
    setup dies two thirds of the way in, at the Pod image, on a Head where everything else worked.
    """

    def calls(self, monkeypatch, tmp_path) -> list[list[str]]:
        seen = []
        monkeypatch.setattr(setup, "run", lambda argv, **kw: seen.append([str(a) for a in argv]))
        monkeypatch.setattr(setup.getpass, "getuser", lambda: "whs24")
        monkeypatch.setattr(setup.os, "getgid", lambda: 1002)   # ldapusers; no `whs24` group exists
        monkeypatch.setattr(setup, "ensure_subid_range", lambda: None)   # pinned by its own class
        sif = tmp_path / "base.sif"
        sif.touch()                                             # skip the build; the mkdir is the test
        monkeypatch.setattr(config, "CCC_SIF", sif)
        setup.build_base_sif()
        return seen

    def test_the_image_dir_is_grouped_by_gid_not_by_username(self, monkeypatch, tmp_path):
        mkdir = [c for c in self.calls(monkeypatch, tmp_path) if c[:2] == ["install", "-d"]][0]
        assert mkdir[mkdir.index("-o") + 1] == "whs24"           # owner is still the operator
        assert mkdir[mkdir.index("-g") + 1] == "1002"            # group is resolved, not guessed


class TestAnOperatorWithNoSubordinateIds:
    """`apptainer build` runs unprivileged and maps its fakeroot namespace out of the operator's
    /etc/subuid range. `adduser` writes one for a LOCAL account only, so a directory-managed operator
    has none and the build dies on `setgroups: Permission denied` — 20 minutes in, naming neither
    subuid nor the user. Every node builds this image, so this is not a Head-only failure.
    """

    def run(self, monkeypatch, tmp_path, subuid: str = "", subgid: str = "") -> list[str]:
        (tmp_path / "subuid").write_text(subuid)
        (tmp_path / "subgid").write_text(subgid)
        monkeypatch.setattr(config, "SUBUID_FILE", tmp_path / "subuid")
        monkeypatch.setattr(config, "SUBGID_FILE", tmp_path / "subgid")
        monkeypatch.setattr(setup.getpass, "getuser", lambda: "whs24")
        monkeypatch.setattr(setup.ui, "say", lambda *a, **kw: None)
        seen = []
        monkeypatch.setattr(setup, "sh", lambda script, **kw: seen.append(script))
        setup.ensure_subid_range()
        return seen

    def test_a_range_is_granted_when_there_is_none(self, monkeypatch, tmp_path):
        appended = self.run(monkeypatch, tmp_path)
        assert len(appended) == 2, "uid and gid ranges are granted together or not at all"
        assert all(f"whs24:{config.SUBID_BASE}:{config.SUBID_COUNT}" in a for a in appended)

    def test_an_existing_range_is_never_moved(self, monkeypatch, tmp_path):
        """A range already handed out is one some namespace may already own files under."""
        line = "whs24:100000:65536"
        assert self.run(monkeypatch, tmp_path, subuid=line + "\n", subgid=line + "\n") == []

    def test_a_new_range_starts_past_every_allocated_one(self, monkeypatch, tmp_path):
        """Two users sharing subids means one can act as the other inside a namespace."""
        appended = self.run(monkeypatch, tmp_path, subuid="ubuntu:100000000:65536\n")
        assert all(f"whs24:{100_000_000 + 65_536}:{config.SUBID_COUNT}" in a for a in appended)

    def test_the_uid_range_is_filled_in_even_if_only_the_gid_one_is_missing(self, monkeypatch,
                                                                           tmp_path):
        """Half a grant is no grant: newuidmap and newgidmap both have to resolve."""
        appended = self.run(monkeypatch, tmp_path, subuid="whs24:100000:65536\n")
        assert len(appended) == 1, "only the file actually missing the operator is appended to"
        assert str(config.SUBGID_FILE) in appended[0] and "whs24" in appended[0]
