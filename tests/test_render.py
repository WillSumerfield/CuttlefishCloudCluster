"""Phase 4 slice 0 — what setup and enrollment actually WRITE to disk.

Every failure guarded here is silent at write time and only surfaces on a donor's box, minutes later,
as a node that registered but can't be scheduled. That is exactly the class of bug worth a unit test:
the render is pure, and the consequence is remote.
"""

import re

import pytest

from cuttle import config, enroll, system, templates


@pytest.fixture
def fake_host(monkeypatch):
    """The Head box, without touching the Head box: no nvidia-smi, no lscpu, no /proc/meminfo."""
    monkeypatch.setattr(system, "hostname", lambda: "headbox")
    monkeypatch.setattr(system, "cpu_topology", lambda: (1, 8, 2))
    monkeypatch.setattr(system, "real_memory_mb", lambda: 63188)
    monkeypatch.setattr(system, "gpu_vram_mb", lambda: 24564)


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

    def test_no_weight_baked_in(self, fake_host):
        """Weight is NOT rendered here, and can't be: it is 1/Power, and Power isn't known until the
        node is live enough to run a benchmark. It arrives later via `scontrol update`."""
        assert "Weight=" not in directives(templates.render_slurm_conf("100.99.0.1"))["NodeName"]


class TestSlurmdDropin:
    """A donor Arm's slurmd flags. The node is *dynamic*, so this string is re-sent to slurmctld on
    every single registration — it is the only durable home for the node's Feature."""

    def test_conf_is_quoted_as_one_argument(self):
        """The landmine. Ubuntu's unit is `ExecStart=/usr/sbin/slurmd --systemd $SLURMD_OPTIONS`, and
        systemd splits a variable's value at whitespace WITHOUT honouring quotes inside it. Via that
        hook, `--conf "Gres=gpu:1 Feature=vram24"` reaches slurmd as three mangled arguments. Quotes
        written literally in a unit file ARE honoured — hence a drop-in that states the command."""
        unit = enroll.slurmd_dropin("donor-box", "100.99.0.1:6817", 24564)
        assert '--conf "Gres=gpu:1 Feature=vram24"' in unit
        assert "SLURMD_OPTIONS" not in unit  # never go back to the /etc/default hook

    def test_resets_the_distro_execstart(self):
        """A drop-in APPENDS to ExecStart unless you clear it first. Without the bare `ExecStart=`,
        systemd would refuse the unit (two ExecStart lines on a Type=notify service)."""
        unit = enroll.slurmd_dropin("donor-box", "100.99.0.1:6817", 24564)
        lines = [ln for ln in unit.splitlines() if ln.startswith("ExecStart")]
        assert lines[0] == "ExecStart="
        assert lines[1].startswith("ExecStart=/usr/sbin/slurmd")
        assert len(lines) == 2

    def test_dynamic_registration_flags_survive(self):
        unit = enroll.slurmd_dropin("donor-box", "100.99.0.1:6817", 24564)
        assert "-Z -N donor-box" in unit           # register as a dynamic node, under this name
        assert "--conf-server 100.99.0.1:6817" in unit  # configless: fetch slurm.conf from slurmctld

    def test_feature_tracks_the_card(self):
        assert "Feature=vram80" in enroll.slurmd_dropin("a", "h:1", 81920)
        assert "Feature=vram8" in enroll.slurmd_dropin("a", "h:1", 8192)


def bench_body(job: str) -> str:
    """The Python the benchmark job heredocs into the Pod, lifted back out of the shell script."""
    return job.split("<<'CCC_BENCH_PY'\n")[1].split("\nCCC_BENCH_PY")[0]


class TestBenchmarkSbatch:
    """The Power benchmark: a real Spawn, submitted by the Head, pinned to the node being priced."""

    def test_pinned_to_the_node_under_test(self):
        job = templates.render_bench_sbatch("donor-box", "100.99.0.1")
        assert "#SBATCH --nodelist=donor-box" in job
        assert "#SBATCH --gres=gpu:1" in job

    def test_runs_inside_the_pod(self):
        """Measured in the Pod, not on the host and not from a spec sheet — so the number reflects the
        machine as Spawns really meet it (a WSL GPU behind /dev/dxg, a throttling laptop card, a stale
        driver). None of that is on a spec sheet, and all of it is what an hour on the card is worth."""
        job = templates.render_bench_sbatch("donor-box", "100.99.0.1")
        assert "apptainer exec --nv" in job
        assert str(config.CCC_SIF) in job

    def test_result_comes_home_by_rsync(self):
        """One code path for a donor's box and for the Head's own Arm (which rsyncs to itself over the
        mesh). rsync will not invent the destination directory — the Head pre-creates it."""
        job = templates.render_bench_sbatch("donor-box", "100.99.0.1")
        assert (f"rsync://100.99.0.1/{config.RSYNC_MODULE_SPAWNS}/"
                f"{config.BENCH_DIR_NAME}/donor-box/") in job

    def test_embedded_python_is_intact(self):
        """The benchmark body is str.format()-ed into a heredoc, and it contains a dict literal — so
        every brace of that literal has to be doubled. Get it wrong and the render either raises, or
        (worse) quietly emits Python that doesn't parse, on a node nobody is watching."""
        body = bench_body(templates.render_bench_sbatch("donor-box", "100.99.0.1"))
        compile(body, "bench.py", "exec")  # the real check: it is valid Python
        assert re.search(r"\{\w+\}", body) is None, "an unrendered {placeholder} survived"
        assert f"range({config.BENCH_ITERS})" in body  # ...and the substitutions really happened

    def test_warmup_is_not_timed(self):
        """The first matmul pays cuBLAS init. Timing it would under-report Power on every card, and by
        an amount that depends on how fast the card is — a systematic bias, not noise."""
        body = bench_body(templates.render_bench_sbatch("n", "ip"))
        warmup, timed = body.index(f"range({config.BENCH_WARMUP})"), body.index("perf_counter()")
        assert warmup < timed
