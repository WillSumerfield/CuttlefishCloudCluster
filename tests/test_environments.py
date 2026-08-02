"""Per-Spawn **Environment** detection and build rendering (docs/TODO.md "Python Environments").

Pure functions with remote consequences, in CONTRIBUTING.md's sense: nothing here needs apptainer, a
Guise, or a network, but every one of these decides what runs on a donor's box. The two things worth
pinning are that detection never guesses past the project root, and that the rendered build keeps the
one property the whole design rests on — the env is built at the SAME absolute path it is later run at.
"""

import pytest

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
    assert '--bind "$proj,$ccc_out$envbind"' in body


def test_tidepool_binds_the_environment_too():
    """A Tide Pool debugs against the env its Spawn would get, or it is not debugging what failed."""
    body = _script(tidepool_port=22001, tidepool_pubkey="ssh-ed25519 AAAA")
    assert "$envbind" in body
    assert f"{env.ENV_MOUNT}/bin" in body  # on PATH for the interactive shell
