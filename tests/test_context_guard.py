"""A command on the wrong box must refuse before it touches anything — not fail confusingly deep in
its body. `cuttle user add bob` on an Arm used to die on the missing API venv (a stack of internal
noise); a USER command with no session used to fire a tokenless request and come back a bare 401.

**Consequences, not a table.** Each test names something that outlives the screen: a Head-only
command that runs its admin mutation on a box that has no business running it, a submitter command
that reaches the Head with no credential, or — the other direction — the guard over-reaching and
blocking the very box where the command belongs (a Head operator locked out of `user add`, an Arm
owner who can no longer retract their own card from their laptop).
"""

import pytest
from typer.testing import CliRunner

from cuttle import config, menu
from cuttle.context import Ctx, satisfied
from cuttle.main import app


@pytest.fixture
def box(monkeypatch, tmp_path):
    """Fabricate what box this is, without a real role on disk. Returns a setter taking the four
    facts the guard reads; the session token is a file that does or doesn't exist."""
    session = tmp_path / "session"
    monkeypatch.setattr(config, "SESSION_FILE", session)

    def be(*, head=False, arm=False, pointed=False, logged_in=False):
        monkeypatch.setattr(menu, "is_head", lambda: head)
        monkeypatch.setattr(menu, "is_arm", lambda: arm)
        monkeypatch.setattr(menu, "is_pointed", lambda: pointed or logged_in)
        if logged_in:
            session.write_text("tok")
        elif session.exists():
            session.unlink()

    return be


@pytest.fixture
def ran(monkeypatch):
    """Record whether a command body reached the code it delegates to. Patches the module seam each
    command calls, so 'refused' means 'never got there'."""
    seen = {}

    def watch(modname, attr, key):
        import importlib
        mod = importlib.import_module(f"cuttle.{modname}")
        monkeypatch.setattr(mod, attr, lambda *a, **k: seen.__setitem__(key, (a, k)))

    watch("auth", "user_add", "user_add")
    watch("auth", "login", "login")
    watch("queue", "queue", "queue")
    watch("enroll", "retract_arm", "retract")
    return seen


runner = CliRunner()


# --- the guard refuses the wrong box, and runs nothing -----------------------------

def test_head_only_command_on_an_arm_runs_nothing(box, ran):
    """`user add` mutates the auth store as the ccc service account — on an Arm there is no such
    store. The guard must stop it before it tries, or the operator sees an internal failure and
    can't tell a wrong-box mistake from a real bug."""
    box(arm=True)
    r = runner.invoke(app, ["user", "add", "bob"])
    assert r.exit_code != 0
    assert "Head" in r.output
    assert "user_add" not in ran            # body never reached

def test_submitter_command_without_a_session_runs_nothing(box, ran):
    """`queue` needs a session bearer; with none, firing the request returns a bare 401 that reads
    like the Head rejected a real login. Refuse locally with a 'log in' hint instead."""
    box(pointed=True, logged_in=False)
    r = runner.invoke(app, ["queue"])
    assert r.exit_code != 0
    assert "log in" in r.output.lower()
    assert "queue" not in ran

def test_pointed_command_on_an_unpointed_box_runs_nothing(box, ran):
    """`login` on a box that knows no Head used to interrogate for a URL; that is `setup --client`'s
    job. The guard sends them there rather than minting a session against nothing."""
    box(pointed=False)
    r = runner.invoke(app, ["login"])
    assert r.exit_code != 0
    assert "setup --client" in r.output
    assert "login" not in ran

def test_login_with_an_explicit_head_points_the_unpointed_box_itself(box, ran):
    """`--head` is the one thing that may precede pointing: `auth.login` records the address, so the
    flag supplies the very precondition a bare `login` demands. The guard must let it through on a box
    that is not pointed yet, or the only way to point a box becomes the wizard — and the flag is dead."""
    box(pointed=False)
    runner.invoke(app, ["login", "--head", "ccc.lab"])
    assert "login" in ran


# --- the guard does NOT over-reach: the right box still runs -----------------------

def test_head_operator_can_still_add_users(box, ran):
    """The point of the guard is the wrong box, not every box — a Head-only command must still run
    on the Head, or we've traded a confusing failure for a locked door."""
    box(head=True)
    runner.invoke(app, ["user", "add", "bob"])
    assert "user_add" in ran

def test_owner_can_retract_their_own_arm_from_a_client(box, ran):
    """retract/return are {HEAD, USER}: an owner retracts their own card over the API from anywhere.
    The guard must keep that door open for a logged-in client, not make it Head-only."""
    box(pointed=True, logged_in=True)
    runner.invoke(app, ["arm", "retract", "node1"])
    assert "retract" in ran


# --- satisfied() labels the box correctly ------------------------------------------

def test_user_context_needs_both_pointed_and_a_token(box):
    """USER is the whole submitter surface. A box that is pointed but logged out must NOT count as
    USER, or every submitter command would sail past the guard and fail later at the Head."""
    box(pointed=True, logged_in=False)
    assert Ctx.POINTED in satisfied() and Ctx.USER not in satisfied()
    box(pointed=True, logged_in=True)
    assert Ctx.USER in satisfied()

def test_a_bare_box_is_only_any(box):
    """A machine with no role, no Head, no session can do exactly one thing — be set up. Nothing
    role- or session-gated may think it belongs here."""
    box()
    assert satisfied() == {Ctx.ANY}


def test_a_user_sees_arms_from_their_own_machine(box, ran, monkeypatch):
    """`arm list` is {HEAD, USER}: off the Head it reads the API (what a Dive can be sized against),
    never the Head-local registry it doesn't have."""
    from cuttle import enroll, queue
    monkeypatch.setattr(queue, "arms", lambda **k: ran.__setitem__("user_arms", k))
    monkeypatch.setattr(enroll, "list_arms", lambda **k: ran.__setitem__("head_arms", k))
    box(pointed=True, logged_in=True)
    runner.invoke(app, ["arm", "list", "--json"])
    assert ran == {"user_arms": {"as_json": True}}


def test_the_head_operator_keeps_the_admin_arm_view(box, ran, monkeypatch):
    from cuttle import enroll, queue
    monkeypatch.setattr(queue, "arms", lambda **k: ran.__setitem__("user_arms", k))
    monkeypatch.setattr(enroll, "list_arms", lambda **k: ran.__setitem__("head_arms", k))
    box(head=True)
    runner.invoke(app, ["arm", "list"])
    assert ran == {"head_arms": {"as_json": False}}
