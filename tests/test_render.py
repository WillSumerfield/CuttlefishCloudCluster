"""What setup and enrollment actually WRITE to disk.

Every failure guarded here is silent at write time and only surfaces on a donor's box, minutes later,
as a node that registered but can't be scheduled. That is exactly the class of bug worth a unit test:
the render is pure, and the consequence is remote.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

from cuttle import ccc_launch, config, enroll, headapi, system, templates


@pytest.fixture
def fake_host(monkeypatch):
    """The Head box, without touching the Head box: no nvidia-smi, no lscpu, no /proc/meminfo."""
    monkeypatch.setattr(system, "hostname", lambda: "headbox")
    monkeypatch.setattr(system, "cpu_topology", lambda: (1, 8, 2))
    monkeypatch.setattr(system, "real_memory_mb", lambda: 63188)
    monkeypatch.setattr(system, "gpu_vram_mb", lambda: 24564)
    monkeypatch.setattr(system, "gpu_devices", lambda: ["/dev/nvidia0"])


def directives(conf: str) -> dict[str, str]:
    """The settings slurmctld will actually READ: comments and blank lines dropped.

    Parsed, not substring-matched. `"PriorityType=priority/multifactor" in conf` is satisfied by a
    line that has been *commented out* — which is precisely how this setting would get lost, and it
    would take the entire Karma economy silently with it.
    """
    out = {}
    for line in conf.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key] = value
    return out


class TestSlurmConf:
    def test_priority_is_multifactor(self, fake_host):
        """The single setting without which Karma cannot exist. Slurm's default is `priority/basic` —
        pure FIFO, which IGNORES nice entirely. Karma reaches the queue only by lowering the nice of a
        poor-standing User's pending Spawns, so on the default plugin every ranking decision we make
        would be computed correctly, pushed to Slurm successfully, and have no effect whatsoever."""
        assert directives(templates.render_slurm_conf("100.99.0.1"))["PriorityType"] \
            == "priority/multifactor"

    def test_poor_standing_costs_at_most_one_ramp_of_queue_age(self, fake_host):
        """Nice and age must span the SAME range, so the worst standing in the cluster costs a Spawn at
        most one PriorityMaxAge of apparent queue age — Slurm treats it as though it arrived a day later
        than it did. If the nice range outgrew the age weight, waiting could no longer buy back what
        standing took, and poor standing would become a gate rather than a handicap.

        (This is a bound on the handicap, NOT a starvation bound: age saturates at PriorityMaxAge, and a
        rival can max its own age out too. Starvation is bounded economically — spending Karma is what
        makes you poor. See temp/KARMA.md.)
        """
        d = directives(templates.render_slurm_conf("100.99.0.1"))
        assert int(d["PriorityWeightAge"]) >= config.NICE_MAX
        assert d["PriorityMaxAge"]

    def test_a_fresh_spawn_never_hits_the_priority_floor(self, fake_host):
        """The bug that would have silently killed the whole economy.

        Slurm's priority is a `uint32_t` and it clamps at 0. The weighted sum of a *fresh* Spawn has
        age_factor 0, so without a flat base it computes `-nice` — and every fresh Spawn from every
        User below perfect standing would underflow onto the floor, all tied. Karma would have ranked
        correctly, pushed the nice successfully, and changed nothing: the same silent death as
        priority/basic ignoring nice.

        So the worst case the economy can produce — brand new Spawn, worst possible standing — must
        still land clear of zero.
        """
        d = directives(templates.render_slurm_conf("100.99.0.1"))
        base, age = int(d["PriorityWeightPartition"]), int(d["PriorityWeightAge"])
        assert base - config.NICE_MAX > 0          # fresh + worst standing: age contributes nothing
        assert base + age + config.NICE_MAX < 2**32  # ...and the other rail is just as fatal

    def test_a_bump_outranks_anything_the_economy_can_do(self, fake_host):
        """An admin Queue bump is one negative step of nice, and it has to actually win.

        Worst case for the bumped Spawn: it was submitted a second ago, so age gives it nothing. Best
        case for its rival: perfect standing (nice 0) and fully aged. Nice can only ever *lower* a
        rival, so beating that one is beating all of them — which makes the whole requirement
        `BUMP_STEP > PriorityWeightAge`.
        """
        d = directives(templates.render_slurm_conf("100.99.0.1"))
        assert config.BUMP_STEP > int(d["PriorityWeightAge"])

    def test_only_age_and_karma_rank_the_queue(self, fake_host):
        """Every other factor is zeroed. Fairshare especially: it would need slurmdbd, and we compute
        standing ourselves — two schedulers disagreeing about priority is worse than either alone.

        Partition is NOT in this list: it is the flat base (see above), not a ranking factor. With a
        single partition Slurm normalizes its factor to exactly 1.0 for every job, so it adds a
        constant and reorders nobody — which is also why a SECOND partition would silently scale the
        base down, and why there is exactly one.
        """
        conf = templates.render_slurm_conf("100.99.0.1")
        d = directives(conf)
        for factor in ("Fairshare", "JobSize", "QOS"):
            assert d[f"PriorityWeight{factor}"] == "0"
        assert len([ln for ln in conf.splitlines() if ln.startswith("PartitionName=")]) == 1

    def test_head_node_advertises_its_vram(self, fake_host):
        """The Head's co-located Arm is an Arm like any other, and must be eligible like one."""
        assert "Feature=vram24" in directives(templates.render_slurm_conf("100.99.0.1"))["NodeName"]

    def test_weight_is_baked_in(self, fake_host):
        """Weight IS rendered here, deliberately: `scontrol reconfigure` resets every node to what
        the config files say, so a Weight pushed only by `scontrol update` is wiped by the next
        `arm authorize`/`admit`/`remove` (measured: update -> 276, reconfigure -> 1). The file is
        the only durable home a *static* node has."""
        assert "Weight=276" in directives(templates.render_slurm_conf("100.99.0.1", 276))["NodeName"]

    def test_an_unbenchmarked_head_still_renders(self, fake_host):
        """The first setup writes slurm.conf BEFORE the benchmark can run (the node must be live to be
        benchmarked), so Power is genuinely unknown here exactly once. It must render anyway — and
        `scontrol update` carries the real Weight until the next render."""
        assert "Weight=1" in directives(templates.render_slurm_conf("100.99.0.1"))["NodeName"]


def gres_lines(conf: str) -> list[str]:
    return [ln for ln in conf.splitlines() if ln and not ln.startswith("#")]


class TestGresConf:
    """The file that maps a Slurm GRES to a real device node — and, since the Devices are indexed in
    the order these lines are read, the file that decides which **Power** a running Spawn is settled
    at. A wrong line here is not a crash: it is a bill quietly charged at the other card's rate."""

    def test_a_single_card_arm_gets_one_line(self, fake_host):
        conf = templates.render_gres_conf([("donor-box", ["/dev/nvidia0"])])
        assert gres_lines(conf) == ["NodeName=donor-box Name=gpu File=/dev/nvidia0"]

    def test_every_card_gets_its_own_line_in_order(self, fake_host):
        """One line per Device, not `File=/dev/nvidia[0-3]` and not `MultipleFiles` (which is MIG's,
        and would declare ONE resource backed by four files). Order is the index order Slurm reports
        back as `IDX:n`, so it must follow the device list exactly."""
        conf = templates.render_gres_conf([("big-box", [f"/dev/nvidia{i}" for i in range(4)])])
        assert gres_lines(conf) == [f"NodeName=big-box Name=gpu File=/dev/nvidia{i}"
                                    for i in range(4)]

    def test_a_gappy_device_set_is_described_exactly(self, fake_host):
        """A box with a card pulled has no contiguous range to abbreviate. Separate lines say what is
        actually there; a bracket range would have to invent hostlist syntax for it."""
        conf = templates.render_gres_conf([("gappy", ["/dev/nvidia0", "/dev/nvidia2"])])
        assert gres_lines(conf) == ["NodeName=gappy Name=gpu File=/dev/nvidia0",
                                    "NodeName=gappy Name=gpu File=/dev/nvidia2"]

    def test_the_head_and_the_arms_share_one_file(self, monkeypatch, fake_host):
        """Configless serves ONE gres.conf to the whole fleet, so the Head's own co-located Arm and
        every donor are described in the same file — and the Head is the box most likely to be the
        multi-card one."""
        monkeypatch.setattr(system, "gpu_devices", lambda: ["/dev/nvidia0", "/dev/nvidia1"])
        conf = templates.render_gres_conf([templates.head_gres_node(), ("donor", ["/dev/nvidia0"])])
        assert gres_lines(conf) == ["NodeName=headbox Name=gpu File=/dev/nvidia0",
                                    "NodeName=headbox Name=gpu File=/dev/nvidia1",
                                    "NodeName=donor Name=gpu File=/dev/nvidia0"]

    def test_the_head_node_declares_all_of_its_cards(self, monkeypatch, fake_host):
        """slurm.conf's static NodeName line must agree with the gres.conf lines above, or slurmctld
        marks the node invalid and the Head's own Arm takes no work at all."""
        monkeypatch.setattr(system, "gpu_devices", lambda: [f"/dev/nvidia{i}" for i in range(4)])
        assert "Gres=gpu:4" in directives(templates.render_slurm_conf("100.99.0.1"))["NodeName"]


class TestCgroupIsolation:
    """What makes a GRES allocation real. Measured on the first remote job: `--gres=gpu:1` scheduled
    fine and `SLURM_JOB_GPUS` came back EMPTY — Slurm counted the card and never bound it. Both plugins
    plus ConstrainDevices are the fix, and each one is silent when missing: the job still runs, on a
    machine where every GPU is visible to it."""

    def test_conf_names_both_cgroup_plugins(self, fake_host):
        """task/cgroup is what puts a step in a device cgroup; proctrack/cgroup is its prerequisite.
        Either one back at its old value (task/none, proctrack/linuxproc) and isolation silently
        stops happening."""
        d = directives(templates.render_slurm_conf("100.99.0.1"))
        assert d["TaskPlugin"] == "task/cgroup"
        assert d["ProctrackType"] == "proctrack/cgroup"

    def test_devices_are_constrained(self):
        assert directives(templates.render_cgroup_conf())["ConstrainDevices"] == "yes"

    def test_nothing_else_is_constrained(self):
        """Deliberately devices-only. A Spawn declares --time and --vram — never a RAM or core budget —
        so a memory/CPU limit would be a number nobody set, enforced by an OOM kill."""
        d = directives(templates.render_cgroup_conf())
        assert "ConstrainRAMSpace" not in d
        assert "ConstrainCores" not in d

    def test_cgroup_version_is_autodetected(self):
        """One file is served to every node (configless has no per-node override), so it does not
        pin either cgroup version."""
        assert directives(templates.render_cgroup_conf())["CgroupPlugin"] == "autodetect"


class TestScratchEpilog:
    """slurmd reclaims a Spawn's node-local scratch after it ends, HOWEVER it ended — the reclaim the
    job script's own `trap ... EXIT` can't do, since SIGKILL (OOM, `scancel -9`, a Phase-2 Retract)
    bypasses the trap. The conf names the epilog; the node holds the script."""

    def test_conf_declares_the_epilog(self, fake_host):
        assert directives(templates.render_slurm_conf("100.99.0.1"))["Epilog"] \
            == str(config.SLURM_EPILOG)

    def test_epilog_removes_the_jobs_scratch_in_both_branches(self):
        """Both job-script branches: a remote Arm scratches in /tmp; the Head's co-located Arm under its
        state dir. One epilog runs on every node, so it targets both, keyed by the job id."""
        script = templates.render_epilog()
        assert 'rm -rf "/tmp/ccc-$SLURM_JOB_ID"' in script
        assert f'"{config.HEAD_SCRATCH_DIR}/$SLURM_JOB_ID"' in script

    def test_epilog_always_exits_zero(self):
        """A non-zero epilog DRAINS the node. A stale or already-gone scratch dir must never do that, so
        the script must end in an unconditional success."""
        assert templates.render_epilog().rstrip().endswith("exit 0")

    def test_epilog_reaps_sbatch_sinks_too(self):
        """The job script's own `trap ... EXIT` already reaps its empty sbatch sink — but that trap
        misses SIGKILL just like the scratch dir does (docs/TODO.md, sbatch wrapper litters /tmp).
        The epilog is the catch-all, so it must apply the same 'keep only if non-empty' rule to both
        the Spawn's and the benchmark's sink."""
        script = templates.render_epilog()
        assert '[ -s "$s" ] || rm -f "$s"' in script
        assert '[ -s "/tmp/ccc-bench-$SLURM_JOB_ID.log" ] || rm -f "/tmp/ccc-bench-$SLURM_JOB_ID.log"' in script

    def test_the_sink_sweep_matches_the_tag_the_epilog_cannot_derive(self):
        """A Spawn's sink carries a per-submission tag (ccc_head_api.spawns, `sink=`) precisely because
        a job id is not unique across teardowns. The epilog knows only the job id, so it has to glob —
        and must also match the untagged name older Spawns used."""
        script = templates.render_epilog()
        assert '"/tmp/ccc-$SLURM_JOB_ID.slurm" "/tmp/ccc-$SLURM_JOB_ID-"*.slurm' in script


class TestGuiseProlog:
    """The Slurm prolog: where a **Guise** comes into existence on a machine (see
    temp/spawn-isolation.md). It runs as root, on a donor's box, with `SLURM_JOB_UID` as its only
    input — so every failure here is a `useradd` running on someone else's machine with the
    wrong argument, or a node drained for a job it had no opinion about."""

    def test_the_account_is_named_after_the_uid_and_nothing_else(self):
        """The prolog has no network and no head.db. If the name came from anywhere but the uid it
        could not be formed at all — this is the whole reason a Guise is `ccc-u<uid>`."""
        script = templates.render_prolog()
        assert 'uid="$SLURM_JOB_UID"' in script
        assert f'guise="{config.GUISE_PREFIX}$uid"' in script
        assert config.guise_name(60002) == "ccc-u60002"

    def test_a_uid_outside_the_reserved_block_is_left_alone(self, ):
        """root, slurm, a donor's own login, the Mantle itself — all outside the block, none of them
        ours to create, and NONE of them a reason to fail the job."""
        script = templates.render_prolog()
        assert f'[ "$uid" -lt {config.GUISE_CLUSTER_UID} ]' in script
        assert f'[ "$uid" -gt {config.GUISE_UID_MAX} ]' in script
        assert config.CCC_UID < config.GUISE_CLUSTER_UID   # the Mantle is below the block

    def test_a_uid_held_by_a_stranger_fails_the_job(self):
        """The one case the prolog refuses. Running a Spawn as an account CCC did not create is worse
        than draining the node, because it would run a User's payload as a donor's own login."""
        script = templates.render_prolog()
        assert 'elif [ "$existing" != "$guise" ]; then' in script
        # The refusal branch, not the "created it" branch, is what exits non-zero.
        refusal = script.split('elif [ "$existing" != "$guise" ]; then')[1].split("fi")[0]
        assert "exit 1" in refusal

    def test_the_account_is_homeless_and_tagged(self):
        """`-M` so a donor's box gains no directory; the GECOS tag so `teardown --arm` can find exactly
        these accounts again — they are named after uids, so nothing else distinguishes them."""
        script = templates.render_prolog()
        assert "--no-create-home" in script
        assert f'--comment "{config.GUISE_GECOS}"' in script
        assert "--remove" not in script   # nothing of the donor's may be in the blast radius

    def test_it_always_exits_zero_on_the_ordinary_path(self):
        """Same rule as the epilog: a non-zero prolog drains the node. Only the two real failures
        (useradd failed, the uid is a stranger's) may do that."""
        assert templates.render_prolog().rstrip().endswith("exit 0")

    def test_the_head_only_lending_is_guarded_by_the_directory(self):
        """The Spawn/scratch dirs exist only on the Head's co-located Arm. On a donor Arm both branches
        must no-op rather than create a directory tree CCC has no business making there."""
        script = templates.render_prolog()
        assert f'if [ -d "{config.HEAD_SPAWNS_DIR}" ]; then' in script
        assert f'if [ -d "{config.HEAD_SCRATCH_DIR}" ]; then' in script

    def test_the_epilog_gives_the_spawn_dir_back(self):
        """Prolog lends, epilog returns — so the grant lasts exactly the job, and survives a SIGKILL
        that the job script's own trap does not. Without this a User's LATER Spawn could rewrite an
        earlier one's recorded exit code, since both run as the same Guise."""
        epilog = templates.render_epilog()
        assert f'chown -R {config.HEAD_API_USER}:{config.HEAD_API_USER} ' \
               f'"{config.HEAD_SPAWNS_DIR}/$SLURM_JOB_ID"' in epilog
        # ...and still never drains the node over a failed give-back.
        assert epilog.rstrip().endswith("exit 0")


class TestMigHooks:
    """A MIG-mode card's resting state is ONE full-size instance, because CCC prices whole cards and
    has no **Beak** to carve for placement (CONTEXT.md, "Slice"). The prolog hands a Spawn that state;
    the epilog puts it back after whatever the Spawn did to it.

    Measured on hardware: MIG mode `Enabled` with nothing carved is a GPU with *no CUDA device at all*,
    which is what dex's benchmark died of — an assert in the Pod, five seconds in, with the traceback
    destroyed alongside the job's private /tmp.
    """

    def test_the_epilog_resets_and_the_prolog_only_repairs(self):
        """Two different jobs. Resetting at the end is what keeps an *idle* box honest about what it
        holds; repairing at the start is only for the times the epilog never ran — a rebooted node, a
        killed slurmd — and for the first Spawn onto a card whose owner enabled MIG and never carved."""
        prolog, epilog = templates.render_prolog(), templates.render_epilog()
        assert '[ "$(ccc_mig_count "$uuid")" = 1 ] || ccc_mig_reset "$uuid"' in prolog
        assert 'ccc_mig_enabled "$uuid" && ccc_mig_reset "$uuid"' in epilog

    def test_the_caps_are_opened_for_the_held_cards_and_closed_again(self):
        """Creating an instance as root leaves its capability node 0400 root — so a **Guise** cannot
        open the MIG device Slurm just allocated it. Opening these is not the carving feature, it is
        what makes MIG work at all. Held cards only, and given back at the end like the Spawn dir."""
        assert 'ccc_mig_caps "$minor" 0440' in templates.render_prolog()
        assert 'ccc_mig_caps "$minor" 0400' in templates.render_epilog()
        for script in (templates.render_prolog(), templates.render_epilog()):
            assert 'for minor in $(ccc_held_minors); do' in script
            assert f'CCC_GUISE_GROUP={config.GUISE_GROUP}' in script

    def test_the_global_config_cap_is_opened_but_never_closed(self):
        """`nvidia-cap1` is ONE node for the whole box, not one per card. Closing it when a Spawn ends
        would take carving away from a concurrent Spawn still running on the other card — a bug two
        cards make reachable and a one-card box hides. Safe to leave: config alone reaches no card,
        since the per-GPU access caps are what a MIG device is opened through and ConstrainDevices
        still decides which /dev/nvidiaN a Pod may touch."""
        assert "ccc_mig_cap_config" in templates.render_prolog()
        epilog = templates.render_epilog()
        # Defined (it rides the shared block) but never CALLED there.
        assert "ccc_mig_cap_config()" in epilog
        assert not re.search(r"^\s*ccc_mig_cap_config$", epilog, re.M)

    def test_a_whole_card_arm_is_untouched(self):
        """The fleet is mixed and always will be. Every branch here hangs off `ccc_mig_enabled`, so a
        node with no MIG-capable card runs the hooks exactly as it did before any of this existed."""
        for script in (templates.render_prolog(), templates.render_epilog()):
            assert "ccc_mig_enabled" in script
            assert "[ -d /proc/driver/nvidia-caps ] || return 0" in script
        assert 'ccc_mig_enabled "$uuid" || continue' in templates.render_prolog()

    def test_neither_hook_can_drain_a_node_over_a_card(self):
        """A non-zero prolog drains the node, and a drained node stays drained across a slurmd restart.
        A stale MIG instance must never cost the cluster the whole Arm."""
        for script in (templates.render_prolog(), templates.render_epilog()):
            assert script.rstrip().endswith("exit 0")

    def test_the_full_card_profile_is_named_once(self):
        """`4g.96gb` is ID 0 on an RTX PRO 6000. The `+me`/`-me`/`+gfx` spellings exist only on the 1g
        and 2g profiles, so there is no suffixed variant of the full card to pick between."""
        for script in (templates.render_prolog(), templates.render_epilog()):
            assert "CCC_MIG_FULL_PROFILE=0" in script
            assert '-cgi $CCC_MIG_FULL_PROFILE -C' in script


class TestPrologRollout:
    """`Prolog=` is the one fleet-wide setting that punishes being early: an Arm named by it without
    the script fails its jobs and drains, and *stays* drained across a slurmd restart — so putting the
    file there afterwards does not heal it. The Head refuses to name it until every Arm has it."""

    def test_the_line_is_absent_while_any_arm_is_missing_it(self, fake_host):
        conf = templates.render_slurm_conf("100.99.0.1", missing_prolog=["willpersonal"])
        assert "Prolog" not in directives(conf)

    def test_the_arms_holding_it_up_are_named_in_the_file(self, fake_host):
        """The operator's next move is to go and re-run `setup --arm` on those boxes, so the file has
        to say which."""
        conf = templates.render_slurm_conf("100.99.0.1", missing_prolog=["willpersonal", "spare"])
        assert "willpersonal" in conf and "spare" in conf

    def test_the_line_appears_once_the_fleet_is_ready(self, fake_host):
        conf = templates.render_slurm_conf("100.99.0.1", missing_prolog=[])
        assert directives(conf)["Prolog"] == str(config.SLURM_PROLOG)

    def test_an_arm_that_has_not_reported_holds_it_up(self, monkeypatch, tmp_path):
        reg = tmp_path / "arms.json"
        reg.write_text(json.dumps({
            "ready": {"admitted": True, "provides": ["prolog"]},
            "stale": {"admitted": True, "provides": []},
            "old": {"admitted": True},                       # never sent the field at all
            "unadmitted": {"admitted": False},               # takes no placement — not a blocker
            "headbox": {"admitted": True, "head": True},     # never enrolls; setup writes its own
        }))
        monkeypatch.setattr(config, "ARMS_REGISTRY", reg)
        assert enroll.arms_missing("prolog") == ["old", "stale"]

    def test_a_retracted_arm_still_holds_it_up(self, monkeypatch, tmp_path):
        """Retraction lives in head.db, which this side never reads — and a Retract can be Returned at
        any moment, which would put a prolog-less node straight back into the placement pool."""
        reg = tmp_path / "arms.json"
        reg.write_text(json.dumps({"paused": {"admitted": True, "provides": []}}))
        monkeypatch.setattr(config, "ARMS_REGISTRY", reg)
        assert enroll.arms_missing("prolog") == ["paused"]


class TestNothingIsLeftInTmp:
    """`install` COPIES, so every root-owned file this project writes leaves its source behind
    unless something removes it. Four sites didn't, two writing secrets — the [bench] rsync
    credential and, on an Arm, the cluster munge key — both 0600/0400 at their destination but
    left in /tmp indefinitely, defeating the mode each was given. (855 such files can accumulate
    on an unpatched Head.)"""

    def test_the_temp_copy_does_not_outlive_the_install(self, monkeypatch):
        seen = []
        monkeypatch.setattr(system, "run", lambda argv, **kw: seen.append(argv[-2]))
        system.install_file("some rendered config", Path("/etc/ccc/thing"))
        assert seen and not Path(seen[0]).exists()

    def test_it_is_removed_even_when_the_install_fails(self, monkeypatch):
        """The case that matters most: a bad path, a full disk or a sudo timeout is exactly when the
        temp file is likeliest to be holding something worth not leaving lying around."""
        seen = []

        def boom(argv, **kw):
            seen.append(argv[-2])
            raise subprocess.CalledProcessError(1, argv)

        monkeypatch.setattr(system, "run", boom)
        with pytest.raises(subprocess.CalledProcessError):
            system.install_file("ccc-bench:a-real-looking-secret",
                                config.RSYNCD_BENCH_SECRETS, mode="0600")
        assert seen and not Path(seen[0]).exists()

    def test_binary_content_survives_the_round_trip(self, monkeypatch):
        """The munge key is bytes, not text. Routing it through a text-mode temp file would corrupt
        the cluster's shared secret rather than merely leak it."""
        written = []
        monkeypatch.setattr(system, "run",
                            lambda argv, **kw: written.append(Path(argv[-2]).read_bytes()))
        key = bytes(range(256))
        system.install_file(key, config.MUNGE_KEY, mode="0400", owner="munge", group="munge")
        assert written == [key]

    def test_ownership_is_passed_through_only_when_asked_for(self, monkeypatch):
        """setup's own renders take the root default; the registry and the munge key name a user."""
        argvs = []
        monkeypatch.setattr(system, "run", lambda argv, **kw: argvs.append(list(argv)))
        system.install_file("x", Path("/etc/ccc/plain"))
        system.install_file("x", Path("/etc/ccc/owned"), owner="munge", group="munge")
        assert "-o" not in argvs[0]
        assert argvs[1][argvs[1].index("-o") + 1] == "munge"


class TestPrivateTmpRollout:
    """`job_container/tmpfs` is what contains the WRAPPER around a Spawn's payload — without it a
    remote Spawn's scratch sits in the host's shared /tmp beside every concurrent Spawn's. It
    carries the same drain-the-node hazard as the prolog, and the same gate, with its OWN capability."""

    def test_both_lines_are_absent_while_any_arm_is_missing_the_conf(self, fake_host):
        d = directives(templates.render_slurm_conf("100.99.0.1", missing_tmpfs=["willpersonal"]))
        assert "JobContainerType" not in d and "PrologFlags" not in d

    def test_the_arms_holding_it_up_are_named_in_the_file(self, fake_host):
        conf = templates.render_slurm_conf("100.99.0.1", missing_tmpfs=["willpersonal", "spare"])
        assert "willpersonal" in conf and "spare" in conf

    def test_both_lines_appear_once_the_fleet_is_ready(self, fake_host):
        """PrologFlags=Contain is not optional decoration — the plugin refuses to work without it
        ("'PrologFlags=Contain' must be set to use this plugin", man 5 slurm.conf), so a render that
        turned one on without the other would be a container that never contains anything."""
        d = directives(templates.render_slurm_conf("100.99.0.1", missing_tmpfs=[]))
        assert d["JobContainerType"] == "job_container/tmpfs"
        assert d["PrologFlags"] == "Contain"

    def test_an_arm_without_the_conf_does_not_cost_the_fleet_its_guises(self, fake_host):
        """The reason tmpfs has a capability of its own: an Arm that only ever reported
        `provides: ["prolog"]` would lose its working Guises the moment a shared gate started
        requiring tmpfs too — silently putting every Spawn back to running as the Mantle."""
        d = directives(templates.render_slurm_conf("100.99.0.1", missing_tmpfs=["oldarm"]))
        assert d["Prolog"] == str(config.SLURM_PROLOG)
        assert "JobContainerType" not in d

    def test_the_reverse_holds_too(self, fake_host):
        d = directives(templates.render_slurm_conf("100.99.0.1", missing_prolog=["newarm"]))
        assert d["JobContainerType"] == "job_container/tmpfs"
        assert "Prolog" not in d

    def test_the_two_capabilities_are_asked_about_separately(self, monkeypatch, tmp_path):
        reg = tmp_path / "arms.json"
        reg.write_text(json.dumps({
            "ready": {"admitted": True, "provides": ["prolog", "tmpfs"]},
            "phase3only": {"admitted": True, "provides": ["prolog"]},
        }))
        monkeypatch.setattr(config, "ARMS_REGISTRY", reg)
        assert enroll.arms_missing(config.ARM_CAP_PROLOG) == []
        assert enroll.arms_missing(config.ARM_CAP_TMPFS) == ["phase3only"]


class TestTheFleetActuallyGetsTheNewConfig:
    """Rendering a fleet-wide line and DELIVERING it are two different things. A configless
    slurmd caches the copy it fetched and re-fetches only on its own startup or a reconfigure —
    a slurmctld restart is neither. `Prolog=` reaches an Arm only when the deploy happens to
    include an `arm admit`, which reconfigures for its own reasons (enroll.render_gres). Without
    this the same silence would swallow any future fleet-wide config line."""

    @staticmethod
    def _daemons(monkeypatch, registry=None):
        """`start_daemons`, with every command it issues collected in one list. Both modules are
        patched: the reconfigure itself lives in `enroll` now (see enroll.reconfigure)."""
        from cuttle import enroll, setup
        ran = []
        monkeypatch.setattr(setup, "run", lambda argv, **kw: ran.append(list(argv)))
        monkeypatch.setattr(enroll, "run", lambda argv, **kw: ran.append(list(argv)))
        monkeypatch.setattr(setup, "ensure_munge_key", lambda: None)
        monkeypatch.setattr(enroll, "load_registry", lambda: registry or {})
        setup.start_daemons()
        return ran

    def test_setting_up_the_head_pushes_the_config_to_the_arms(self, monkeypatch):
        assert ["scontrol", "reconfigure"] in self._daemons(monkeypatch)

    def test_it_comes_after_the_restart_not_before(self, monkeypatch):
        """Order is the point: reconfigure tells the daemons to re-read, so a reconfigure issued
        before slurmctld has the new file on disk pushes the OLD one to every Arm."""
        ran = self._daemons(monkeypatch)
        restart = next(i for i, c in enumerate(ran) if c[:2] == ["systemctl", "restart"])
        assert ran.index(["scontrol", "reconfigure"]) > restart

    def test_every_admitted_arms_weight_is_re_pushed_behind_it(self, monkeypatch):
        """A reconfigure resets every DYNAMIC node's Weight to 1, and donor Arms are dynamic — so a
        routine `setup --head` used to silently degrade scheduling to registration order. The
        re-push has to follow the reconfigure, not precede it."""
        reg = {"donor": {"admitted": True, "powers": [40.0], "gpu_devices": ["/dev/nvidia0"]},
               "unadmitted": {"admitted": False, "powers": [40.0], "gpu_devices": []}}
        ran = self._daemons(monkeypatch, reg)
        pushed = [c for c in ran if c[:1] == ["scontrol"] and "update" in c]
        assert [c for c in pushed if "NodeName=donor" in c]
        assert not [c for c in pushed if "NodeName=unadmitted" in c]
        assert ran.index(pushed[0]) > ran.index(["scontrol", "reconfigure"])

    def test_every_admitted_arms_features_are_re_pushed_too(self, monkeypatch):
        """The same reset, on the attribute where it costs placement rather than card order. A donor
        Arm's Features come from slurmd's `--conf` and no config file mentions a dynamic node, so a
        reconfigure leaves slurmctld nothing to restore them from — while the registry keeps showing
        them, so the Dashboard and the feasibility check both still say the Arm carries the Feature."""
        reg = {"donor": {"admitted": True, "vram_mb": 24576, "gpu_devices": ["/dev/nvidia0"],
                         "features": [{"name": "vsim", "path": "/opt/vsim"}]}}
        ran = self._daemons(monkeypatch, reg)
        pushed = [c for c in ran if c[:2] == ["scontrol", "update"] and "NodeName=donor" in c]
        assert ["AvailableFeatures=vram24,vsim", "ActiveFeatures=vram24,vsim"] == \
            [a for c in pushed for a in c if "Features=" in a]
        assert ran.index(pushed[0]) > ran.index(["scontrol", "reconfigure"])

    def test_the_vram_bucket_rides_along_with_the_declared_features(self, monkeypatch):
        """`scontrol update` takes the feature set whole, so a re-push naming only the Arm Features
        would restore those by dropping the node out of `--vram` matching — trading one placement
        failure for another. An Arm declaring nothing still has its bucket re-stated."""
        reg = {"bare": {"admitted": True, "vram_mb": 49152, "gpu_devices": ["/dev/nvidia0"]}}
        ran = self._daemons(monkeypatch, reg)
        assert any("AvailableFeatures=vram48" in c for c in ran)


    def test_an_arm_not_yet_registered_does_not_fail_the_re_push(self, monkeypatch):
        """First admit: the Arm's slurmd starts only after it, so slurmctld has no such node yet and
        `scontrol update` exits 1. That must not abort the admit — the node brings its own Features."""
        from cuttle import enroll
        calls = []
        monkeypatch.setattr(enroll, "run", lambda cmd, **kw: calls.append(kw))
        enroll.reapply_features({"new": {"admitted": True, "vram_mb": 24576, "gpu_devices": ["/dev/dxg"]}})
        assert calls and all(kw.get("check") is False for kw in calls)

class TestJobContainerConf:
    """The per-node file the plugin refuses to start without — its contents, and its lifetime."""

    def test_an_arm_writes_it_before_its_slurmd_starts(self, monkeypatch, fake_host):
        """The whole ordering the rollout rests on. slurmd reads job_container.conf at startup, so a
        node that starts first and gets the file second runs uncontained until someone notices — and
        the /enroll POST that follows would claim otherwise."""
        from cuttle import setup
        events = []
        monkeypatch.setattr(setup, "_write_root_file",
                            lambda p, *a, **kw: events.append(("write", str(p))))
        monkeypatch.setattr(setup, "run",
                            lambda argv, **kw: events.append(("run", " ".join(map(str, argv)))))
        setup._start_slurmd("donor-box", "100.99.0.1:6817")
        wrote = events.index(("write", str(config.JOB_CONTAINER_CONF)))
        started = next(i for i, (kind, v) in enumerate(events)
                       if kind == "run" and v == "systemctl restart slurmd")
        assert wrote < started

    def test_a_departing_donor_keeps_no_mount_tree(self, monkeypatch):
        """AutoBasePath creates BasePath and "the directory is not deleted during slurm shutdown", so
        nothing but teardown will ever reclaim it. Every caller stops slurmd first — it holds BasePath
        mounted `private` for as long as it runs."""
        from cuttle import teardown
        calls = []
        monkeypatch.setattr(teardown, "run", lambda argv, **kw: calls.append(list(argv)))
        teardown.remove_slurm_config()
        removed = [a for c in calls if c[:2] == ["rm", "-rf"] for a in c[2:]]
        assert str(config.JOB_CONTAINER_ROOT) in removed
        # The tree, not one node's directory — `%h` is expanded by slurmd, never by us.
        assert config.JOB_CONTAINER_BASE not in removed

    def test_slurmd_creates_the_base_path_itself(self):
        """AutoBasePath=false (the default) means the plugin EXPECTS the directory to exist — and a
        BasePath that isn't there is another drain, on a directory nothing else would ever create."""
        assert "AutoBasePath=true" in templates.render_job_container_conf()

    def test_the_base_path_is_per_node(self):
        """"The BasePath must be unique to each node" (man 5 job_container.conf). slurmd expands %h to
        its own hostname; the Head never learns or writes which node it is."""
        assert f"BasePath={config.JOB_CONTAINER_ROOT}/%h" in templates.render_job_container_conf()

    def test_dev_shm_is_left_to_the_default(self):
        """Naming `Dirs` at all is how this gets broken. The default is "/tmp,/dev/shm", and /dev/shm
        "has special handling, and instead of a bind mount is always a fresh tmpfs filesystem" — so
        shared-memory IPC (a torch DataLoader's workers, above all) keeps a RAM-backed filesystem
        rather than landing on BasePath's disk. Only /tmp is the bind mount."""
        assert "Dirs" not in templates.render_job_container_conf()

    def test_it_lives_beside_slurm_conf(self):
        """Not a choice: "the file will always be located in the same directory as the slurm.conf"."""
        assert config.JOB_CONTAINER_CONF.parent == config.SLURM_CONF.parent


class TestSlurmSudoers:
    """The Head's ONE unattended privileged grant. What it is NOT is the whole point."""

    def test_the_runas_is_the_guise_group_never_root(self):
        rule = headapi._SUDOERS_CCC_SLURM
        assert f"({config.HEAD_API_USER} ALL=(%{config.GUISE_GROUP})" not in rule  # sanity on shape
        assert f"ALL=(%{config.GUISE_GROUP})" in rule
        assert "ALL=(root)" not in rule and "ALL=(ALL)" not in rule

    def test_it_grants_exactly_the_three_slurm_clients_and_nothing_else(self):
        """Slurm's verbs and nothing else. `apptainer` used to be a fourth, granted while the Head built
        each Spawn's **Environment** inside base.sif as the submitter's Guise; the Arm builds it now, in
        its own job, so the Head API runs no User's code and needs no way into a Pod.

        No shell, no root, and Runas is still the Guise group — widening this set is exactly what this
        test exists to notice."""
        rule = headapi._SUDOERS_CCC_SLURM
        cmds = rule.split("NOPASSWD:")[1]
        assert set(c.strip() for c in cmds.replace("\\\n", "").split(",")) == {
            "/usr/bin/sbatch", "/usr/bin/scancel", "/usr/bin/scontrol"}

class TestSlurmdDropin:
    """A donor Arm's slurmd flags. The node is *dynamic*, so this string is re-sent to slurmctld on
    every single registration — it is the only durable home for the node's Feature."""

    def test_conf_is_quoted_as_one_argument(self):
        """The landmine. Ubuntu's unit is `ExecStart=/usr/sbin/slurmd --systemd $SLURMD_OPTIONS`, and
        systemd splits a variable's value at whitespace WITHOUT honouring quotes inside it. Via that
        hook, `--conf "Gres=gpu:1 Feature=vram24"` reaches slurmd as three mangled arguments. Quotes
        written literally in a unit file ARE honoured — hence a drop-in that states the command."""
        unit = enroll.slurmd_dropin("donor-box", "100.99.0.1:6817", 24564, 1)
        assert '--conf "Gres=gpu:1 Feature=vram24"' in unit
        assert "SLURMD_OPTIONS" not in unit  # never go back to the /etc/default hook

    def test_resets_the_distro_execstart(self):
        """A drop-in APPENDS to ExecStart unless you clear it first. Without the bare `ExecStart=`,
        systemd would refuse the unit (two ExecStart lines on a Type=notify service)."""
        unit = enroll.slurmd_dropin("donor-box", "100.99.0.1:6817", 24564, 1)
        lines = [ln for ln in unit.splitlines() if ln.startswith("ExecStart")]
        assert lines[0] == "ExecStart="
        assert lines[1].startswith("ExecStart=/usr/sbin/slurmd")
        assert len(lines) == 2

    def test_dynamic_registration_flags_survive(self):
        unit = enroll.slurmd_dropin("donor-box", "100.99.0.1:6817", 24564, 1)
        assert "-Z -N donor-box" in unit           # register as a dynamic node, under this name
        assert "--conf-server 100.99.0.1:6817" in unit  # configless: fetch slurm.conf from slurmctld

    def test_feature_tracks_the_card(self):
        assert "Feature=vram80" in enroll.slurmd_dropin("a", "h:1", 81920, 1)
        assert "Feature=vram8" in enroll.slurmd_dropin("a", "h:1", 8192, 1)

    def test_gres_counts_every_card(self):
        """The whole point of multi-GPU registration. A dynamic node's `--conf` is re-sent at EVERY
        registration and is the only thing that declares its GRES count — understate it and three of a
        donor's four cards are simply invisible to the cluster forever."""
        assert '--conf "Gres=gpu:4 Feature=vram80"' in \
            enroll.slurmd_dropin("donor-box", "h:1", 81920, 4)

    def test_arm_features_join_the_vram_bucket(self):
        """Arm Features (docs/TODO.md) ride the SAME Feature= a Spawn's --vram already matches against
        — a Slurm Feature is a flat per-node set, so there is no structural difference between them
        once rendered. Without this, a re-registration (this string is re-sent every time) would drop
        whatever the donor declared at `setup --arm` and every requiring Spawn would stop matching."""
        unit = enroll.slurmd_dropin("donor-box", "h:1", 24564, 1, ["vsim", "isaacsim"])
        assert "Feature=vram24,isaacsim,vsim" in unit  # sorted, so re-registration is deterministic

    def test_no_features_is_unchanged(self):
        """Backward compatible: an Arm with none declared renders exactly as it did before this
        existed — no trailing comma, no empty token."""
        assert "Feature=vram24" in enroll.slurmd_dropin("donor-box", "h:1", 24564, 1)
        assert "Feature=vram24," not in enroll.slurmd_dropin("donor-box", "h:1", 24564, 1)


BENCH_IMG = "ccc.glados:5000/ccc/bench@sha256:" + "c" * 64


def bench(node="donor-box", gpus=1):
    return templates.render_bench_sbatch(node, "100.99.0.1", "b3nch", BENCH_IMG, gpus)


class TestBenchmarkSbatch:
    """The Benchmark: submitted by the Head as the cluster Guise, pinned to the node being priced,
    run in the benchmark image through ccc-launch."""

    def test_pinned_to_the_node_under_test(self):
        job = bench()
        assert "#SBATCH --nodelist=donor-box" in job
        assert "#SBATCH --gres=gpu:1" in job

    def test_one_job_holds_every_card(self):
        """Power is per Device, from ONE job: separate jobs could land while a sibling card is busy."""
        job = bench("big-box", 4)
        assert "#SBATCH --gres=gpu:4" in job
        assert len([ln for ln in job.splitlines() if ln.startswith("#SBATCH --gres")]) == 1

    def test_it_runs_through_ccc_launch_with_its_spec(self):
        job = bench()
        assert f'sudo -n {config.CCC_LAUNCH_BIN} "$SLURM_JOB_ID"' in job
        spec = ccc_launch.validate_spec(ccc_launch.read_spec(job))
        assert spec["image"] == BENCH_IMG
        assert spec["argv"] == config.BENCH_ARGV

    def test_launcher_constants_agree(self):
        """ccc_launch.py runs under the system python and imports nothing of CCC's."""
        assert str(config.JOBS_DIR) == str(ccc_launch.JOBS_DIR)
        assert str(config.REGISTRY_AUTH) == ccc_launch.AUTH_FILE
        assert config.REGISTRY == ccc_launch.REGISTRY
        assert config.JOB_CONTAINER_PREFIX == ccc_launch.CONTAINER_PREFIX
        assert config.GUISE_UID_MAX == ccc_launch.UID_CEIL and config.CCC_UID == ccc_launch.UID_FLOOR

    def test_result_comes_home_by_rsync(self):
        """The power file lands in the job dir (handed back to the Guise by ccc-launch) and goes home
        over the [bench] module — the same path on a donor's box and on the Head's own Arm."""
        job = bench()
        assert [ln for ln in job.splitlines() if ln.startswith("rsync ")] == \
               [f'rsync -a "{config.JOBS_DIR}/$SLURM_JOB_ID/power" "rsync://'
                f'{config.RSYNC_BENCH_USER}@100.99.0.1/{config.RSYNC_MODULE_BENCH}/donor-box/"']

    def test_it_authenticates_to_the_bench_module(self):
        job = bench()
        assert "export RSYNC_PASSWORD=b3nch" in job
        assert str(config.HEAD_SPAWNS_DIR) not in job   # the bench tree only


class TestBenchPy:
    def test_embedded_python_is_intact(self):
        """str.format()-ed with a dict literal inside — every brace doubled, or it won't parse."""
        body = templates.render_bench_py()
        compile(body, "bench.py", "exec")
        assert re.search(r"\{\w+\}", body) is None, "an unrendered {placeholder} survived"
        assert repr(config.BENCH_BIN) in body

    def test_warmup_is_not_timed(self):
        cu = templates.BENCH_CU
        assert cu.index("i < BENCH_WARMUP") < cu.index("steady_clock::now()") < cu.index("i < BENCH_ITERS")

    def test_one_process_per_device_in_gres_order(self, tmp_path, monkeypatch):
        """One bench process per CCC_GPUS entry with CUDA_VISIBLE_DEVICES narrowed to it (a MIG
        instance is the only device its process can address), then merged in GRES order."""
        calls = []

        def fake_run(argv, check, capture_output, text, env):
            calls.append((argv, env["CUDA_VISIBLE_DEVICES"]))
            return subprocess.CompletedProcess(
                argv, 0, '{"power": %s, "vram_mb": 1, "gpu": "g"}' % (10 + len(calls) - 1))
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setenv("CCC_GPUS", "GPU-a,MIG-b")
        monkeypatch.setenv("CCC_BENCH_OUT", str(tmp_path))
        exec(compile(templates.render_bench_py(), "bench.py", "exec"), {"__file__": "bench.py"})
        assert calls == [([config.BENCH_BIN], "GPU-a"), ([config.BENCH_BIN], "MIG-b")]
        assert json.loads((tmp_path / "power").read_text())["powers"] == [10, 11]


class TestFeaturesEnv:
    """`/etc/ccc/features.env` — what an Arm tells its own Spawns about the software it carries.

    The Head cannot bake any of this into the job script it renders for the whole fleet: every value
    here is Arm-local, chosen by that box's owner. So the Head renders the *decision* (may this Spawn
    see a licence?) and the Arm supplies the *paths*.
    """

    def test_a_feature_becomes_an_exported_path(self):
        """`isaacsim` has no profile, so nothing is mirrored and the owner's own path is what a Spawn
        is told. A Feature CCC does mirror gets CCC's copy instead — see tests/test_features.py, which
        is where that half lives."""
        out = enroll.render_features_env([{"name": "isaacsim", "path": "/opt/isaacsim"}])
        assert "export ISAACSIM_PATH=/opt/isaacsim" in out

    def test_a_dotted_or_hyphenated_name_becomes_a_legal_identifier(self):
        """FEATURE_NAME_RE allows `.` and `-`; a shell identifier does not, so `vsim-pro` has to become
        VSIM_PRO_PATH or sourcing this file is a syntax error that breaks every Spawn on the node."""
        out = enroll.render_features_env([{"name": "vsim-pro.2", "path": "/opt/v"}])
        assert "export VSIM_PRO_2_PATH=/opt/v" in out

    def test_the_licence_home_is_one_per_arm_not_one_per_feature(self):
        """A licence is locked to hardware and an Arm is one machine, so two Features can never collide
        over it — there is exactly one exported name no matter how many Features are declared."""
        out = enroll.render_features_env(
            [{"name": "vlearn", "path": "/opt/v"}, {"name": "isaacsim", "path": "/opt/i"}],
            licence_home="/opt/ccc/licence")
        assert out.count(f"export {config.LICENCE_HOME_VAR}=") == 1
        assert f"export {config.LICENCE_HOME_VAR}=/opt/ccc/licence" in out

    def test_an_arm_with_no_licence_exports_none(self):
        """Absent, not empty: the job script guards on `[ -d "${CCC_LICENCE_HOME:-}" ]`, so an
        exported empty string and an unset var behave alike — but only one of them says nothing."""
        out = enroll.render_features_env([{"name": "vlearn", "path": "/opt/v"}])
        assert config.LICENCE_HOME_VAR not in out

    def test_everything_is_exported_not_merely_assigned(self):
        """The job script sources this with no `set -a`, and apptainer passes the calling environment in
        (no --cleanenv) — so a bare assignment would reach the wrapper and stop at the Pod wall."""
        out = enroll.render_features_env([{"name": "vlearn", "path": "/opt/v"}],
                                         licence_home="/opt/ccc/licence")
        assert all(ln.startswith("export ") for ln in out.splitlines() if ln and not ln.startswith("#"))


class TestArmNameIsLowercaseOnly:
    """Headscale normalizes a node's `given_name` to DNS rules, which lowercases it — so an uppercase
    Arm name is one CCC writes to the registry, hands to Headscale, and then cannot recognise coming
    back (`ccc_head_api.enroll._node_named_at`). Refused at `arm authorize`, before a single-use
    preauth key is burned on a name that could never have worked."""

    def test_an_uppercase_name_is_refused_before_anything_is_spent(self, monkeypatch):
        called = []
        monkeypatch.setattr(enroll.auth, "user_exists", lambda u: called.append(u) or True)
        with pytest.raises(Exception):
            enroll._authorize_arm("willPC", "will", lambda *a, **k: None)
        assert called == [], "the owner lookup ran before the name was even validated"

    def test_the_lowercase_form_gets_past_the_name_check(self, monkeypatch):
        """Past the NAME gate specifically — it still aborts on the (absent) owner, which is the next
        check and proves this one let it through."""
        seen = []
        monkeypatch.setattr(enroll.auth, "user_exists", lambda u: seen.append(u) or False)
        with pytest.raises(Exception):
            enroll._authorize_arm("willpc", "will", lambda *a, **k: None)
        assert seen == ["will"]

    def test_the_registry_assert_still_accepts_a_legacy_uppercase_row(self):
        """config.NODE_NAME_SAFE_RE, not NODE_NAME_RE: an Arm admitted before the rule tightened must
        still render into gres.conf, or the upgrade breaks the very `arm remove` that retires it."""
        assert re.match(config.NODE_NAME_SAFE_RE, "willPC")
        assert not re.match(config.NODE_NAME_RE, "willPC")
