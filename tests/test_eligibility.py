"""Eligibility: which devices a Spawn may run on.

Users declare what they *need*; only the Admin names a machine. That distinction is load-bearing for
the economy, not just for ergonomics: a queued Spawn adds Demand to every device it is eligible for,
so pinning to a machine would sidestep the Demand it should have created everywhere else.
"""

import pytest
from fastapi import HTTPException

from ccc_head_api import slurm


def registry(monkeypatch, arms: dict) -> None:
    monkeypatch.setattr(slurm, "load_registry", lambda: arms)


def arm(vram_mb: int | None, admitted: bool = True) -> dict:
    return {"vram_mb": vram_mb, "admitted": admitted}


class TestVramConstraint:
    """`--vram N` -> a Slurm constraint. Slurm's constraints are string SET-MEMBERSHIP: there is no
    `>=`. So an arbitrary integer can only be honoured by someone who knows every distinct card in the
    fleet and expands the request into the OR of the ones that qualify. That someone is the Head."""

    def test_or_expands_over_every_qualifying_card(self, monkeypatch):
        registry(monkeypatch, {
            "a": arm(24576),   # 24 GB
            "b": arm(49152),   # 48 GB
            "c": arm(81920),   # 80 GB
        })
        assert slurm.vram_constraint(20) == "vram24|vram48|vram80"

    def test_excludes_cards_that_are_too_small(self, monkeypatch):
        registry(monkeypatch, {"small": arm(8192), "big": arm(81920)})
        assert slurm.vram_constraint(24) == "vram80"

    def test_exact_fit_is_eligible(self, monkeypatch):
        """`>=`, not `>`: asking for 24 GB must be satisfied by a 24 GB card."""
        registry(monkeypatch, {"a": arm(24576)})
        assert slurm.vram_constraint(24) == "vram24"

    def test_identical_cards_collapse_to_one_feature(self, monkeypatch):
        """Three 24 GB boxes are one *feature*, not three. Slurm matches the feature; which of the
        three actually runs it is Slurm's business (it takes the fastest free one, by Weight)."""
        registry(monkeypatch, {"a": arm(24576), "b": arm(24576), "c": arm(24564)})
        assert slurm.vram_constraint(16) == "vram24"

    def test_unadmitted_arms_do_not_count(self, monkeypatch):
        """An authorized-but-not-yet-admitted Arm has a registry row and no node in Slurm. Offering its
        VRAM would render a constraint that matches nothing, and the Spawn would queue forever."""
        registry(monkeypatch, {"live": arm(24576), "pending": arm(81920, admitted=False)})
        assert slurm.vram_constraint(24) == "vram24"

    def test_unbenchmarked_arms_do_not_count(self, monkeypatch):
        """vram_mb is null between `arm authorize` and the donor's /enroll."""
        registry(monkeypatch, {"known": arm(24576), "unknown": arm(None)})
        assert slurm.vram_constraint(8) == "vram24"

    def test_impossible_request_is_refused_at_submit(self, monkeypatch):
        """Refused NOW, with the fleet's real sizes in the message — not queued forever against a
        constraint no card in the cluster can ever satisfy."""
        registry(monkeypatch, {"a": arm(24576), "b": arm(8192)})
        with pytest.raises(HTTPException) as e:
            slurm.vram_constraint(80)
        assert e.value.status_code == 422
        assert "8GB, 24GB" in e.value.detail

    def test_empty_cluster_is_refused(self, monkeypatch):
        registry(monkeypatch, {})
        with pytest.raises(HTTPException) as e:
            slurm.vram_constraint(8)
        assert e.value.status_code == 422


def arm_with_features(admitted: bool, features: list[str]) -> dict:
    return {"admitted": admitted, "features": [{"name": n, "path": f"/opt/{n}"} for n in features]}


class TestFeatureConstraint:
    """Arm Features (docs/TODO.md): a named, admin-curated capability an Arm may report having (VSim,
    IsaacSim, ...). Unlike VRAM (a bucketed OR — no card is EXACTLY the size asked for), a Feature is
    an exact name a node either has or doesn't, so requiring several is a plain AND."""

    def test_ands_every_requested_feature(self):
        assert slurm.feature_constraint(["isaacsim", "vsim"]) == "isaacsim&vsim"

    def test_duplicates_collapse(self):
        assert slurm.feature_constraint(["vsim", "vsim"]) == "vsim"


class TestArmsWithFeatures:
    """The feasibility check `submit_checked` runs before ever calling sbatch — mirroring
    `vram_constraint`'s "no Arm has that much VRAM" refusal, but for an exact-name AND instead of a
    size OR."""

    def test_finds_every_admitted_arm_carrying_all_of_them(self, monkeypatch):
        registry(monkeypatch, {
            "both": arm_with_features(True, ["vsim", "isaacsim"]),
            "one": arm_with_features(True, ["vsim"]),
            "none": arm_with_features(True, []),
        })
        assert sorted(slurm.arms_with_features(["vsim"])) == ["both", "one"]
        assert slurm.arms_with_features(["vsim", "isaacsim"]) == ["both"]

    def test_unadmitted_arms_do_not_count(self, monkeypatch):
        """An authorized-but-not-yet-admitted Arm has a registry row and no node in Slurm — offering
        its declared feature would render a constraint no live node can ever match."""
        registry(monkeypatch, {"pending": arm_with_features(False, ["vsim"])})
        assert slurm.arms_with_features(["vsim"]) == []

    def test_no_arm_satisfies_an_uncarried_feature(self, monkeypatch):
        registry(monkeypatch, {"a": arm_with_features(True, ["vsim"])})
        assert slurm.arms_with_features(["ghost"]) == []

    def test_empty_request_is_satisfied_by_any_admitted_arm(self, monkeypatch):
        registry(monkeypatch, {"any": arm_with_features(True, [])})
        assert slurm.arms_with_features([]) == ["any"]


class TestAllFeatureNames:
    def test_union_of_every_admitted_arms_features(self, monkeypatch):
        registry(monkeypatch, {
            "a": arm_with_features(True, ["vsim"]),
            "b": arm_with_features(True, ["isaacsim"]),
            "pending": arm_with_features(False, ["ghost"]),
        })
        assert slurm.all_feature_names() == ["isaacsim", "vsim"]
