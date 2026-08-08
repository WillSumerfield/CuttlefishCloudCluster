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


def arm(vram_mb: int | None, admitted: bool = True, devices: int = 1) -> dict:
    """`devices` is how many cards the box has — `gpu_devices` in a real registry row, one name per
    card. All the same size: a mixed-GPU box is refused at enroll, which is what makes `vram_mb` one
    number for the node and the per-card division exact."""
    return {"vram_mb": vram_mb, "admitted": admitted,
            "gpu_devices": [f"card{i}" for i in range(devices)]}


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


class TestMultiDevice:
    """`--gpus N` asks for N Devices **on one Arm** (CONTEXT.md — never across two), and `--vram` is
    the TOTAL across them. Both are one joint question: an Arm with enough cards, each holding its
    share."""

    def test_vram_is_divided_across_the_cards_asked_for(self, monkeypatch):
        """128GB over two cards asks 64 of each — a 2×96 box has it, a 2×48 box does not, though its
        two cards TOGETHER hold 96."""
        registry(monkeypatch, {"pair": arm(98304, devices=2)})
        assert slurm.vram_constraint(128, 2) == "vram96"
        registry(monkeypatch, {"pair": arm(49152, devices=2)})
        with pytest.raises(HTTPException) as e:
            slurm.vram_constraint(128, 2)
        assert e.value.status_code == 422

    def test_the_two_needs_are_asked_of_one_arm_together(self, monkeypatch):
        """The trap the joint check exists for. A 2×40 box satisfies "two cards"; a 1×96 box satisfies
        "90GB each". Asked separately both pass and the Spawn queues forever, because neither box can
        host it."""
        registry(monkeypatch, {"pair": arm(40960, devices=2), "big": arm(98304, devices=1)})
        with pytest.raises(HTTPException) as e:
            slurm.vram_constraint(180, 2)
        assert e.value.status_code == 422

    def test_a_count_no_arm_has_names_the_largest(self, monkeypatch):
        registry(monkeypatch, {"a": arm(98304, devices=1), "b": arm(24576, devices=2)})
        with pytest.raises(HTTPException) as e:
            slurm.vram_constraint(8, 4)
        assert "no Arm has 4 GPUs" in e.value.detail
        assert "the largest has 2" in e.value.detail

    def test_an_unmeetable_total_suggests_the_count_that_would_work(self, monkeypatch):
        """The derived need and the fix, and nothing else — no machine names, no per-Arm tallies. 96GB
        on one 48GB card is impossible; spread over the box's two, it is 48 each, which fits."""
        registry(monkeypatch, {"pair": arm(49152, devices=2)})
        with pytest.raises(HTTPException) as e:
            slurm.vram_constraint(96, 1)
        assert "no Arm has 96GB of VRAM" in e.value.detail
        assert "try --gpus 2" in e.value.detail

    def test_a_multi_card_refusal_names_the_derived_per_card_need(self, monkeypatch):
        """Past one card the total alone doesn't explain the refusal — 128GB sounds satisfiable on a
        box holding 96. The per-card figure is the number that actually failed."""
        registry(monkeypatch, {"pair": arm(49152, devices=2)})
        with pytest.raises(HTTPException) as e:
            slurm.vram_constraint(128, 2)
        assert "128GB across 2 GPUs needs 64GB per card" in e.value.detail

    def test_no_count_works_so_nothing_is_suggested(self, monkeypatch):
        """Two 24GB cards can never total 200GB. Suggesting a count that also fails would be worse than
        saying nothing."""
        registry(monkeypatch, {"pair": arm(24576, devices=2)})
        with pytest.raises(HTTPException) as e:
            slurm.vram_constraint(200, 2)
        assert "try --gpus" not in e.value.detail

    def test_one_gpu_behaves_exactly_as_before(self, monkeypatch):
        """The common path is untouched: at one Device the total IS the per-card need, and the refusal
        still names the fleet's real sizes."""
        registry(monkeypatch, {"a": arm(24576), "b": arm(49152)})
        assert slurm.vram_constraint(20, 1) == "vram24|vram48"
        assert slurm.vram_constraint(20) == "vram24|vram48"

    def test_a_partly_filled_box_still_counts_every_card_it_has(self, monkeypatch):
        """Device count comes from the registry's per-card list, not from VRAM — the two say different
        things and only one of them counts cards."""
        registry(monkeypatch, {"quad": arm(24576, devices=4)})
        assert slurm.max_devices() == 4
        assert slurm.vram_constraint(96, 4) == "vram24"


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
