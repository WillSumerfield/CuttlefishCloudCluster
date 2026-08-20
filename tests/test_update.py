"""What a re-run of `setup` is allowed to spend.

Setting a box up again is routine — a CCC upgrade, a new Feature, a rebuilt Environment — and it was
quietly expensive. `_setup_head` re-benchmarked the Head's own card every time, ten minutes and a
held GPU. It also re-ran the vendor activation every time, which draws down a finite count on the
licence key and does not come back; the message when they run out ("no more activations left on this
key") arrives on a box that cannot then be admitted. And `register_head_arm` wrote the Head's registry
row fresh on every run, resetting the very fields that would have said "already done".

All three are silent. Nothing failed, nothing was logged, and the only visible symptom was a setup
that took longer than it should have — until the key ran dry.
"""

import json

import pytest

from cuttle import config, enroll, features


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


class TestTheHeadsRegistryRowSurvivesBeingRewritten:
    """`register_head_arm` runs on every `setup --head`, and the row it writes is also where the
    Head's own Arm keeps everything only a real Spawn could learn. Writing it fresh reset all of it,
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

    def test_a_proved_activation_is_carried_forward(self, registry):
        mark = {"name": "vlearn", "path": "/opt/vlearn", "key": "/opt/keys/vlearn.dat"}
        registry({"headbox": {"activated": [mark]}})
        enroll.register_head_arm("100.99.0.1", [{"name": "vlearn", "path": "/opt/vlearn"}])
        assert enroll.load_registry()["headbox"]["activated"] == [mark]

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


class TestAnActivationIsNeverSpentTwice:
    """Each one draws down a finite count on the vendor's key, and the message when they run out
    ("no more activations left on this key") arrives on a box that cannot then be admitted."""

    FEATURE = {"name": "vlearn", "path": "/opt/vlearn", "key": "/opt/keys/vlearn.dat"}

    @pytest.fixture(autouse=True)
    def never_really_activates(self, monkeypatch):
        self.spent = []
        monkeypatch.setattr(features, "activation_project", lambda name: {"activate.py": ""})
        monkeypatch.setattr(enroll, "_activate_one",
                            lambda name, feat, project, client: self.spent.append(feat))

    def test_an_already_proved_feature_is_skipped(self, registry):
        registry({"willpc": {"activated": [enroll.activation_mark(self.FEATURE)]}})
        enroll.activate_arm("willpc", [self.FEATURE])
        assert self.spent == []

    def test_a_feature_never_proved_is_activated(self, registry):
        registry({"willpc": {"activated": []}})
        enroll.activate_arm("willpc", [self.FEATURE])
        assert self.spent == ["vlearn"]

    def test_success_is_what_records_the_marker(self, registry):
        """Recorded after the Spawn comes back clean, never before — a marker written ahead of the
        handshake would make one failed activation look permanently done."""
        registry({"willpc": {}})
        enroll.activate_arm("willpc", [self.FEATURE])
        assert enroll.load_registry()["willpc"]["activated"] == [
            enroll.activation_mark(self.FEATURE)]

    def test_a_renewed_licence_key_re_activates(self, registry):
        """The case the key path is part of the marker for: a new key file at a path the operator
        just named is exactly when re-activating is what they meant."""
        registry({"willpc": {"activated": [enroll.activation_mark(self.FEATURE)]}})
        enroll.activate_arm("willpc", [{**self.FEATURE, "key": "/opt/keys/vlearn-2027.dat"}])
        assert self.spent == ["vlearn"]

    def test_a_moved_install_re_activates(self, registry):
        registry({"willpc": {"activated": [enroll.activation_mark(self.FEATURE)]}})
        enroll.activate_arm("willpc", [{**self.FEATURE, "path": "/srv/vlearn"}])
        assert self.spent == ["vlearn"]

    def test_force_ignores_the_marker_entirely(self, registry):
        """`cuttle arm activate` — the operator saying the record stopped being true, for a licence
        that expired or an activation that failed and was fixed on the Arm since. Nothing calls it
        automatically, because each run costs one."""
        registry({"willpc": {"activated": [enroll.activation_mark(self.FEATURE)]}})
        enroll.activate_arm("willpc", [self.FEATURE], force=True)
        assert self.spent == ["vlearn"]

    def test_only_the_feature_that_changed_is_re_activated(self, registry):
        """Markers are per-Feature. Re-proving a whole Arm because one of its Features moved would
        spend activations on keys nothing happened to."""
        other = {"name": "othersim", "path": "/opt/other", "key": "/opt/keys/other.dat"}
        registry({"willpc": {"activated": [enroll.activation_mark(self.FEATURE),
                                           enroll.activation_mark(other)]}})
        enroll.activate_arm("willpc", [{**self.FEATURE, "key": "/new.dat"}, other])
        assert self.spent == ["vlearn"]
