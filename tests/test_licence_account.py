"""The **Licence** account — an identity that exists only so its home is the Licence directory.

TurboActivate (vlearn) picks where to keep its activation state via
`getpwuid(getuid())->pw_dir`, ignoring `$HOME` entirely. So the only way to control where a
licence lands is the uid that performs the activation — hence an account whose sole purpose is
its passwd home field.

Hand-copying the vendor's files out of a human's home doesn't work: they're named from an
obfuscated per-release token in neither TurboActivate.dat nor the vlearn wheels, so it can't be
derived, and a version bump renames them all while the bind guard (which only checks the
directory exists) keeps succeeding. See temp/FEATURES_LICENCE.md.
"""

import pytest

from cuttle import config, setup, teardown


@pytest.fixture
def commands(monkeypatch):
    """Every `run` the provisioning makes, as plain argv lists. These are useradd/usermod/install —
    nothing that may actually execute in a test."""
    calls = []

    class Result:
        def __init__(self, stdout=""):
            self.stdout = stdout

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        # `ensure_guise_group` reads the gid back and asserts it, so the fake has to answer as getent
        # would on a box where groupadd worked.
        if cmd[:2] == ["getent", "group"]:
            return Result(f"{config.GUISE_GROUP}:x:{config.GUISE_GROUP_GID}:\n")
        return Result()

    monkeypatch.setattr(setup, "run", fake_run)
    monkeypatch.setattr(teardown, "run", fake_run)
    return calls


def _argv(calls, program: str) -> list[str]:
    for c in calls:
        if c and c[0] == program:
            return c
    raise AssertionError(f"no {program!r} in {calls}")


class TestTheAccountCarriesTheDirectory:
    def test_the_licence_directory_becomes_the_accounts_home(self, commands):
        """The entire point. `--home-dir` is what getpwuid reports, and getpwuid is what the vendor's
        library reads — so this argument is the mechanism, not a convenience."""
        setup.provision_licence_home("/opt/ccc/licence")
        argv = _argv(commands, "useradd")
        assert "--home-dir" in argv
        assert argv[argv.index("--home-dir") + 1] == "/opt/ccc/licence"

    def test_ccc_creates_the_directory_rather_than_the_operator(self, commands):
        """The operator is asked to NAME a directory, not to make one. `--create-home` is why that
        prompt can honestly say the path need not exist yet."""
        setup.provision_licence_home("/opt/ccc/licence")
        assert "--create-home" in _argv(commands, "useradd")

    def test_the_home_is_re_pointed_on_a_re_setup(self, commands):
        """useradd is non-fatal when the account exists, so a re-setup that named a DIFFERENT directory
        would otherwise leave the account activating into the old one — silently, since the new
        directory would exist and pass the bind guard while holding nothing."""
        setup.provision_licence_home("/srv/licence")
        argv = _argv(commands, "usermod")
        assert argv[argv.index("--home") + 1] == "/srv/licence"

    def test_it_never_logs_in(self, commands):
        """It is a passwd entry, not a person. Nothing should ever run a shell as it."""
        setup.provision_licence_home("/opt/ccc/licence")
        assert "/usr/sbin/nologin" in _argv(commands, "useradd")


class TestTheUidIsDeliberatelyNotPinned:
    def test_it_cannot_shadow_a_guise(self, commands):
        """Unlike the Mantle, which MUST be one uid cluster-wide because Slurm setuid()s to the
        submitting uid on every node. This account never runs a Dive and never leaves its box, so it
        takes a locally-allocated system uid — below 1000, where the prolog (which only ever acts on
        the 60000+ Guise range) can never mistake it for a Guise."""
        setup.provision_licence_home("/opt/ccc/licence")
        argv = _argv(commands, "useradd")
        assert "--system" in argv
        assert "--uid" not in argv


class TestASpawnCanWriteToIt:
    def test_the_guise_group_owns_it(self, commands):
        """A Dive runs as a **Guise** — a different uid — so group ownership is the only way it
        reaches this directory at all."""
        setup.provision_licence_home("/opt/ccc/licence")
        argv = _argv(commands, "install")
        assert argv[argv.index("-g") + 1] == config.GUISE_GROUP

    def test_it_is_group_writable_and_setgid(self, commands):
        """Writable because the activation record is rewritten on periodic online re-validation, so a
        read-only licence eventually goes stale. setgid so what re-validation writes inherits the
        shared group instead of the Guise's own private one, which the next Guise could not read."""
        setup.provision_licence_home("/opt/ccc/licence")
        argv = _argv(commands, "install")
        assert argv[argv.index("-m") + 1] == "2770"

    def test_the_shared_group_is_ensured_first(self, commands):
        """On an Arm the group may not exist yet — the prolog creates it lazily, when the first Dive
        lands, which is after setup. Chowning to a group that does not exist would fail the run."""
        setup.provision_licence_home("/opt/ccc/licence")
        assert commands[0][0] == "groupadd"
        assert config.GUISE_GROUP in commands[0]


class TestTeardownKeepsTheActivation:
    def test_the_account_goes(self, commands):
        teardown.remove_licence_account()
        assert _argv(commands, "userdel")[-1] == config.LICENCE_USER

    def test_the_activation_state_does_not(self, commands):
        """`--remove` would delete the home, and the home is the licence. Teardown+setup is the normal
        redeploy on these boxes, so a teardown that spent one of a finite number of activations every
        time would make the routine path the expensive one."""
        teardown.remove_licence_account()
        assert "--remove" not in _argv(commands, "userdel")
        assert "-r" not in _argv(commands, "userdel")

    def test_every_teardown_path_removes_it_before_the_group_it_belongs_to(self):
        """groupdel refuses a group that still has members, so this account must go before
        `remove_guises` drops GUISE_GROUP. Enforced by call ORDER at three separate sites rather than
        by putting the call inside remove_guises, which would make that function delete a non-Guise —
        so the ordering is checked in the source, where the mistake would actually be made."""
        import inspect
        src = inspect.getsource(teardown)
        for site in ("remove_licence_account()", "remove_guises()"):
            assert src.count(site) >= 3, f"{site} not at every teardown path"
        # Each removal must be preceded by the licence account's, nowhere further than the line before.
        lines = [ln.strip() for ln in src.splitlines()]
        for i, ln in enumerate(lines):
            if ln.startswith("remove_guises()"):
                assert lines[i - 1].startswith("remove_licence_account()"), \
                    f"remove_guises() at source line {i + 1} is not preceded by remove_licence_account()"
