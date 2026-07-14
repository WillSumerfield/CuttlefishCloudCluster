"""Phase 4 slice 0 — the two numbers that price a device: what it can HOLD and what it can DO.

`Price = Power x Demand`. These cover the Power half, which is intrinsic to the card and fixed at
enroll, plus the VRAM feature that decides which cards a Spawn is even eligible for.
"""

from cuttle import config


class TestVramFeature:
    """VRAM -> the Slurm feature a card advertises. Slurm constraints are string set-membership, so
    this string is the *only* thing a Spawn's `--vram` can be matched against."""

    def test_round_numbers(self):
        assert config.vram_feature(8192) == "vram8"
        assert config.vram_feature(81920) == "vram80"

    def test_rounds_up_not_down(self):
        """A 4090 reports 24564 MiB, which is 23.99 GiB. Flooring would advertise it as `vram23` — so
        `--vram 24` would find no card, on a cluster that plainly has a 24 GB GPU in it."""
        assert config.vram_feature(24564) == "vram24"

    def test_real_cards(self):
        for vram_mb, expected in [
            (16303, "vram16"),  # the Head box (measured)
            (24576, "vram24"),  # 3090
            (40960, "vram40"),  # A100 40GB
            (81920, "vram80"),  # A100/H100 80GB
        ]:
            assert config.vram_feature(vram_mb) == expected


class TestNodeWeight:
    """Slurm schedules the LOWEST-weight eligible node first, so Weight must run *inverse* to Power."""

    def test_faster_card_wins(self):
        assert config.node_weight(80.0) < config.node_weight(20.0)

    def test_strictly_monotonic(self):
        weights = [config.node_weight(p) for p in (5.0, 11.0, 35.0, 82.3, 300.0)]
        assert weights == sorted(weights, reverse=True)

    def test_never_zero(self):
        """Weight=0 is not a valid Slurm node weight. An absurdly fast card must still clamp to 1."""
        assert config.node_weight(1e9) == 1

    def test_slow_card_is_not_free_to_prefer(self):
        """The inversion this whole mechanism exists to prevent: bill by the second alone and the SLOW
        card earns 4x for the same work, so donating your worst GPU would pay best. Weight has to make
        Slurm reach for the fast card first, not the cheap-looking one."""
        fast, slow = 80.0, 20.0
        assert config.node_weight(fast) * 4 == config.node_weight(slow)
