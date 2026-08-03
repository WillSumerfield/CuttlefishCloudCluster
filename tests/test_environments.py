"""Per-Spawn **Environment** detection, build rendering, and the held-Spawn lifecycle around them
(docs/TODO.md "Python Environments").

Mostly pure functions with remote consequences, in CONTRIBUTING.md's sense: nothing in the first half
needs apptainer, a Guise, or a network, but every one of these decides what runs on a donor's box. The
two things worth pinning are that detection never guesses past the project root, and that the rendered
build keeps the one property the whole design rests on — the env is built at the SAME absolute path it
is later run at.

The second half is the part that is not pure: a Spawn is `sbatch --hold`ed, built behind the request,
then released or failed. What is worth pinning there is that every exit from a build reaches ONE of
those two ends — a Spawn left held is one holding its submitter's Escrow forever, with no thread left
to come back for it.
"""

import io
import tarfile
from pathlib import Path

import pytest

from ccc_head_api import db
from ccc_head_api import environments as env
from ccc_head_api import spawns


def _project(tmp_path, **files):
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return tmp_path


# --- detection -------------------------------------------------------------

def test_no_manifest_is_no_environment(tmp_path):
    """The common case, and today's behaviour: a project that declares nothing runs on the base image."""
    assert env.detect(_project(tmp_path, **{"train.py": "print(1)"})) is None


@pytest.mark.parametrize("name,kind", [
    ("uv.lock", "uv-project"),
    ("pyproject.toml", "uv-project"),
    ("requirements.txt", "uv-requirements"),
    ("environment.yml", "conda"),
    ("environment.yaml", "conda"),
])
def test_each_manifest_shape_is_recognised(tmp_path, name, kind):
    m = env.detect(_project(tmp_path, **{name: ""}))
    assert (m.kind, m.path) == (kind, name)


def test_lock_file_wins_over_bare_pyproject(tmp_path):
    """A lock is preferred because it is the only manifest that makes a Recipe's `frozen` promise true
    of its Environment too (CONTEXT.md, **Recipe**)."""
    m = env.detect(_project(tmp_path, **{"pyproject.toml": "", "uv.lock": "", "requirements.txt": ""}))
    assert m.path == "uv.lock"


def test_detection_never_walks_below_the_root(tmp_path):
    """A pyproject three directories down belongs to something the User is vendoring, not to this Spawn.
    Finding it would build a stranger's dependencies and bill this User for the privilege."""
    assert env.detect(_project(tmp_path, **{"vendor/thirdparty/pyproject.toml": ""})) is None


def test_none_declines_a_build_outright(tmp_path):
    """A file in a repo is not an instruction to the cluster — CONTEXT.md, **Environment**."""
    p = _project(tmp_path, **{"pyproject.toml": ""})
    assert env.detect(p, manifest="none") is None
    assert env.detect(p, manifest="NONE") is None


def test_named_manifest_overrides_the_guess(tmp_path):
    p = _project(tmp_path, **{"pyproject.toml": "", "requirements-gpu.txt": ""})
    m = env.detect(p, manifest="requirements-gpu.txt")
    assert (m.kind, m.path) == ("uv-requirements", "requirements-gpu.txt")


def test_named_manifest_in_a_subdirectory_is_allowed(tmp_path):
    """Explicit is different from auto-detected: the User pointed at it, so depth is their business."""
    m = env.detect(_project(tmp_path, **{"envs/cuda12.yml": ""}), manifest="envs/cuda12.yml")
    assert m.kind == "conda"


def test_named_manifest_must_exist(tmp_path):
    with pytest.raises(env.BuildError, match="no such manifest"):
        env.detect(_project(tmp_path), manifest="requirements.txt")


def test_named_manifest_cannot_escape_the_project(tmp_path):
    """The containment rule declared output names get (spawns.OUTPUT_NAME_RE), for the same reason: this
    path is joined onto a directory the Head owns, and `..` must not be a way out of it."""
    (tmp_path.parent / "outside.txt").write_text("")
    with pytest.raises(env.BuildError, match="no such manifest"):
        env.detect(_project(tmp_path), manifest="../outside.txt")


def test_unclassifiable_named_manifest_says_what_is_accepted(tmp_path):
    with pytest.raises(env.BuildError, match="cannot tell what kind"):
        env.detect(_project(tmp_path, **{"deps.cfg": ""}), manifest="deps.cfg")


# --- the submit-time echo --------------------------------------------------

def test_echo_names_the_no_environment_case(tmp_path):
    """The worst failure is a manifest one directory too deep: not a build failure at all, so this line
    is the only thing that would ever mention it — before a GPU is held, not after."""
    assert "no dependency file found" in env.describe(None)


def test_echo_distinguishes_conda_as_a_replacement(tmp_path):
    """uv layers on the base image; conda replaces it. A User should not have to know that from docs."""
    assert "replaces" in env.describe(env.detect(_project(tmp_path, **{"environment.yml": ""})))
    assert "on top of" in env.describe(env.detect(_project(tmp_path, **{"requirements.txt": ""})))


# --- build rendering -------------------------------------------------------

def test_every_build_targets_the_one_fixed_mount(tmp_path):
    """The property the whole design rests on. A venv bakes its prefix into pyvenv.cfg and its shebangs;
    a conda prefix bakes it into hundreds of files. Built anywhere other than where it is later run,
    an env arrives on the Arm broken."""
    for name in ("uv.lock", "requirements.txt", "environment.yml"):
        m = env.detect(_project(tmp_path / name, **{name: ""}))
        cmds = env.render_build(m)
        assert any(env.ENV_MOUNT in part for cmd in cmds for part in cmd), name


def test_uv_paths_inherit_the_image_rather_than_rebuild_it(tmp_path):
    """--system-site-packages is what makes this the DELTA. Without it a bare `torch` in a manifest
    redownloads the 2.5GB the base image already ships, on every Spawn, with nothing cached."""
    for name in ("uv.lock", "requirements.txt"):
        m = env.detect(_project(tmp_path / name, **{name: ""}))
        assert "--system-site-packages" in env.render_build(m)[0]


def test_uv_project_build_installs_the_project_itself(tmp_path):
    """Plain `uv sync`, NOT --no-install-project: uv already treats a project with no [build-system] as
    virtual (this repo's own root pyproject does exactly that), while a real src-layout package gets
    installed — which is the only way its imports resolve."""
    cmds = env.render_build(env.detect(_project(tmp_path, **{"uv.lock": ""})))
    sync = cmds[-1]
    assert "sync" in sync and "--no-install-project" not in sync


def test_conda_build_injects_wandb(tmp_path):
    """A conda env replaces the image's Python and inherits nothing — including wandb. Without this the
    Head's promise that a Spawn logs to its submitter's own account (CONTEXT.md) silently stops holding
    for exactly the Users who declared the most."""
    cmds = env.render_build(env.detect(_project(tmp_path, **{"environment.yml": ""})))
    assert any("wandb" in part for cmd in cmds for part in cmd)


def test_conda_build_does_not_inherit_system_site_packages(tmp_path):
    """The asymmetry is deliberate: uv layers, conda replaces."""
    cmds = env.render_build(env.detect(_project(tmp_path, **{"environment.yml": ""})))
    assert not any("--system-site-packages" in part for cmd in cmds for part in cmd)


def test_interpreter_is_under_the_mount(tmp_path):
    """What the rendered JOB_SCRIPT names as `$py`. It has to resolve inside the Pod, against the bind —
    not against whatever path the env happened to be built in on the Head."""
    assert env.ENV_INTERPRETER.startswith(env.ENV_MOUNT + "/")


# --- what the rendered job script does with it -----------------------------
# The remote consequence: this text runs on a donor's box, and a wrong bind is silent here and fatal
# there. See CONTRIBUTING.md on why these assert on structure rather than substrings where it matters.

def _script(**kw):
    kw.setdefault("minutes", 60)
    kw.setdefault("constraint", "")
    kw.setdefault("node", None)
    kw.setdefault("outputs", ["outputs"])
    return spawns._render(staging="/tmp/u", gpu=1, script="train.py", args=[], files=[],
                          token="a3f9", secret="s3cr3t", **kw)


def test_script_decides_at_run_time_whether_there_is_an_environment():
    """One rendering serves both cases: the script tests for the env dir itself rather than the Head
    baking in an answer. A build that produced nothing degrades to the base interpreter on its own."""
    body = _script()
    assert f'[ -d "$proj/{env.ENV_DIRNAME}" ]' in body
    assert 'py="python"' in body


def test_environment_is_moved_out_of_the_project_before_running():
    """$proj is what recover_scratch() copies for the Recovery Window and measures against
    RECOVERY_MAX_BYTES. An 8GB conda env left inside it would silently disable recovery for exactly the
    Spawns most likely to need it."""
    body = _script()
    assert f'mv "$proj/{env.ENV_DIRNAME}" "$scratch/env"' in body


def test_environment_is_bound_at_the_path_it_was_built_at():
    """The one property the design rests on — see test_every_build_targets_the_one_fixed_mount."""
    assert f'envbind=",$scratch/env:{env.ENV_MOUNT}"' in _script()


def test_payload_runs_the_environments_interpreter_and_binds_it_together():
    """`$py` and `$envbind` are set in the same branch, so the interpreter named can never be one the
    bind did not provide — a Pod that mounted no env but ran /opt/ccc/env/bin/python fails with a
    file-not-found the User cannot act on."""
    body = _script()
    assert '"$sif" $py train.py' in body
    assert '--bind "$proj,$ccc_out$envbind$licbind"' in body


def test_the_remote_stage_in_recurses():
    """The other half of the `.ccc-env` filelist entry, and a bug that shipped: `--files-from` implies
    --dirs but NOT --recursive — rsync(1) says `-a` does not imply `-r` here — so a bare directory name
    arrives as an EMPTY directory. The env was built, staged as nothing, bound over ENV_MOUNT, and the
    Spawn died on `stat /opt/ccc/env/bin/python` (hardware, 2026-08-02, Spawn 16). Remote Arms only;
    the Head-local branch is a `cp -a` and was never affected."""
    stage_in = next(ln for ln in _script().splitlines()
                    if '--files-from="$scratch/filelist"' in ln)
    assert " -r " in stage_in


def test_tidepool_binds_the_environment_too():
    """A Tide Pool debugs against the env its Spawn would get, or it is not debugging what failed."""
    body = _script(tidepool_port=22001, tidepool_pubkey="ssh-ed25519 AAAA")
    assert "$envbind" in body
    assert f"{env.ENV_MOUNT}/bin" in body  # on PATH for the interactive shell


# --- a Head that cannot build ----------------------------------------------
# The regression this guards against is not hypothetical: TOOLS_DIR is populated by
# `cuttle setup --head`, so every Head provisioned before this feature existed has an empty one. If a
# missing binary failed the build, every project carrying a `pyproject.toml` — which is most of them —
# would stop submitting on those Heads, having submitted fine the day before.

@pytest.fixture
def no_tools(monkeypatch, tmp_path):
    """A Head with an empty TOOLS_DIR."""
    monkeypatch.setattr(env, "UV_BIN", tmp_path / "tools" / "uv")
    monkeypatch.setattr(env, "MICROMAMBA_BIN", tmp_path / "tools" / "micromamba")


@pytest.fixture
def with_tools(monkeypatch, tmp_path):
    """A Head that has both build tools installed, and a cache it may write to."""
    d = tmp_path / "tools"
    d.mkdir(parents=True, exist_ok=True)
    for name in ("uv", "micromamba"):
        (d / name).write_text("#!/bin/sh\n")
    monkeypatch.setattr(env, "UV_BIN", d / "uv")
    monkeypatch.setattr(env, "MICROMAMBA_BIN", d / "micromamba")
    monkeypatch.setattr(env, "TOOLS_DIR", d)
    monkeypatch.setattr(env, "CACHE_DIR", tmp_path / "cache")


def test_a_missing_tool_is_named_not_hidden(no_tools, tmp_path):
    m = env.detect(_project(tmp_path, **{"requirements.txt": ""}))
    assert env.missing_tool(m).endswith("/uv")


def test_conda_asks_for_micromamba_not_uv(no_tools, tmp_path):
    m = env.detect(_project(tmp_path, **{"environment.yml": ""}))
    assert env.missing_tool(m).endswith("/micromamba")


def test_an_equipped_head_is_missing_nothing(with_tools, tmp_path):
    m = env.detect(_project(tmp_path, **{"requirements.txt": ""}))
    assert env.missing_tool(m) is None


def test_the_echo_says_which_manifest_went_unbuilt(no_tools, tmp_path):
    """Not merely that something did. A User whose Spawn ran on the base image needs to know the Head
    is unequipped, not conclude their manifest was wrong and go on editing it."""
    m = env.detect(_project(tmp_path, **{"requirements.txt": ""}))
    note = env.describe(m, env.missing_tool(m))
    assert "requirements.txt" in note and "uv" in note and "cluster base" in note


def test_the_build_can_reach_its_tools_inside_the_pod(with_tools, tmp_path, monkeypatch):
    """--contain gives the build nothing of the Head's filesystem but what is bound. uv and micromamba
    live on the HEAD, not in base.sif, so without this bind the argv names a binary that does not exist
    inside the Pod — and every declared Environment fails on a Head that installed them correctly."""
    seen = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(env.subprocess, "run", lambda cmd, **kw: (seen.append(cmd), R())[1])
    project = _project(tmp_path / "p", **{"requirements.txt": ""})
    env.build(project, env.detect(project), "ccc-u60002")
    binds = [c[c.index("--bind") + 1] for c in seen if "--bind" in c]
    assert binds and all(f"{env.TOOLS_DIR}:{env.TOOLS_DIR}:ro" in b for b in binds)


def test_uv_sync_is_pointed_at_the_mount_by_variable_not_by_active(with_tools, tmp_path, monkeypatch):
    """`--active` only means "use $VIRTUAL_ENV", and nothing in a build Pod sets it — so uv fell back to
    the `.venv` it makes beside the manifest, inside the Mantle-owned project a Guise may only read, and
    every pyproject.toml/uv.lock Spawn died on "Permission denied" (hardware, 2026-08-02, Spawns 14/15).
    UV_PROJECT_ENVIRONMENT names the path outright, as --python already did on the uv-pip branch."""
    assert "--active" not in env.render_build(env.Manifest("uv-project", "pyproject.toml"))[-1]
    seen = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(env.subprocess, "run", lambda cmd, **kw: (seen.append(cmd), R())[1])
    project = _project(tmp_path / "p", **{"pyproject.toml": ""})
    env.build(project, env.detect(project), "ccc-u60002")
    envs = [c[c.index("--env") + 1] for c in seen if "--env" in c]
    assert envs and all(f"UV_PROJECT_ENVIRONMENT={env.ENV_MOUNT}" in e for e in envs)


# --- the held Spawn: submit, build, release or fail -------------------------
# A Spawn is `sbatch --hold`ed so Slurm cannot place it before its env exists, then built behind the
# request. Every path out of that build must reach exactly one of two ends — released, or failed — or
# the Spawn sits held forever with its submitter's Escrow suppressing their Share.

@pytest.fixture
def head(monkeypatch, tmp_path, with_tools):
    """A Head with a real DB and a captured shell. Returns the list of argv it ran."""
    state = tmp_path / "state"
    for name in ("projects", "spawns", "scratch"):
        (state / name).mkdir(parents=True)
    monkeypatch.setattr(spawns, "PROJECTS_DIR", state / "projects")
    monkeypatch.setattr(spawns, "SPAWNS_DIR", state / "spawns")
    monkeypatch.setattr(spawns, "SCRATCH_DIR", state / "scratch")
    monkeypatch.setattr(spawns, "MESH_IP", "100.99.0.1")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    db.ensure_schema()
    with db.connect() as conn:
        db.add_user(conn, "alice")
    calls = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return type("R", (), {"stdout": "4242\n", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(spawns.subprocess, "run", fake_run)
    # The build is what these tests drive by hand — starting a thread would race every assertion.
    monkeypatch.setattr(spawns, "start_build", lambda *a, **kw: None)
    return calls


def _tar(tmp_path, *names):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for n in names:
            info = tarfile.TarInfo(n)
            info.size = 0
            tf.addfile(info, io.BytesIO(b""))
    p = tmp_path / "up.tar.gz"
    p.write_bytes(buf.getvalue())
    return p


def _submit(tmp_path, *names, **kw):
    return spawns.submit(user_id=1, script="train.py", gpu=1, args=[], wandb_env={},
                         tar_path=_tar(tmp_path, "train.py", *names), minutes=60,
                         constraint="", outputs=["out"], **kw)


def _sbatch(calls):
    return next(c for c in calls if "sbatch" in c)


def test_a_spawn_with_no_manifest_is_never_held(head, tmp_path):
    """The overwhelmingly common case, and the one that must not regress: nothing to build, so nothing
    to wait for, and the Spawn goes straight into the queue exactly as it always did."""
    jid = _submit(tmp_path)
    assert "--hold" not in _sbatch(head)
    with db.connect() as conn:
        assert db.get_spawn(conn, jid)["build_state"] is None


def test_a_declared_environment_holds_the_spawn(head, tmp_path):
    """The dispatch interlock. Without --hold, Slurm can place the Spawn on an Arm while the env it is
    supposed to run against is still being built on the Head — and the Arm stages in a half-written
    directory, or none at all."""
    jid = _submit(tmp_path, "requirements.txt")
    assert "--hold" in _sbatch(head)
    with db.connect() as conn:
        assert db.get_spawn(conn, jid)["build_state"] == db.BUILDING


def test_the_environment_is_in_what_a_remote_arm_stages_in(head, tmp_path, monkeypatch):
    """It cannot be enumerated like the other files — it does not exist yet when the script is rendered
    — so one directory entry stands in for the tree, and the stage-in's explicit -r is what makes that
    one entry pull the whole env (see the next test). Without the entry a remote Arm stages in a project
    with no env in it and runs against an interpreter that was never copied."""
    seen = {}
    real = spawns._render
    # `files` is the 5th positional arg of _render, not a keyword — see spawns.submit's call.
    monkeypatch.setattr(spawns, "_render",
                        lambda *a, **kw: (seen.update(files=a[4]), real(*a, **kw))[1])
    _submit(tmp_path, "requirements.txt")
    assert env.ENV_DIRNAME in seen["files"]


def test_a_spawn_with_no_environment_stages_no_env_dir(head, tmp_path, monkeypatch):
    """The other half: naming a directory that will never exist would make every ordinary Spawn's
    stage-in report a missing file."""
    seen = {}
    real = spawns._render
    # `files` is the 5th positional arg of _render, not a keyword — see spawns.submit's call.
    monkeypatch.setattr(spawns, "_render",
                        lambda *a, **kw: (seen.update(files=a[4]), real(*a, **kw))[1])
    _submit(tmp_path)
    assert env.ENV_DIRNAME not in seen["files"]


def test_a_head_without_the_tools_submits_the_spawn_anyway(head, tmp_path, monkeypatch):
    """The regression. Before Environments existed, a project carrying a pyproject.toml submitted fine
    and ran on the base image. On a Head that never installed uv, it must still do exactly that —
    refusing it would break submissions that worked the day before, for a feature nobody asked for.

    Un-installs uv after `head` (which provides one) rather than using the `no_tools` fixture: fixture
    order would otherwise put the tools back."""
    monkeypatch.setattr(env, "UV_BIN", tmp_path / "nowhere" / "uv")
    jid = _submit(tmp_path, "pyproject.toml")
    assert "--hold" not in _sbatch(head)
    with db.connect() as conn:
        assert db.get_spawn(conn, jid)["build_state"] is None
    assert "cluster base" in spawns.environment_note(jid)


def test_the_echo_is_written_for_every_spawn(head, tmp_path):
    jid = _submit(tmp_path, "requirements.txt")
    assert "requirements.txt" in spawns.environment_note(jid)


def test_none_declines_the_build_end_to_end(head, tmp_path):
    """`--manifest none`, all the way from the wire to sbatch — a file in a repo is not an instruction
    to the cluster (CONTEXT.md, **Environment**)."""
    jid = _submit(tmp_path, "requirements.txt", manifest="none")
    assert "--hold" not in _sbatch(head)
    assert "no dependency file found" in spawns.environment_note(jid)


def test_a_built_spawn_is_released_as_its_guise(head, tmp_path, monkeypatch):
    """Release is the ONLY thing standing between a successful build and a Spawn that never runs. As
    the Guise, not root: Slurm lets a job's owner release their own hold, and CCC holds no root."""
    jid = _submit(tmp_path, "requirements.txt")
    monkeypatch.setattr(env, "build", lambda *a, **kw: "solved 3 packages")
    monkeypatch.setattr(env, "prune_cache", lambda: None)
    spawns._build_then_release(jid, spawns.PROJECTS_DIR, None, "ccc-u60002")
    release = next(c for c in head if "release" in c)
    assert release == ["sudo", "-n", "-u", "ccc-u60002", "scontrol", "release", str(jid)]
    with db.connect() as conn:
        assert db.get_spawn(conn, jid)["build_state"] is None


def test_a_successful_build_keeps_its_log(head, tmp_path, monkeypatch):
    """`uv sync` reports what it resolved, and that is the only answer to "why did my Spawn get that
    version" once the env itself has been swept with the staging dir."""
    jid = _submit(tmp_path, "requirements.txt")
    monkeypatch.setattr(env, "build", lambda *a, **kw: "resolved torch==2.13.0")
    monkeypatch.setattr(env, "prune_cache", lambda: None)
    spawns._build_then_release(jid, spawns.PROJECTS_DIR, None, "ccc-u60002")
    assert "torch==2.13.0" in spawns.build_log(jid)


def test_a_failed_build_is_a_failed_spawn_not_a_lost_one(head, tmp_path, monkeypatch):
    """It was accepted, Escrowed and queued minutes ago and its submitter has walked away — so it ends
    the way every other failed Spawn ends, with an `err` they can read and a non-zero `exit`. scancel
    takes it out of the queue, and its absence from the next tick's poll settles the Escrow at zero (it
    held no Device for a second of this)."""
    jid = _submit(tmp_path, "requirements.txt")

    def boom(*a, **kw):
        raise env.BuildError("environment build failed (requirements.txt):\nno such package", log="$ uv")

    monkeypatch.setattr(env, "build", boom)
    spawns._build_then_release(jid, spawns.PROJECTS_DIR, None, "ccc-u60002")
    assert spawns.exit_code(jid) == 1
    assert "no such package" in spawns.err_text(jid)
    assert any("scancel" in c for c in head)
    with db.connect() as conn:
        assert db.get_spawn(conn, jid)["build_state"] is None


def test_a_crashing_build_still_ends_the_spawn(head, tmp_path, monkeypatch):
    """Any exception, not just BuildError. This runs in a thread: an escaping exception vanishes into
    the void and leaves a Spawn held forever, which is the one outcome this design cannot have."""
    jid = _submit(tmp_path, "requirements.txt")

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(env, "build", boom)
    spawns._build_then_release(jid, spawns.PROJECTS_DIR, None, "ccc-u60002")
    assert spawns.exit_code(jid) == 1
    with db.connect() as conn:
        assert db.get_spawn(conn, jid)["build_state"] is None


def test_a_restart_fails_the_builds_it_interrupted(head, tmp_path, monkeypatch):
    """Builds run in threads of the API process, so a build_state surviving into a fresh one is
    necessarily orphaned — nothing is coming back for it. Failed rather than rebuilt: its Escrow has
    been suppressing its submitter's Share the whole time the Head was down."""
    jid = _submit(tmp_path, "requirements.txt")
    monkeypatch.setattr(spawns, "_owner_of", lambda _id: "ccc-u60002")
    spawns.sweep_orphaned_builds()
    assert spawns.exit_code(jid) == 1
    assert "restarted" in spawns.err_text(jid)
    with db.connect() as conn:
        assert db.building_spawns(conn) == set()


def test_the_queue_tells_building_apart_from_waiting_for_a_card(head, tmp_path, monkeypatch):
    """squeue reports a held job as plain PENDING — indistinguishable from one waiting for a machine.
    The difference decides what the queue says, whether the Spawn generates Demand, and whether the
    tick re-nices it, so it comes from the Head's own record rather than an inference."""
    from ccc_head_api import slurm
    jid = _submit(tmp_path, "requirements.txt")
    monkeypatch.setattr(slurm, "_slurm_json", lambda *a, **kw: {"jobs": [
        {"job_id": jid, "partition": slurm.PARTITION, "job_state": ["PENDING"], "name": "t",
         "nodes": "", "user_name": "ccc-u60002", "time_limit": 60},
    ]})
    assert slurm.spawns()[0]["state"] == "building"
    assert slurm.queue()[0]["state"] == "BUILDING"


def test_a_building_spawn_generates_no_demand(head, tmp_path, monkeypatch):
    """Nobody is waiting on a Device yet. Counting it would price every Arm it is eligible for as
    wanted, on the strength of a Spawn that cannot be placed at all until the Head finishes."""
    from ccc_head_api import karma, slurm
    jid = _submit(tmp_path, "requirements.txt")
    monkeypatch.setattr(slurm, "_slurm_json", lambda *a, **kw: {"jobs": [
        {"job_id": jid, "partition": slurm.PARTITION, "job_state": ["PENDING"], "name": "t",
         "nodes": "", "user_name": "ccc-u60002", "time_limit": 60},
    ]})
    arms = [{"name": "arm1", "powers": [10.0], "state": "idle", "vram_gb": 24, "features": []}]
    needs = {jid: {"gpus": 1, "vram_gb": 24, "node": None}}
    assert karma.demand_by_arm(arms, slurm.queue(), needs)["arm1"] == 0.0
