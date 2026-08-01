"""Phase 4 slice 0 — what setup and enrollment actually WRITE to disk.

Every failure guarded here is silent at write time and only surfaces on a donor's box, minutes later,
as a node that registered but can't be scheduled. That is exactly the class of bug worth a unit test:
the render is pure, and the consequence is remote.
"""

import json
import re

import pytest

from cuttle import config, enroll, headapi, system, templates


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
        """Weight IS rendered here — the opposite of what slice 0 decided, and the reversal is the
        point. `scontrol reconfigure` resets every node to what the config files say, so a Weight
        pushed only by `scontrol update` is wiped by the next `arm authorize`/`admit`/`remove`
        (verified on hardware 2026-07-14: update -> 276, reconfigure -> 1). The file is the only
        durable home a *static* node has."""
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

    def test_a_wsl_arm_gets_exactly_one_dxg_line(self, fake_host):
        """Permanent, not deferred: /dev/dxg is one device node for ALL of a Windows box's cards and a
        device cgroup cannot partition it, so a WSL Arm is one Device however many GPUs it holds."""
        conf = templates.render_gres_conf([("wsl-box", ["/dev/dxg"])])
        assert gres_lines(conf) == ["NodeName=wsl-box Name=gpu File=/dev/dxg"]

    def test_the_head_and_the_arms_share_one_file(self, monkeypatch, fake_host):
        """Configless serves ONE gres.conf to the whole fleet, so the Head's own co-located Arm and
        every donor are described in the same file — and the Head is the box most likely to be the
        multi-card one."""
        monkeypatch.setattr(system, "gpu_devices", lambda: ["/dev/nvidia0", "/dev/nvidia1"])
        conf = templates.render_gres_conf([templates.head_gres_node(), ("donor", ["/dev/dxg"])])
        assert gres_lines(conf) == ["NodeName=headbox Name=gpu File=/dev/nvidia0",
                                    "NodeName=headbox Name=gpu File=/dev/nvidia1",
                                    "NodeName=donor Name=gpu File=/dev/dxg"]

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
        """The fleet is mixed: native Ubuntu 24.04 is cgroup v2, a WSL Arm presents a fake v1 `tmpfs`.
        One file is served to every node (configless has no per-node override), so it cannot pin
        either version."""
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
        assert '[ -s "/tmp/ccc-$SLURM_JOB_ID.slurm" ] || rm -f "/tmp/ccc-$SLURM_JOB_ID.slurm"' in script
        assert '[ -s "/tmp/ccc-bench-$SLURM_JOB_ID.log" ] || rm -f "/tmp/ccc-bench-$SLURM_JOB_ID.log"' in script


class TestGuiseProlog:
    """The Slurm prolog: where a **Guise** comes into existence on a machine (temp/spawn-isolation.md
    Phase 3c). It runs as root, on a donor's box, with `SLURM_JOB_UID` as its only input — so every
    failure here is a `useradd` running on someone else's machine with the wrong argument, or a node
    drained for a job it had no opinion about."""

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


class TestTheFleetActuallyGetsTheNewConfig:
    """Rendering a fleet-wide line and DELIVERING it are two different things, and for the whole life
    of this project only the first one happened. A configless slurmd caches the copy it fetched and
    re-fetches on its own startup or on a reconfigure — a slurmctld restart is neither. `Prolog=`
    reached willpersonal only because the deploy happened to include an `arm admit`, which reconfigures
    for its own reasons (enroll.render_gres). Without this the same silence swallows Phase 4."""

    def test_setting_up_the_head_pushes_the_config_to_the_arms(self, monkeypatch):
        from cuttle import setup
        ran = []
        monkeypatch.setattr(setup, "run", lambda argv, **kw: ran.append(list(argv)))
        monkeypatch.setattr(setup, "ensure_munge_key", lambda: None)
        setup.start_daemons()
        assert ["scontrol", "reconfigure"] in ran

    def test_it_comes_after_the_restart_not_before(self, monkeypatch):
        """Order is the point: reconfigure tells the daemons to re-read, so a reconfigure issued
        before slurmctld has the new file on disk pushes the OLD one to every Arm."""
        from cuttle import setup
        ran = []
        monkeypatch.setattr(setup, "run", lambda argv, **kw: ran.append(list(argv)))
        monkeypatch.setattr(setup, "ensure_munge_key", lambda: None)
        setup.start_daemons()
        restart = next(i for i, c in enumerate(ran) if c[:2] == ["systemctl", "restart"])
        assert ran.index(["scontrol", "reconfigure"]) > restart


class TestSlurmSudoers:
    """The Head's ONE unattended privileged grant in Phase 3. What it is NOT is the whole point."""

    def test_the_runas_is_the_guise_group_never_root(self):
        rule = headapi._SUDOERS_CCC_SLURM
        assert f"({config.HEAD_API_USER} ALL=(%{config.GUISE_GROUP})" not in rule  # sanity on shape
        assert f"ALL=(%{config.GUISE_GROUP})" in rule
        assert "ALL=(root)" not in rule and "ALL=(ALL)" not in rule

    def test_it_grants_exactly_the_three_slurm_clients(self):
        rule = headapi._SUDOERS_CCC_SLURM
        cmds = rule.split("NOPASSWD:")[1]
        assert set(c.strip() for c in cmds.replace("\\\n", "").split(",")) == {
            "/usr/bin/sbatch", "/usr/bin/scancel", "/usr/bin/scontrol"}

    def test_the_wandb_environment_survives_sudos_scrub(self):
        """sbatch propagates its own env to the job — that is how a User's W&B key reaches the Pod.
        sudo scrubs it by default, so without this every Spawn silently logs nothing, which its
        submitter only discovers afterwards in an empty W&B project."""
        rule = headapi._SUDOERS_CCC_SLURM
        assert "env_keep" in rule
        for var in ("WANDB_API_KEY", "WANDB_MODE", "WANDB_PROJECT", "WANDB_ENTITY"):
            assert var in rule
        # NOT the blanket alternatives, both of which grant more than is needed.
        assert "SETENV" not in rule


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


def bench_body(job: str) -> str:
    """The Python the benchmark job heredocs into the Pod, lifted back out of the shell script."""
    return job.split("<<'CCC_BENCH_PY'\n")[1].split("\nCCC_BENCH_PY")[0]


class TestBenchmarkSbatch:
    """The Power benchmark: a real Spawn, submitted by the Head, pinned to the node being priced."""

    def test_pinned_to_the_node_under_test(self):
        job = templates.render_bench_sbatch("donor-box", "100.99.0.1", "b3nch")
        assert "#SBATCH --nodelist=donor-box" in job
        assert "#SBATCH --gres=gpu:1" in job

    def test_runs_inside_the_pod(self):
        """Measured in the Pod, not on the host and not from a spec sheet — so the number reflects the
        machine as Spawns really meet it (a WSL GPU behind /dev/dxg, a throttling laptop card, a stale
        driver). None of that is on a spec sheet, and all of it is what an hour on the card is worth."""
        job = templates.render_bench_sbatch("donor-box", "100.99.0.1", "b3nch")
        assert "apptainer exec --nv" in job
        assert str(config.CCC_SIF) in job

    def test_result_comes_home_by_rsync(self):
        """One code path for a donor's box and for the Head's own Arm (which rsyncs to itself over the
        mesh). rsync will not invent the destination directory — the Head pre-creates it."""
        job = templates.render_bench_sbatch("donor-box", "100.99.0.1", "b3nch")
        assert (f"rsync://{config.RSYNC_BENCH_USER}@100.99.0.1/{config.RSYNC_MODULE_BENCH}/"
                f"donor-box/") in job

    def test_it_authenticates_to_the_bench_module(self):
        """A benchmark cannot be handed a **Siphon** — it never goes through the Head API — so it is
        the one job carrying the cluster-lifetime credential instead. Without the password in its
        environment the module's `auth users` refuses it and the Power file never arrives, which
        surfaces as an Arm that enrolled and then priced at nothing."""
        job = templates.render_bench_sbatch("donor-box", "100.99.0.1", "b3nch")
        assert "export RSYNC_PASSWORD=b3nch" in job
        # The bench tree, not the Spawns tree: a benchmark must not be able to reach a Spawn's results.
        assert str(config.HEAD_SPAWNS_DIR) not in job

    def test_embedded_python_is_intact(self):
        """The benchmark body is str.format()-ed into a heredoc, and it contains a dict literal — so
        every brace of that literal has to be doubled. Get it wrong and the render either raises, or
        (worse) quietly emits Python that doesn't parse, on a node nobody is watching."""
        body = bench_body(templates.render_bench_sbatch("donor-box", "100.99.0.1", "b3nch"))
        compile(body, "bench.py", "exec")  # the real check: it is valid Python
        assert re.search(r"\{\w+\}", body) is None, "an unrendered {placeholder} survived"
        assert f"range({config.BENCH_ITERS})" in body  # ...and the substitutions really happened

    def test_warmup_is_not_timed(self):
        """The first matmul pays cuBLAS init. Timing it would under-report Power on every card, and by
        an amount that depends on how fast the card is — a systematic bias, not noise."""
        body = bench_body(templates.render_bench_sbatch("n", "ip", "b3nch"))
        warmup, timed = body.index(f"range({config.BENCH_WARMUP})"), body.index("perf_counter()")
        assert warmup < timed

    def test_one_job_holds_every_card(self):
        """Power is per **Device**, so a four-card box needs four numbers — but from ONE Spawn. Four
        separate jobs would queue behind each other and could each land while a sibling card was held
        by somebody else's work, measuring a box under contention rather than the card itself."""
        job = templates.render_bench_sbatch("big-box", "100.99.0.1", "b3nch", 4)
        assert "#SBATCH --gres=gpu:4" in job
        assert len([ln for ln in job.splitlines() if ln.startswith("#SBATCH --gres")]) == 1

    def test_it_measures_each_visible_device(self):
        """Identical models still differ in practice — an x4 slot, the card that throttles under the
        other — so every card is timed, not one and multiplied."""
        body = bench_body(templates.render_bench_sbatch("big-box", "100.99.0.1", "b3nch", 4))
        assert "torch.cuda.device_count()" in body
        assert "for i in range(ndev)" in body
        assert '"powers": powers' in body
