"""Phase 4 slice 0 — the two numbers that price a device: what it can HOLD and what it can DO.

`Price = Power x Demand`. These cover the Power half, which is intrinsic to the card and fixed at
enroll, plus the VRAM feature that decides which cards a Spawn is even eligible for.

Also the **Registration** those numbers are collected by: how many **Device**s a box actually offers,
and the refusal of a box whose GPUs are not all alike (which no single `vramN` Feature could describe).
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from ccc_head_api import karma
from cuttle import config, system


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
        assert config.node_weight([80.0]) < config.node_weight([20.0])

    def test_strictly_monotonic(self):
        weights = [config.node_weight([p]) for p in (5.0, 11.0, 35.0, 82.3, 300.0)]
        assert weights == sorted(weights, reverse=True)

    def test_never_zero(self):
        """Weight=0 is not a valid Slurm node weight. An absurdly fast card must still clamp to 1."""
        assert config.node_weight([1e9]) == 1

    def test_slow_card_is_not_free_to_prefer(self):
        """The inversion this whole mechanism exists to prevent: bill by the second alone and the SLOW
        card earns 4x for the same work, so donating your worst GPU would pay best. Weight has to make
        Slurm reach for the fast card first, not the cheap-looking one."""
        fast, slow = 80.0, 20.0
        assert config.node_weight([fast]) * 4 == config.node_weight([slow])

    def test_a_multi_card_box_weighs_by_its_mean(self):
        """Slurm has one Weight per node, so a box's per-Device Powers must reduce to a single number
        somewhere. The mean is right HERE and nowhere else: Weight is a preference ordering among
        nodes, never a price — nothing multiplies a second of held time by it."""
        assert config.node_weight([40.0, 60.0]) == config.node_weight([50.0])

    def test_the_reduction_never_reaches_a_bill(self):
        """The guard on the paragraph above. Two Spawns on the same box, one on each card, must cost
        differently — the node's mean Weight has no say in what either of them pays."""
        assert karma.cost(3600, [40.0], 1.0) != karma.cost(3600, [60.0], 1.0)


def fake_smi(monkeypatch, gpus: list[tuple[str, int]]) -> None:
    """Stand in for `nvidia-smi --query-gpu=name,memory.total`, one CSV row per physical card."""
    csv = "".join(f"{name}, {vram}\n" for name, vram in gpus)
    monkeypatch.setattr(system, "nvidia_smi", lambda: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(system.subprocess, "run",
                        lambda *a, **kw: SimpleNamespace(stdout=csv, returncode=0))


def fake_dev(monkeypatch, names: list[str], dxg: bool = False) -> None:
    """Stand in for /dev: the device nodes a box exposes."""
    monkeypatch.setattr(system.Path, "exists", lambda self: dxg and str(self) == "/dev/dxg")
    monkeypatch.setattr(system.Path, "glob", lambda self, pat: [Path(f"/dev/{n}") for n in names])


class TestDeviceEnumeration:
    """How many **Device**s a box offers — the count that becomes `Gres=gpu:N` and the list that
    becomes its gres.conf lines. Getting it wrong loses whole cards silently: they simply never appear
    to the scheduler, on someone else's machine."""

    def test_one_card(self, monkeypatch):
        fake_dev(monkeypatch, ["nvidia0"])
        assert system.gpu_devices() == ["/dev/nvidia0"]

    def test_every_card_is_reported(self, monkeypatch):
        fake_dev(monkeypatch, ["nvidia0", "nvidia1", "nvidia2", "nvidia3"])
        assert system.gpu_devices() == [f"/dev/nvidia{i}" for i in range(4)]

    def test_devices_are_ordered_numerically_not_lexically(self, monkeypatch):
        """This order IS the map from a Slurm GRES index to a card, and so to the Power a Spawn is
        settled at. Sorted as strings, `/dev/nvidia10` lands second and every Spawn on a ten-card box
        past index 1 is billed at another card's rate."""
        fake_dev(monkeypatch, ["nvidia10", "nvidia2", "nvidia0", "nvidia1"])
        assert system.gpu_devices() == ["/dev/nvidia0", "/dev/nvidia1", "/dev/nvidia2",
                                        "/dev/nvidia10"]

    def test_control_nodes_are_not_cards(self, monkeypatch):
        """/dev/nvidiactl and /dev/nvidia-uvm sit right beside the cards and are not ones. A glob of
        `nvidia*` would register two phantom GPUs on every single node."""
        fake_dev(monkeypatch, ["nvidia0", "nvidia1"])
        got = system.gpu_devices()
        assert got == ["/dev/nvidia0", "/dev/nvidia1"]
        assert all(g.removeprefix("/dev/nvidia").isdigit() for g in got)

    def test_wsl_is_permanently_one_device_however_many_cards(self, monkeypatch):
        """Not a deferral. /dev/dxg is ONE device node for ALL of a Windows box's GPUs and a device
        cgroup can only grant or deny it whole — there is nothing in it to partition, and no Slurm
        release can change that. So a WSL Arm with four 4090s still registers exactly one Device."""
        fake_smi(monkeypatch, [("NVIDIA GeForce RTX 4090", 24564)] * 4)
        fake_dev(monkeypatch, [], dxg=True)
        assert system.gpu_devices() == ["/dev/dxg"]
        assert len(system.gpu_inventory()) == 4   # the cards are really there; only one is offered


class TestMixedGpusAreRefused:
    """A Slurm node **Feature** is per-node: one box advertises exactly one `vramN`, and that string
    is the only thing `--vram` can be matched against. A box holding a 24 GB card and an 80 GB one
    cannot describe itself honestly, so enrollment refuses it rather than pick a side and misprice
    every Spawn that lands on the other card."""

    def test_identical_cards_are_fine(self, monkeypatch):
        fake_smi(monkeypatch, [("NVIDIA GeForce RTX 4090", 24564)] * 4)
        assert system.gpu_profile() == ("NVIDIA GeForce RTX 4090", 24564)
        assert system.gpu_vram_mb() == 24564

    def test_mixed_vram_is_refused_naming_what_was_found(self, monkeypatch, capsys):
        """Named, not just refused: "your GPUs differ" sends an operator to look, while the two lines
        below tell them which box and which slot without leaving the terminal."""
        fake_smi(monkeypatch, [("NVIDIA A100-SXM4-40GB", 40960),
                               ("NVIDIA A100-SXM4-80GB", 81920)])
        with pytest.raises(typer.Exit):
            system.gpu_profile()
        said = capsys.readouterr().err
        assert "same model" in said and "40960" in said and "81920" in said

    def test_mixed_model_is_refused_even_at_the_same_vram(self, monkeypatch, capsys):
        """Model and memory answer different questions — the model is what **Power** tracks, the
        memory is what **Eligibility** matches — so a 3090 and a 4090 both reading 24 GB are still two
        different prices under one Weight."""
        fake_smi(monkeypatch, [("NVIDIA GeForce RTX 3090", 24576),
                               ("NVIDIA GeForce RTX 4090", 24576)])
        with pytest.raises(typer.Exit):
            system.gpu_profile()
        said = capsys.readouterr().err
        assert "RTX 3090" in said and "RTX 4090" in said

    def test_vram_reads_every_card_rather_than_the_first(self, monkeypatch):
        """The old bug: `.split("\\n")[0]` described a whole box by whichever card the driver happened
        to enumerate first, silently. Reading them all is what MAKES the homogeneity check."""
        fake_smi(monkeypatch, [("NVIDIA GeForce RTX 4090", 24564),
                               ("NVIDIA A100-SXM4-80GB", 81920)])
        with pytest.raises(typer.Exit):
            system.gpu_vram_mb()

    def test_a_box_with_no_gpu_is_refused(self, monkeypatch):
        fake_smi(monkeypatch, [])
        with pytest.raises(typer.Exit):
            system.gpu_profile()
