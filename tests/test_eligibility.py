"""Phase 4 slice 0 — Eligibility: which devices a Spawn may run on.

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
