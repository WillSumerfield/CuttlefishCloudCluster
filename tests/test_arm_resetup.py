"""Setting an Arm up again, without a second **enrollment token**.

A re-setup is routine — new Features, a rebuilt Environment, a CCC upgrade — and none of it is
re-enrollment. But the token is a secret printed once and deliberately never logged, so needing it
again meant going back to the Admin for a fresh one, which revokes nothing and leaves a second live
preauth key lying around for its hour.

The consequences guarded here are both silent. Reusing an enrollment on a box that has fallen OFF
the mesh would send `mesh.up_arm` down its wipe-then-join path with no key to join with — destroying
the identity it was asked to preserve and leaving the box strictly worse off than before. And the
whole flag rests on `up_arm` short-circuiting for an already-joined box: if that ever stops, every
`--enrolled` run starts spending a key it does not have.
"""

import pytest
import typer

from cuttle import enroll, main, mesh, settings, ui

COORDINATOR = "https://head.example:8080"


@pytest.fixture
def enrolled(ccc_home, monkeypatch):
    """A box that redeemed a token once: the Arm block setup writes, and a live mesh under it."""
    settings.save({"arm": {"node_name": "willpc", "mesh_ip": "100.99.0.2",
                           "head_mesh_ip": "100.99.0.1", "ctld_addr": "100.99.0.1:6817"}})
    monkeypatch.setattr(mesh, "current_coordinator", lambda: COORDINATOR)
    monkeypatch.setattr(mesh, "joined_to", lambda c: c == COORDINATOR)


class TestAnEnrollmentIsRecoverableFromTheBoxItself:
    def test_it_carries_what_the_token_carried(self, enrolled):
        """The Head's mesh IP and this Arm's name come from settings; the coordinator from
        tailscaled's own state, which is the one fact settings never recorded."""
        assert enroll.enrollment_from_state() == {
            "coordinator": COORDINATOR, "mesh_ip": "100.99.0.1", "node_name": "willpc",
            "authkey": ""}

    def test_the_preauth_key_is_left_empty_rather_than_invented(self, enrolled):
        """It is the one thing that cannot be recovered — and the one thing this path never uses. A
        placeholder that looked like a key would be redeemed on any path that did use it."""
        assert enroll.enrollment_from_state()["authkey"] == ""

    def test_a_box_off_the_mesh_is_refused_before_anything_is_wiped(self, enrolled, monkeypatch):
        """The dangerous case. `up_arm` wipes the local identity BEFORE it discovers it has no key to
        re-join with, so proceeding here would cost the box the enrollment it asked to reuse."""
        monkeypatch.setattr(mesh, "joined_to", lambda c: False)
        with pytest.raises(typer.Exit):
            enroll.enrollment_from_state()

    def test_a_box_registered_against_a_different_coordinator_is_refused(self, enrolled,
                                                                        monkeypatch):
        """Its node key means nothing to the Head it is now being pointed at."""
        monkeypatch.setattr(mesh, "current_coordinator", lambda: "https://elsewhere:8080")
        with pytest.raises(typer.Exit):
            enroll.enrollment_from_state()

    def test_a_box_that_never_enrolled_is_refused(self, ccc_home, monkeypatch):
        monkeypatch.setattr(mesh, "current_coordinator", lambda: COORDINATOR)
        monkeypatch.setattr(mesh, "joined_to", lambda c: True)
        settings.save({})
        with pytest.raises(typer.Exit):
            enroll.enrollment_from_state()


class TestAnAlreadyJoinedBoxNeverSpendsAKey:
    """What `--enrolled` rests on. If `up_arm` stops short-circuiting, a re-setup wipes the mesh
    identity and tries to redeem the empty key above — which is the failure this whole flag exists to
    avoid, arriving by the back door."""

    def test_the_join_is_skipped_entirely(self, monkeypatch):
        monkeypatch.setattr(mesh, "ensure_headscale_trusted", lambda c: None)
        monkeypatch.setattr(mesh, "install_tailscale", lambda: None)
        monkeypatch.setattr(mesh, "start_ccc_tailscaled", lambda w: None)
        monkeypatch.setattr(mesh, "joined_to", lambda c: True)
        monkeypatch.setattr(mesh, "tailscale_ip", lambda: "100.99.0.2")
        monkeypatch.setattr(mesh, "_wipe_local_state",
                            lambda: pytest.fail("wiped an identity it was told to keep"))
        monkeypatch.setattr(mesh, "join_mesh",
                            lambda *a: pytest.fail("redeemed a key it does not have"))
        assert mesh.up_arm(COORDINATOR, "willpc", "", lambda *a: None) == "100.99.0.2"


class TestTheFlagsCannotContradictEachOther:
    """`--re-enroll` is `--enrolled`'s exact opposite — it throws the enrollment away. Silently
    picking one would either strand the box or waste a token."""

    def _setup(self, **kw):
        opts = {"head": False, "local": False, "arm": True, "client": False, "address": None,
                "token": None, "enrolled": False, "re_enroll": False}
        return main.setup(**{**opts, **kw})

    def test_a_token_and_a_reused_enrollment_are_refused(self):
        with pytest.raises(typer.BadParameter, match="drop --token"):
            self._setup(enrolled=True, token="abc")

    def test_reusing_and_re_enrolling_are_refused(self):
        with pytest.raises(typer.BadParameter, match="fresh token"):
            self._setup(enrolled=True, re_enroll=True)

    def test_neither_flag_names_both_ways_out(self):
        """A donor enrolling for the first time needs a token; one setting up again does not, and
        would otherwise go and ask for a secret nobody needed to mint."""
        with pytest.raises(typer.BadParameter) as caught:
            self._setup()
        assert "arm authorize" in str(caught.value) and "--enrolled" in str(caught.value)
