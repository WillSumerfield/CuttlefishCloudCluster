"""`cuttle update` — laying a box's role down again from what it already recorded.

Re-running `setup` after a CCC upgrade works and is what everyone did, but it re-asks every question
setup ever asked: ten of them on a Head, and every answer already on the box. Worse, two of those
re-runs SPENT something. The Head re-benchmarked its own card, and it re-ran the vendor activation —
which draws down a finite count on the licence key and does not come back. A routine re-provision
was quietly consuming a resource nobody was watching.

So the consequences guarded here are the expensive and the silent ones: that an Update asks nothing
at all (enforced, not merely intended), that neither duty re-spends a benchmark or an activation it
has already paid for, that the Head's registry row survives being written over, and that a box with
nothing recorded is told so rather than half-provisioned from defaults.
"""

import json

import pytest
import typer

from cuttle import config, enroll, mesh, menu, settings, tls, ui
from cuttle import setup as setup_mod


@pytest.fixture
def answers():
    """A scripted human, guaranteed uninstalled — a leaked one makes the next test answer prompts it
    never asked."""
    def install(*ordered):
        ui.install_answers(ui.Answers(*ordered))
    yield install
    ui.install_answers(None)


@pytest.fixture
def registry(monkeypatch, tmp_path):
    """A writable arms.json. `save_registry` goes through `sudo install` on a real box; here it is a
    plain write, so a test can read back what the code believed it stored."""
    path = tmp_path / "arms.json"
    monkeypatch.setattr(config, "ARMS_REGISTRY", path)
    monkeypatch.setattr(enroll, "save_registry",
                        lambda reg: path.write_text(json.dumps(reg, indent=2, sort_keys=True)))

    def write(reg):
        path.write_text(json.dumps(reg, indent=2, sort_keys=True))
    return write


class TestAnUpdateAsksNothing:
    """The whole point of the duty. Guarding each call site is what makes it true today; `no_prompting`
    is what keeps it true, because the way this breaks is somebody adding a prompt to a shared step
    long after anyone remembers that a silent duty runs through it."""

    def test_a_prompt_raised_inside_it_is_refused_not_answered(self):
        with pytest.raises(typer.Exit):
            with ui.no_prompting("`cuttle update`"):
                ui.ask_text("Default project", default="ccc")

    def test_it_is_refused_even_with_an_answer_available(self, answers):
        """Checked ahead of the scripted human deliberately: a test that answers the question would
        hide the exact regression this exists to catch, and so would a stray `--yes`."""
        answers("ccc")
        with pytest.raises(typer.Exit):
            with ui.no_prompting("`cuttle update`"):
                ui.ask_text("Default project", default="ccc")

    def test_the_guard_is_lifted_again_afterwards(self):
        """It is a property of one flow, not of the process. A leaked guard would turn the next
        command's first question into a crash."""
        with pytest.raises(typer.Exit):
            with ui.no_prompting("`cuttle update`"):
                ui.ask_text("anything")
        assert ui._REFUSE is None

    def test_sudo_still_asks_for_its_password(self, answers):
        """A sudo password is permission, not an answer this box already holds — refusing it made
        `cuttle update` abort on every box whose sudo needs a password."""
        answers("hunter2")
        with ui.no_prompting("`cuttle update`"):
            assert ui.ask_password("Your sudo password", permission=True) == "hunter2"

    def test_the_head_flow_runs_under_it(self, monkeypatch):
        """Not just that `setup_head` is called with update=True — that it is called somewhere it
        *cannot* ask, which is the promise a User is actually given."""
        monkeypatch.setattr(menu, "is_head", lambda: True)
        monkeypatch.setattr(setup_mod, "setup_head",
                            lambda address=None, update=False: ui.ask_text("smuggled question"))
        with pytest.raises(typer.Exit):
            setup_mod.update_this_box()

    def test_the_arm_flow_runs_under_it(self, monkeypatch):
        monkeypatch.setattr(menu, "is_head", lambda: False)
        monkeypatch.setattr(menu, "is_arm", lambda: True)
        monkeypatch.setattr(setup_mod, "setup_arm",
                            lambda token, re_enroll=False, update=False: ui.ask_text("smuggled"))
        with pytest.raises(typer.Exit):
            setup_mod.update_this_box()

    def test_the_client_flow_may_still_ask_its_one_question(self, monkeypatch):
        """The exception, and the only one: re-trusting a Head that was rebuilt behind the same name
        is a trust decision, and no stored answer can stand in for a human looking at a fingerprint."""
        monkeypatch.setattr(menu, "is_head", lambda: False)
        monkeypatch.setattr(menu, "is_arm", lambda: False)
        monkeypatch.setattr(menu, "is_pointed", lambda: True)
        asked = []
        monkeypatch.setattr(setup_mod, "setup_client",
                            lambda update=False: asked.append(ui._REFUSE))
        setup_mod.update_this_box()
        assert asked == [None]


class TestTheRoleComesFromTheBoxNotAFlag:
    """An Update never decides what this box is — so there is no way to tell it to make this box
    something else. That is the entire line between it and `setup`."""

    @pytest.mark.parametrize("head,arm,pointed,expect", [
        (True, False, False, "head"),
        (False, True, False, "arm"),
        (False, False, True, "client"),
    ])
    def test_each_role_lays_down_its_own_duty(self, monkeypatch, head, arm, pointed, expect):
        monkeypatch.setattr(menu, "is_head", lambda: head)
        monkeypatch.setattr(menu, "is_arm", lambda: arm)
        monkeypatch.setattr(menu, "is_pointed", lambda: pointed)
        ran = []
        monkeypatch.setattr(setup_mod, "setup_head",
                            lambda address=None, update=False: ran.append(("head", update)))
        monkeypatch.setattr(setup_mod, "setup_arm",
                            lambda token, re_enroll=False, update=False: ran.append(("arm", update,
                                                                                     token)))
        monkeypatch.setattr(setup_mod, "setup_client",
                            lambda update=False: ran.append(("client", update)))
        setup_mod.update_this_box()
        assert ran[0][0] == expect and ran[0][1] is True

    def test_an_arm_update_carries_no_token(self, monkeypatch):
        """An Update is never an enrollment. A token here would be a second identity for a box that
        already has one — and the whole reason the duty exists is that the token is long spent."""
        monkeypatch.setattr(menu, "is_head", lambda: False)
        monkeypatch.setattr(menu, "is_arm", lambda: True)
        seen = {}
        monkeypatch.setattr(setup_mod, "setup_arm",
                            lambda token, re_enroll=False, update=False: seen.update(
                                token=token, re_enroll=re_enroll))
        setup_mod.update_this_box()
        assert seen == {"token": None, "re_enroll": False}

    def test_a_box_that_was_never_set_up_is_told_so(self, monkeypatch):
        """There is nothing to lay down again. Proceeding would mean answering from defaults, i.e.
        deciding what this box is — which is the one thing an Update does not do."""
        monkeypatch.setattr(menu, "is_head", lambda: False)
        monkeypatch.setattr(menu, "is_arm", lambda: False)
        monkeypatch.setattr(menu, "is_pointed", lambda: False)
        with pytest.raises(typer.Exit):
            setup_mod.update_this_box()

    def test_an_arm_off_the_mesh_is_refused_by_the_flow_it_calls(self, ccc_home, monkeypatch):
        """`enrollment_from_state` already aborts naming a fresh token and `--re-enroll`, which is
        both the right message and the only fix. Re-worded here it would print twice."""
        settings.save({"arm": {"node_name": "willpc", "head_mesh_ip": "100.99.0.1"}})
        monkeypatch.setattr(mesh, "current_coordinator", lambda: "https://head:8080")
        monkeypatch.setattr(mesh, "joined_to", lambda c: False)
        with pytest.raises(typer.Exit):
            enroll.enrollment_from_state()


class TestTheHeadsRegistryRowSurvivesBeingRewritten:
    """`register_head_arm` runs on every `setup --head`, and the row it writes is also where the
    Head's own Arm keeps everything only a real Dive could learn. Writing it fresh reset all of it,
    so each re-setup re-benchmarked the card and re-ran the licence handshake — and an activation
    spent that way does not come back."""

    @pytest.fixture(autouse=True)
    def hardware(self, monkeypatch):
        monkeypatch.setattr(enroll.system, "hostname", lambda: "headbox")
        monkeypatch.setattr(enroll.system, "gpu_devices", lambda: ["/dev/nvidia0"])
        monkeypatch.setattr(enroll.system, "gpu_vram_mb", lambda: 24564)

    def test_a_measured_power_is_carried_forward(self, registry):
        registry({"headbox": {"owner": "will", "powers": [61.4], "gpu": "RTX 4090"}})
        enroll.register_head_arm("100.99.0.1", [])
        row = enroll.load_registry()["headbox"]
        assert row["powers"] == [61.4] and row["gpu"] == "RTX 4090"

    def test_a_first_setup_still_starts_unmeasured(self, registry):
        """The merge must not invent a Power for a box that has never had one — `ensure_priced` reads
        exactly this field to decide whether the benchmark is owed."""
        registry({})
        enroll.register_head_arm("100.99.0.1", [])
        assert enroll.load_registry()["headbox"]["powers"] is None

    def test_the_declaration_itself_is_replaced_not_merged(self, registry):
        """Features are the one part `setup` is there to CHANGE. A Feature the operator just removed
        must leave the row, or the Head keeps scheduling against hardware that no longer declares it."""
        registry({"headbox": {"features": [{"name": "vlearn", "path": "/opt/vlearn"}]}})
        enroll.register_head_arm("100.99.0.1", [])
        assert enroll.load_registry()["headbox"]["features"] == []

    def test_a_licence_key_path_never_reaches_the_registry(self, registry):
        """The registry is served to the whole cluster; where a box keeps its key is that box's
        business. Name and path only, exactly as /enroll receives them."""
        registry({})
        enroll.register_head_arm("100.99.0.1",
                                 [{"name": "vlearn", "path": "/opt/vlearn", "key": "/opt/k.dat"}])
        assert enroll.load_registry()["headbox"]["features"] == [
            {"name": "vlearn", "path": "/opt/vlearn"}]


class TestNothingIsMeasuredOrLicensedTwice:
    def test_a_priced_arm_is_not_re_benchmarked(self, registry, monkeypatch):
        """Ten minutes and a held GPU. A re-run of `setup` is not a claim that the hardware changed."""
        registry({"willpc": {"powers": [61.4]}})
        monkeypatch.setattr(enroll, "benchmark_arm",
                            lambda name: pytest.fail("re-benchmarked an Arm that was already priced"))
        enroll.ensure_priced("willpc")

    def test_an_unpriced_arm_is_still_benchmarked(self, registry, monkeypatch):
        """The other half. An Arm with no Power cannot be scheduled against, so joining must measure
        it — and an Update that finds one unmeasured should fix that rather than carry it forward."""
        registry({"willpc": {"powers": None}})
        ran = []
        monkeypatch.setattr(enroll, "benchmark_arm", lambda name: ran.append(name))
        enroll.ensure_priced("willpc")
        assert ran == ["willpc"]


class TestAClientUpdateOnlyRepairsItsTrust:
    """Almost nothing is installed on a client, so almost nothing can go out of date. What can is the
    pinned CA — and the way a client meets that is the Head being torn down and set up again behind
    the same name, after which every request fails verification and the error surfaces at the next
    `cuttle queue` looking like a network fault."""

    def test_a_lost_hosts_line_is_refused_rather_than_guessed(self, ccc_home, monkeypatch):
        """A client keeps its Head's address in that /etc/hosts line and nowhere else — the stored URL
        is the **Cluster name**. With the line gone there is nothing here to restore it from, and
        inventing an address is re-pointing the box, which is a `setup`."""
        settings.save({"head": "https://ccc.lab"})
        monkeypatch.setattr(setup_mod, "make_user_dirs", lambda: None)
        monkeypatch.setattr(setup_mod.socket, "gethostbyname",
                            lambda host: (_ for _ in ()).throw(OSError("no such host")))
        with pytest.raises(typer.Exit):
            setup_mod._update_client()

    def test_a_changed_authority_is_never_re_pinned_unattended(self, ccc_home, monkeypatch):
        """Silently re-pinning would turn trust-on-first-use into trust-on-every-use: whatever answers
        at that address gets trusted, which is the check the first ceremony exists to make."""
        pin = config.PINS_DIR / "ccc.lab"
        pin.parent.mkdir(parents=True, exist_ok=True)
        pin.write_text("old")
        monkeypatch.setattr(tls, "_pin_path", lambda url: pin)
        monkeypatch.setattr(tls, "signs_leaf", lambda url, pem: "new" in pem)
        monkeypatch.setattr(tls, "fetch_ca", lambda url: "new")
        monkeypatch.setattr(tls, "fingerprint", lambda pem: "AA:BB")
        monkeypatch.setattr(ui, "interactive", lambda: False)
        with pytest.raises(typer.Exit):
            tls.refresh_pin("https://ccc.lab")
        assert "old" in pin.read_text(), "re-pinned with nobody there to check the fingerprint"

    def test_an_unchanged_authority_is_left_alone(self, ccc_home, monkeypatch):
        """The ordinary case, and it must cost nothing — no fetch, no question, no root write."""
        pin = config.PINS_DIR / "ccc.lab"
        pin.parent.mkdir(parents=True, exist_ok=True)
        pin.write_text("good")
        monkeypatch.setattr(tls, "_pin_path", lambda url: pin)
        monkeypatch.setattr(tls, "signs_leaf", lambda url, pem: True)
        monkeypatch.setattr(tls, "fetch_ca",
                            lambda url: pytest.fail("refetched a CA that still verifies"))
        tls.refresh_pin("https://ccc.lab")
