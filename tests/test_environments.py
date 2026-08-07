"""Per-Spawn **Environment** declaration, build rendering, and the held-Spawn lifecycle around
them (docs/elements/environments.md).

Mostly pure functions with remote consequences (CONTRIBUTING.md): nothing in the first half
needs apptainer, a Guise, or a network, but each decides what runs on a donor's box. Three things
worth pinning: naming a manifest is mandatory and nothing is ever detected, an env inherits
nothing and carries its own interpreter, and the rendered build keeps the property the whole
design rests on — built at the SAME absolute path it's later run at.

The second half isn't pure: a Spawn is `sbatch --hold`ed, built behind the request, then
released or failed. Worth pinning: every exit from a build reaches ONE of those two ends — a
Spawn left held keeps its submitter's Escrow forever, with no thread left to come back for it.
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


# --- declaration -----------------------------------------------------------
# Naming the manifest is mandatory and there is no auto-detection: an **Environment** carries its own
# interpreter and inherits nothing, so which file it is built from decides what every line of the
# User's work runs against. Too much to infer from a file someone happened to commit.


def test_declaring_nothing_is_refused(tmp_path):
    """And refused even when there is an obvious candidate sitting right there. CCC picking it would
    be CCC deciding that a `pyproject.toml` in the repo is an instruction to the cluster — which it is
    not, if the User is vendoring something."""
    p = _project(tmp_path, **{"pyproject.toml": "", "requirements.txt": ""})
    for nothing in (None, "", "   "):
        with pytest.raises(env.BuildError, match="declares no environment"):
            env.detect(p, nothing)


def test_the_refusal_names_what_to_type(tmp_path):
    """A User who submitted the way they always did meets this message. "Required" alone would leave
    them nowhere to go, so it names the flag and the files it accepts."""
    with pytest.raises(env.BuildError) as e:
        env.detect(_project(tmp_path), None)
    assert "--manifest" in str(e.value) and "pyproject.toml" in str(e.value)


@pytest.mark.parametrize("name,kind", [
    ("uv.lock", "uv-project"),
    ("pyproject.toml", "uv-project"),
    ("requirements.txt", "uv-requirements"),
    ("environment.yml", "conda"),
    ("environment.yaml", "conda"),
])
def test_each_manifest_shape_is_recognised(tmp_path, name, kind):
    m = env.detect(_project(tmp_path, **{name: ""}), name)
    assert (m.kind, m.path) == (kind, name)


def test_a_manifest_in_a_subdirectory_is_allowed(tmp_path):
    """The User pointed at it, so depth is their business — there is no root to be "below" any more."""
    m = env.detect(_project(tmp_path, **{"envs/cuda12.yml": ""}), "envs/cuda12.yml")
    assert m.kind == "conda"


def test_a_manifest_under_any_name_is_allowed(tmp_path):
    """Classified by extension, never by parsing: a User may keep several and point at one without CCC
    opening a file to work out what it is."""
    m = env.detect(_project(tmp_path, **{"requirements-gpu.txt": ""}), "requirements-gpu.txt")
    assert (m.kind, m.path) == ("uv-requirements", "requirements-gpu.txt")


def test_the_named_manifest_must_exist(tmp_path):
    with pytest.raises(env.BuildError, match="no such manifest"):
        env.detect(_project(tmp_path), "requirements.txt")


def test_the_named_manifest_cannot_escape_the_project(tmp_path):
    """The containment rule declared output names get (spawns.OUTPUT_NAME_RE), for the same reason: this
    path is joined onto a directory the Head owns, and `..` must not be a way out of it."""
    (tmp_path.parent / "outside.txt").write_text("")
    with pytest.raises(env.BuildError, match="no such manifest"):
        env.detect(_project(tmp_path), "../outside.txt")


def test_an_unclassifiable_manifest_says_what_is_accepted(tmp_path):
    with pytest.raises(env.BuildError, match="cannot tell what kind"):
        env.detect(_project(tmp_path, **{"deps.cfg": ""}), "deps.cfg")


# --- the submit-time echo --------------------------------------------------

def test_echo_names_the_no_environment_case(tmp_path):
    """Reachable only by a **Tide Pool** now — the one Spawn that may decline to declare. The echo
    still says so out loud rather than staying blank, because after the image slims there is no Python
    at all in that shell and a User meeting it should know why."""
    assert "Tide Pool" in env.describe(None)


def test_echo_names_the_manifest_and_the_tool(tmp_path):
    """Both shapes are self-contained now, so the echo no longer draws a distinction between them — it
    says which file was read and which tool will read it, and nothing about what is inherited, because
    nothing is."""
    assert "conda" in env.describe(env.detect(_project(tmp_path, **{"environment.yml": ""}), "environment.yml"))
    note = env.describe(env.detect(_project(tmp_path, **{"requirements.txt": ""}), "requirements.txt"))
    assert "requirements.txt" in note and "uv" in note


# --- build rendering -------------------------------------------------------

def test_every_build_targets_the_one_fixed_mount(tmp_path):
    """The property the whole design rests on. A venv bakes its prefix into pyvenv.cfg and its shebangs;
    a conda prefix bakes it into hundreds of files. Built anywhere other than where it is later run,
    an env arrives on the Arm broken."""
    for name in ("uv.lock", "requirements.txt", "environment.yml"):
        m = env.detect(_project(tmp_path / name, **{name: ""}), name)
        cmds = env.render_build(m)
        assert any(env.ENV_MOUNT in part for cmd in cmds for part in cmd), name


def test_no_env_inherits_anything_from_the_image(tmp_path):
    """The property the restructure exists for. `--system-site-packages` is a RUNTIME flag, not a set of
    per-package links, so an env that used it held whatever the image happened to hold on the day the
    Spawn ran — which is not a freeze, and is not what a **Recipe** promises."""
    for name in ("uv.lock", "requirements.txt", "environment.yml"):
        m = env.detect(_project(tmp_path / name, **{name: ""}), name)
        cmds = env.render_build(m)
        assert not any("--system-site-packages" in part for cmd in cmds for part in cmd), name


def test_uv_project_build_installs_the_project_itself(tmp_path):
    """Plain `uv sync`, NOT --no-install-project: uv already treats a project with no [build-system] as
    virtual (this repo's own root pyproject does exactly that), while a real src-layout package gets
    installed — which is the only way its imports resolve."""
    cmds = env.render_build(env.detect(_project(tmp_path, **{"uv.lock": ""}), "uv.lock"))
    sync = next(c for c in cmds if "sync" in c)
    assert "--no-install-project" not in sync


def test_a_uv_project_needs_no_venv_prestep(tmp_path):
    """`uv sync` reads `requires-python` and fetches that exact interpreter itself. A `uv venv` first
    could only GUESS the version uv is about to resolve, and a wrong guess is what a project pinning
    `==3.11.14` would meet as a resolver error about an interpreter it never asked for."""
    cmds = env.render_build(env.detect(_project(tmp_path, **{"pyproject.toml": ""}), "pyproject.toml"))
    assert not any(cmd[1:2] == ["venv"] for cmd in cmds)


class TestTheInterpreterIsChosenNotInherited:
    """A `requirements.txt` names no Python version, so CCC names one rather than falling back to
    whatever the image ships — after this restructure the image ships none at all."""

    def _venv(self, project):
        cmds = env.render_build(env.detect(project, "requirements.txt"), project)
        return cmds[0]

    def test_a_pinned_default_when_the_project_says_nothing(self, tmp_path):
        assert ["--python", env.DEFAULT_PYTHON] == self._venv(
            _project(tmp_path, **{"requirements.txt": ""}))[2:4]

    def test_a_dot_python_version_is_the_users_answer_and_wins(self, tmp_path):
        """uv reads `.python-version` itself, so passing --python as well would override the User's own
        answer with the default — silently, and only for projects that bothered to state one."""
        p = _project(tmp_path, **{"requirements.txt": "", ".python-version": "3.11\n"})
        assert "--python" not in self._venv(p)


@pytest.mark.parametrize("pkg", ["wandb", "pip"])
def test_every_uv_env_is_seeded_without_overriding_a_declared_version(tmp_path, pkg):
    """Two packages no manifest asked for. `wandb`, because nothing is inherited any more and without
    it "a Spawn logs to its submitter's own account" (CONTEXT.md) stops being true. `pip`, because uv
    does not seed one and a **Tide Pool** promises `pip install` at the shell works.

    UNPINNED: an already-satisfied requirement is a no-op, so a User who declared their own version
    keeps it — as they did when the image supplied one."""
    for name in ("uv.lock", "requirements.txt"):
        cmds = env.render_build(env.detect(_project(tmp_path / name, **{name: ""}), name))
        seed = next(c for c in cmds if pkg in c and "install" in c)
        assert not any("==" in part for part in seed), name


def test_pip_is_seeded_with_uv_not_with_pip(tmp_path):
    """The bootstrap order that makes it possible at all: `python -m pip install pip` cannot run in an
    env that has no pip yet. `uv pip` needs nothing in the target env."""
    cmds = env.render_build(env.detect(_project(tmp_path, **{"uv.lock": ""}), "uv.lock"))
    seed = next(c for c in cmds if "pip" in c and "install" in c)
    assert seed[0] == str(env.UV_BIN) and "-m" not in seed


def test_conda_build_injects_wandb(tmp_path):
    """A conda env replaces the image's Python and inherits nothing — including wandb. Without this the
    Head's promise that a Spawn logs to its submitter's own account (CONTEXT.md) silently stops holding
    for exactly the Users who declared the most."""
    cmds = env.render_build(env.detect(_project(tmp_path, **{"environment.yml": ""}), "environment.yml"))
    assert any("wandb" in part for cmd in cmds for part in cmd)


@pytest.mark.parametrize("kind", ["uv-project", "uv-requirements", "conda"])
def test_the_interpreter_is_under_the_mount(kind):
    """What the rendered JOB_SCRIPT names as `$py`. It has to resolve inside the Pod, against the bind —
    not against whatever path the env happened to be built in on the Head. Per-SHAPE now rather than one
    constant: micromamba owns ENV_MOUNT outright, while a uv env keeps its venv one level down."""
    m = env.Manifest(kind, "m")
    assert m.interpreter.startswith(env.ENV_MOUNT + "/")
    assert m.interpreter == m.bin_dir + "/python"


def test_the_two_shapes_do_not_share_a_path():
    """Which is the whole reason the interpreter stopped being a module constant. A conda prefix at
    ENV_MOUNT/venv would be a lie, and a venv at ENV_MOUNT would have the interpreter uv downloads
    sitting inside the directory uv itself prunes."""
    assert env.Manifest("conda", "m").interpreter != env.Manifest("uv-project", "m").interpreter


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
    assert '--bind "$proj,$ccc_out,$scratch/tmp:/ccc-tmp$envbind$licbind$featbind"' in body


@pytest.mark.parametrize("kind,expect", [
    ("uv-project", f"{env.VENV_DIR}/bin/python"),
    ("conda", f"{env.ENV_MOUNT}/bin/python"),
])
def test_the_script_names_the_interpreter_the_build_actually_produced(kind, expect):
    """`$py` is per-Spawn substitution data, not a constant: only the manifest says which of the two
    shapes was built, and naming the wrong one is a `stat` failure on a donor's box after the Spawn has
    already staged in and taken a card."""
    assert f'py="{expect}"' in _script(declared=env.Manifest(kind, "m"))


def test_every_spawn_writes_temp_files_to_disk_not_to_the_pods_ram_tmp():
    """Not only a Tide Pool. `--contain` gives EVERY Pod a /tmp carved from apptainer's session
    directory — 64MB of RAM by default, shared with the --writable-tmpfs overlay — and a training
    script extracting a dataset or building a wheel hits it exactly as pip does. `environments.py`
    already makes this fix for the BUILD Pod; this is the same fix for the Pod that runs the work."""
    body = _script()
    assert 'mkdir -p "$scratch/tmp/pip"' in body
    assert "$scratch/tmp:/ccc-tmp" in body
    assert "TMPDIR=/ccc-tmp" in body


def test_the_temp_scratch_is_outside_the_recovery_window():
    """recover_scratch() copies $proj and measures it against RECOVERY_MAX_BYTES. A pip cache inside
    it would spend a User's recovery allowance on temp files nobody wants back."""
    body = _script()
    assert '"$scratch/tmp' in body and '"$proj/tmp' not in body


def test_the_remote_stage_in_recurses():
    """The other half of the `.ccc-env` filelist entry: `--files-from` implies `--dirs` but NOT
    `--recursive` (rsync(1): `-a` doesn't imply `-r` here), so a bare directory name arrives as
    an EMPTY directory — the env staged as nothing, bound over ENV_MOUNT, and the Spawn died on
    `stat /opt/ccc/env/bin/python`. Remote Arms only; the Head-local branch (`cp -a`) is
    unaffected."""
    stage_in = next(ln for ln in _script().splitlines()
                    if '--files-from="$scratch/filelist"' in ln)
    assert " -r " in stage_in


def test_tidepool_binds_the_environment_too():
    """A Tide Pool debugs against the env its Spawn would get, or it is not debugging what failed."""
    body = _script(tidepool_port=22001, tidepool_pubkey="ssh-ed25519 AAAA",
                   declared=env.Manifest("uv-project", "pyproject.toml"))
    assert "$envbind" in body
    assert f"{env.VENV_DIR}/bin" in body  # on PATH for the interactive shell


def test_the_tide_pool_path_follows_the_shape_that_was_built():
    """The bin dir is not one path any more. A Tide Pool that prepended ENV_MOUNT/bin for a uv Spawn
    would put an empty directory on PATH and hand the User the image's `python` — the one interpreter
    that is certainly not what their Spawn runs."""
    conda = _script(tidepool_port=22001, tidepool_pubkey="k",
                    declared=env.Manifest("conda", "environment.yml"))
    assert f'[ -x {env.ENV_MOUNT}/bin/python ]' in conda
    assert env.VENV_DIR not in conda


def test_a_spawn_with_no_environment_names_a_path_that_cannot_exist():
    """The `$py` branch is dead in that Spawn, but the Tide Pool's PATH test still RUNS — so what it
    tests for has to be absent. ENV_MOUNT is never bound when nothing was declared; /usr/bin would
    have passed and put the image's own bin first on PATH for no reason."""
    body = _script(tidepool_port=22001, tidepool_pubkey="k")
    assert f'[ -x {env.ENV_MOUNT}/bin/python ]' in body


# --- a Head that cannot build ----------------------------------------------
# TOOLS_DIR is populated by `cuttle setup --head`. A Head with an empty one now REFUSES rather than
# degrading: the degradation existed so Spawns that worked before Environments existed kept working,
# and there is no such Spawn left — every one declares, so falling back to the base image would mean
# running the User's work against something they did not ask for and cannot see.

@pytest.fixture
def no_tools(monkeypatch, tmp_path):
    """A Head with an empty TOOLS_DIR."""
    monkeypatch.setattr(env, "UV_BIN", tmp_path / "tools" / "uv")
    monkeypatch.setattr(env, "MICROMAMBA_BIN", tmp_path / "tools" / "micromamba")


@pytest.fixture
def with_tools(monkeypatch, tmp_path):
    """A Head that has both build tools installed, and a cache + scratch it may write to."""
    d = tmp_path / "tools"
    d.mkdir(parents=True, exist_ok=True)
    for name in ("uv", "micromamba"):
        (d / name).write_text("#!/bin/sh\n")
    monkeypatch.setattr(env, "UV_BIN", d / "uv")
    monkeypatch.setattr(env, "MICROMAMBA_BIN", d / "micromamba")
    monkeypatch.setattr(env, "TOOLS_DIR", d)
    monkeypatch.setattr(env, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(env, "TMP_DIR", tmp_path / "envtmp")


def test_an_unequipped_head_refuses_and_says_which_binary(no_tools, tmp_path):
    m = env.detect(_project(tmp_path, **{"requirements.txt": ""}), "requirements.txt")
    with pytest.raises(env.BuildError) as e:
        env.require_tool(m)
    assert "uv" in str(e.value)


def test_the_refusal_names_the_command_that_fixes_it(no_tools, tmp_path):
    """Only the operator can fix this, and the User meeting it needs to know that — otherwise they
    conclude their own manifest was wrong and go on editing it."""
    m = env.detect(_project(tmp_path, **{"requirements.txt": ""}), "requirements.txt")
    with pytest.raises(env.BuildError, match="cuttle setup --head"):
        env.require_tool(m)


def test_conda_asks_for_micromamba_not_uv(no_tools, tmp_path):
    m = env.detect(_project(tmp_path, **{"environment.yml": ""}), "environment.yml")
    with pytest.raises(env.BuildError, match="micromamba"):
        env.require_tool(m)


def test_an_equipped_head_refuses_nothing(with_tools, tmp_path):
    m = env.detect(_project(tmp_path, **{"requirements.txt": ""}), "requirements.txt")
    assert env.require_tool(m) is None


def test_the_build_can_reach_its_tools_inside_the_pod(with_tools, tmp_path, monkeypatch):
    """--contain gives the build nothing of the Head's filesystem but what is bound. uv and micromamba
    live on the HEAD, not in base.sif, so without this bind the argv names a binary that does not exist
    inside the Pod — and every declared Environment fails on a Head that installed them correctly."""
    seen = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(env.subprocess, "run", lambda cmd, **kw: (seen.append(cmd), R())[1])
    project = _project(tmp_path / "p", **{"requirements.txt": ""})
    env.build(project, env.detect(project, "requirements.txt"), "ccc-u60002")
    binds = [c[c.index("--bind") + 1] for c in seen if "--bind" in c]
    assert binds and all(f"{env.TOOLS_DIR}:{env.TOOLS_DIR}:ro" in b for b in binds)


class TestTheBuildScratchIsOnDisk:
    """`--contain` gives the Pod a /tmp carved from Apptainer's session directory — a RAM tmpfs
    capped by `sessiondir max size` (64 MB on this fleet). pip, run by an environment.yml's
    `pip:` section, downloads into TMPDIR; a wheel bigger than what's left of that 64 MB dies
    with ENOSPC (nvidia-cudnn-cu13 alone is 366 MB).

    Worth pinning precisely because the symptom lies: pip reports the ENOSPC as "Download failed
    after 6 attempts ... not pip", reading as flaky networking that retries can never fix."""

    def _build(self, tmp_path, monkeypatch):
        seen = []

        class R:
            returncode, stdout, stderr = 0, "", ""

        monkeypatch.setattr(env.subprocess, "run", lambda cmd, **kw: (seen.append(cmd), R())[1])
        project = _project(tmp_path / "p", **{"environment.yml": ""})
        env.build(project, env.detect(project, "environment.yml"), "ccc-u60002")
        return seen

    def test_tmpdir_is_moved_off_the_session_tmpfs(self, with_tools, tmp_path, monkeypatch):
        seen = self._build(tmp_path, monkeypatch)
        envs = [c[c.index("--env") + 1] for c in seen if "--env" in c]
        assert envs and all("TMPDIR=/ccc-tmp" in e for e in envs)

    def test_the_scratch_is_actually_bound(self, with_tools, tmp_path, monkeypatch):
        """TMPDIR naming a path that is not bound would be worse than leaving it alone — the tools
        would fail on a missing directory instead of a full one."""
        seen = self._build(tmp_path, monkeypatch)
        binds = [c[c.index("--bind") + 1] for c in seen if "--bind" in c]
        assert binds and all(":/ccc-tmp" in b for b in binds)

    def test_pips_download_cache_is_a_cache_not_scratch(self, with_tools, tmp_path, monkeypatch):
        """Under /ccc-cache, which persists and is pruned — not /ccc-tmp, which is deleted after every
        build. Otherwise pip re-downloads every wheel on every rebuild."""
        seen = self._build(tmp_path, monkeypatch)
        envs = [c[c.index("--env") + 1] for c in seen if "--env" in c]
        assert envs and all("PIP_CACHE_DIR=/ccc-cache/pip" in e for e in envs)

    def test_the_scratch_is_removed_when_the_build_ends(self, with_tools, tmp_path, monkeypatch):
        """Per-build and deleted in a `finally`: a timed-out build leaves partial downloads behind, and
        nothing ever looks in this tree again — prune_cache deliberately does not cover it."""
        self._build(tmp_path, monkeypatch)
        assert list(env.TMP_DIR.iterdir()) == []

    def test_it_survives_a_failed_build(self, with_tools, tmp_path, monkeypatch):
        """The failing build is exactly the one that leaves the biggest partial download behind."""
        class R:
            returncode, stdout, stderr = 1, "", "boom"

        monkeypatch.setattr(env.subprocess, "run", lambda cmd, **kw: R())
        project = _project(tmp_path / "p", **{"environment.yml": ""})
        with pytest.raises(env.BuildError):
            env.build(project, env.detect(project, "environment.yml"), "ccc-u60002")
        assert list(env.TMP_DIR.iterdir()) == []

    def test_the_scratch_is_not_swept_by_the_cache_reaper(self, with_tools, tmp_path, monkeypatch):
        """prune_cache empties the WHOLE cache when over the cap. With the scratch inside it, one
        build finishing would delete another's half-downloaded wheel out from under it."""
        assert env.TMP_DIR not in env.CACHE_DIR.parents
        assert env.CACHE_DIR not in env.TMP_DIR.parents


def _build_env_list(tmp_path, monkeypatch, **files):
    """The `--env` strings a build Pod is given, with the shell captured."""
    seen = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(env.subprocess, "run", lambda cmd, **kw: (seen.append(cmd), R())[1])
    project = _project(tmp_path / "p", **files)
    env.build(project, env.detect(project, next(iter(files))), "ccc-u60002")
    return [c[c.index("--env") + 1] for c in seen if "--env" in c]


def test_uv_sync_is_pointed_at_the_mount_by_variable_not_by_active(with_tools, tmp_path, monkeypatch):
    """`--active` only means "use $VIRTUAL_ENV", and nothing in a build Pod sets it — so uv falls
    back to the `.venv` it makes beside the manifest, inside the Mantle-owned project a Guise may
    only read, and every pyproject.toml/uv.lock Spawn dies on "Permission denied".
    UV_PROJECT_ENVIRONMENT names the path outright, as --python already did on the uv-pip branch."""
    cmds = env.render_build(env.Manifest("uv-project", "pyproject.toml"))
    assert not any("--active" in part for cmd in cmds for part in cmd)
    envs = _build_env_list(tmp_path, monkeypatch, **{"pyproject.toml": ""})
    assert envs and all(f"UV_PROJECT_ENVIRONMENT={env.VENV_DIR}" in e for e in envs)


class TestTheEnvCarriesItsOwnInterpreter:
    """The mechanism the whole restructure rests on. Both settings are on the build Pod rather than in
    the argv because all three build paths need them, including the one that runs no uv command at all
    until `uv sync` decides for itself which CPython to fetch."""

    def test_uv_may_not_adopt_an_interpreter_it_did_not_install(self, with_tools, tmp_path, monkeypatch):
        """Without only-managed, uv happily picks up a system python of the right version — and the venv
        it builds then points at a path that exists on the HEAD and not on the Arm. Confirmed against
        uv 0.12.1: `uv venv --python 3.12` took the host's 3.12.3 until this was set."""
        envs = _build_env_list(tmp_path, monkeypatch, **{"pyproject.toml": ""})
        assert envs and all("UV_PYTHON_PREFERENCE=only-managed" in e for e in envs)

    def test_the_downloaded_interpreter_lands_inside_the_shipped_tree(self, with_tools, tmp_path,
                                                                      monkeypatch):
        """Otherwise it goes to $HOME — which a **Guise** has not got — or to the Pod's 64MB session
        tmpfs, and either way the env that reaches the Arm has no interpreter to run."""
        envs = _build_env_list(tmp_path, monkeypatch, **{"pyproject.toml": ""})
        assert envs and all(f"UV_PYTHON_INSTALL_DIR={env.PY_INSTALL_DIR}" in e for e in envs)
        assert env.PY_INSTALL_DIR.startswith(env.ENV_MOUNT + "/")


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
    kw.setdefault("script", "train.py")
    kw.setdefault("manifest", names[0] if names else None)
    return spawns.submit(user_id=1, gpu=1, args=[], wandb_env={},
                         tar_path=_tar(tmp_path, "train.py", *names), minutes=60,
                         constraint="", outputs=["out"], **kw)


def _sbatch(calls):
    return next(c for c in calls if "sbatch" in c)


def test_a_spawn_that_declares_nothing_never_reaches_sbatch(head, tmp_path):
    """Refused, not held and not degraded — and refused before sbatch, so there is no Spawn to Escrow,
    nothing in the queue, and nothing for a reaper to come back for."""
    with pytest.raises(env.BuildError, match="declares no environment"):
        _submit(tmp_path)
    assert not [c for c in head if "sbatch" in c]


def test_a_tide_pool_may_decline_and_is_never_held(head, tmp_path):
    """The one exemption. A bare Tide Pool uploads an empty tar — there is no file in it to name — and
    it is a shell rather than a run, so nothing has to be built before it can start."""
    jid = _submit(tmp_path, tidepool=True, script=None)
    assert "--hold" not in _sbatch(head)
    with db.connect() as conn:
        assert db.get_spawn(conn, jid)["build_state"] is None


def test_a_tide_pool_that_declares_one_gets_it_built(head, tmp_path):
    """The exemption is from the REQUIREMENT, not from the feature. A Tide Pool opened over a project
    is how you debug the thing that failed, and that is only true if the shell holds the env the Spawn
    would have run in — so naming a manifest here holds and builds exactly like any other Spawn."""
    jid = _submit(tmp_path, "requirements.txt", tidepool=True, script=None)
    assert "--hold" in _sbatch(head)
    with db.connect() as conn:
        assert db.get_spawn(conn, jid)["build_state"] == db.BUILDING
    assert "requirements.txt" in spawns.environment_note(jid)


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


def test_a_tide_pool_that_declared_nothing_stages_no_env_dir(head, tmp_path, monkeypatch):
    """The other half: naming a directory that will never exist would make that Spawn's stage-in
    report a missing file. Only a Tide Pool can be in this state now."""
    seen = {}
    real = spawns._render
    # `files` is the 5th positional arg of _render, not a keyword — see spawns.submit's call.
    monkeypatch.setattr(spawns, "_render",
                        lambda *a, **kw: (seen.update(files=a[4]), real(*a, **kw))[1])
    _submit(tmp_path, tidepool=True, script=None)
    assert env.ENV_DIRNAME not in seen["files"]


def test_a_head_without_the_tools_refuses_before_sbatch(head, tmp_path, monkeypatch):
    """It used to submit anyway and run on the base image. That fallback was for Spawns written before
    Environments existed; none are left, so falling back now means running the User's work against
    something they never asked for. Refused where nothing is Escrowed and no Spawn exists.

    Un-installs uv after `head` (which provides one) rather than using the `no_tools` fixture:
    fixture order would otherwise put the tools back."""
    monkeypatch.setattr(env, "UV_BIN", tmp_path / "nowhere" / "uv")
    with pytest.raises(env.BuildError, match="cannot build Environments"):
        _submit(tmp_path, "pyproject.toml")
    assert not [c for c in head if "sbatch" in c]


def test_the_echo_is_written_for_every_spawn(head, tmp_path):
    jid = _submit(tmp_path, "requirements.txt")
    assert "requirements.txt" in spawns.environment_note(jid)


def test_a_caller_that_is_about_to_exit_builds_before_it_returns(head, tmp_path, monkeypatch):
    """`background_build=False` — for a caller with no future to build in.

    The build thread is a daemon, so a short-lived process (the `ccc_head_api.admin` CLI,
    submitting every Head-local pinned Spawn) takes it down at exit — and by then the Spawn is
    `--hold`ed: not merely unbuilt, but unrunnable, with nothing left alive to release it. It
    pends as JobHeldUser until someone notices — the shape of a hang, not a failure.
    """
    monkeypatch.setattr(env, "build", lambda *a, **kw: "solved 3 packages")
    monkeypatch.setattr(env, "prune_cache", lambda: None)
    jid = _submit(tmp_path, "requirements.txt", background_build=False)
    assert "--hold" in _sbatch(head), "the interlock still applies — it is just resolved inline"
    assert ["sudo", "-n", "-u", "ccc-u60002", "scontrol", "release", str(jid)] in head
    with db.connect() as conn:
        assert db.get_spawn(conn, jid)["build_state"] is None


def test_neither_the_build_nor_sbatch_inherits_the_operators_directory(head, tmp_path, monkeypatch):
    """Both run as a **Guise**, with no business in the directory `cuttle` was invoked from — on
    the Head-local path, the operator's own project, unreadable to a Guise.

    apptainer's starter chdirs to the inherited cwd on the HOST before `--pwd` applies inside, so
    the build dies as "container creation failed: can't change directory to
    /home/<operator>/..." — a message about the caller, saying nothing about either. Slurm takes
    the job's WorkDir from the submitting process too, stranding it the same way one step later.
    """
    seen = {}
    real = spawns.subprocess.run

    def watch(argv, **kw):
        # Only these two care: everything else here (setfacl, scontrol) names absolute paths or is an
        # RPC, and pinning their cwd would assert nothing.
        for exe in ("apptainer", "sbatch"):
            if exe in argv:
                seen[exe] = kw.get("cwd")
        return real(argv, **kw)

    monkeypatch.setattr(spawns.subprocess, "run", watch)
    monkeypatch.setattr(env, "prune_cache", lambda: None)
    _submit(tmp_path, "requirements.txt", background_build=False)
    assert seen == {"sbatch": "/", "apptainer": "/"}, f"inherited the caller's cwd: {seen}"


def test_backgrounding_is_the_default_and_releases_nothing_by_itself(head, tmp_path):
    """The other half of the same contract: the API service DOES background its build, so `submit`
    returns with the Spawn still held and the release owed by a thread. Asserted so the fix above
    cannot quietly become "build synchronously for everyone" — that would put a cold conda solve
    inside the HTTP request every submitter waits on."""
    jid = _submit(tmp_path, "requirements.txt")
    assert not [c for c in head if "release" in c]
    with db.connect() as conn:
        assert db.get_spawn(conn, jid)["build_state"] == db.BUILDING


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


# --- packages an Arm Feature supplies ---------------------------------------
# A **Feature**'s package is licensed to the Arm, not to the submitter, so the copy that RUNS has to be
# the Arm's own. The submitter still has to be able to declare it or their project will not resolve —
# so CCC accepts the declaration, replaces the wheel with an empty stub before building, and lets the
# Feature put the real one back on the Arm (features.VLEARN_SETUP). What is pinned here is that the
# stub never becomes a working package, and that everything decidable about it is decided at SUBMIT.

import zipfile


def _sourced(tmp_path, path="../wheels/vlearn-0.3.9-cp311-cp311-linux_x86_64.whl"):
    """A project whose pyproject.toml path-sources vlearn, laid out like a real staging dir."""
    proj = tmp_path / "projects" / "abc"
    proj.mkdir(parents=True)
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "t"\nversion = "0"\nrequires-python = "==3.11.14"\n'
        'dependencies = ["vlearn"]\n\n'
        f'[tool.uv.sources]\nvlearn = {{ path = "{path}" }}\n'
    )
    return proj


def test_a_features_package_may_be_declared_as_a_path(tmp_path):
    """The driver for this whole design: a real project pinning its own local wheel has to submit
    unmodified. Recognising it is what makes the substitution possible at all."""
    proj = _sourced(tmp_path)
    found = env.supplied(proj, env.Manifest(kind="uv-project", path="pyproject.toml"))
    assert [(s.package, s.feature) for s in found] == [("vlearn", "vlearn")]
    assert found[0].filename == "vlearn-0.3.9-cp311-cp311-linux_x86_64.whl"
    assert not found[0].inside_project, "../wheels is beside the project, not in it"


def test_an_ordinary_project_declares_nothing_supplied(tmp_path):
    proj = _project(tmp_path, **{"pyproject.toml": '[project]\nname = "t"\nversion = "0"\n'})
    assert env.supplied(proj, env.Manifest(kind="uv-project", path="pyproject.toml")) == []


def test_declaring_a_features_package_without_the_feature_is_refused(tmp_path):
    """The Spawn would build cleanly, place on any Arm, and die at `import` — after the User paid to
    hold a GPU. Everything needed to know that is on the Head before the Spawn exists."""
    proj = _sourced(tmp_path)
    m = env.Manifest(kind="uv-project", path="pyproject.toml")
    with pytest.raises(env.BuildError) as e:
        env.check_supplied(proj, m, requested=[])
    assert "--feature vlearn" in str(e.value)
    assert env.check_supplied(proj, m, requested=["vlearn"])


def test_a_path_source_may_not_reach_out_of_the_staging_root(tmp_path):
    """The stub's directory is BOUND into the build Pod at the path the source resolves to. Without
    this rule a `path` of `../../../etc` would name a system directory as the mountpoint."""
    proj = _sourced(tmp_path, path="../../../../etc/vlearn-0.3.9-py3-none-any.whl")
    with pytest.raises(env.BuildError) as e:
        env.check_supplied(proj, env.Manifest(kind="uv-project", path="pyproject.toml"), ["vlearn"])
    assert "outside" in str(e.value)


def test_a_sibling_directory_is_as_far_as_a_path_source_reaches(tmp_path):
    """One `..` is the ordinary layout — a checkout beside the project — and has to keep working, or
    the rule above would refuse the very case it exists to serve."""
    proj = _sourced(tmp_path)
    env.check_supplied(proj, env.Manifest(kind="uv-project", path="pyproject.toml"), ["vlearn"])


def test_only_a_wheel_can_be_stubbed(tmp_path):
    proj = _sourced(tmp_path, path="../vlearn-main")
    with pytest.raises(env.BuildError) as e:
        env.check_supplied(proj, env.Manifest(kind="uv-project", path="pyproject.toml"), ["vlearn"])
    assert "not a wheel" in str(e.value)


class TestTheStubIsNotAWorkingPackage:
    """What the licensing rests on. The stub exists to satisfy a resolver and must never be something
    `import vlearn` could succeed against."""

    def _built(self, tmp_path):
        proj = _sourced(tmp_path)
        work = tmp_path / "work"
        work.mkdir()
        binds, log = env.substitute_supplied(
            proj, env.Manifest(kind="uv-project", path="pyproject.toml"), work)
        wheel = next(work.rglob("*.whl"))
        return proj, binds, log, wheel

    def test_the_package_it_installs_is_empty(self, tmp_path):
        _, _, _, wheel = self._built(tmp_path)
        with zipfile.ZipFile(wheel) as z:
            assert z.read("vlearn/__init__.py") == b""
            assert not any(n.endswith(".so") for n in z.namelist())

    def test_it_answers_to_the_name_and_version_the_user_declared(self, tmp_path):
        """Read out of the FILENAME, never out of the real wheel — the Head has never seen that file.
        It is on the submitter's own machine and is not uploaded, which is why this is synthesized
        rather than emptied."""
        _, _, _, wheel = self._built(tmp_path)
        with zipfile.ZipFile(wheel) as z:
            meta = z.read("vlearn-0.3.9.dist-info/METADATA").decode()
            assert "Name: vlearn" in meta and "Version: 0.3.9" in meta
            # The compatibility tag has to match too, or uv rejects the wheel as unusable here.
            assert "Tag: cp311-cp311-linux_x86_64" in z.read("vlearn-0.3.9.dist-info/WHEEL").decode()

    def test_a_wheel_outside_the_project_is_bound_in_at_the_path_it_resolves_to(self, tmp_path):
        proj, binds, _, wheel = self._built(tmp_path)
        assert binds == [f"{wheel.parent}:{(proj / '../wheels').resolve()}"]

    def test_the_substitution_is_said_out_loud_in_the_build_log(self, tmp_path):
        """A User who declared a wheel and got someone else's build has to be able to find out why."""
        _, _, log, _ = self._built(tmp_path)
        assert any("vlearn" in ln and "Arm Feature" in ln for ln in log)


def test_a_wheel_inside_the_project_is_overwritten_where_it_lies(tmp_path):
    """The upload carried a real wheel. It needs no bind — the project is already mounted — and the
    stub replaces it, so what gets installed is the same either way."""
    proj = _sourced(tmp_path, path="wheels/vlearn-0.3.9-cp311-cp311-linux_x86_64.whl")
    real = proj / "wheels" / "vlearn-0.3.9-cp311-cp311-linux_x86_64.whl"
    real.parent.mkdir()
    real.write_bytes(b"the user's real, licensed wheel")
    work = tmp_path / "work"
    work.mkdir()
    binds, _ = env.substitute_supplied(
        proj, env.Manifest(kind="uv-project", path="pyproject.toml"), work)
    assert binds == []
    assert zipfile.is_zipfile(real), "the real wheel was replaced by the stub in place"


def test_the_lock_is_repointed_at_the_stub(tmp_path):
    """Not an optimisation. uv records a sha256 of a path-sourced wheel and verifies it on EVERY sync,
    plain or --locked; a mismatch is a hard failure with no re-resolve escape."""
    import hashlib

    proj = _sourced(tmp_path)
    (proj / "uv.lock").write_text(
        'version = 1\n\n'
        '[[package]]\nname = "pyfiglet"\nversion = "1.0.4"\n'
        'wheels = [{ url = "https://x/y.whl", hash = "sha256:' + "a" * 64 + '" }]\n\n'
        '[[package]]\nname = "vlearn"\nversion = "0.3.9"\n'
        'source = { path = "../wheels/vlearn-0.3.9-cp311-cp311-linux_x86_64.whl" }\n'
        'wheels = [{ filename = "vlearn-0.3.9-cp311-cp311-linux_x86_64.whl", '
        'hash = "sha256:' + "b" * 64 + '" }]\n'
    )
    work = tmp_path / "work"
    work.mkdir()
    env.substitute_supplied(proj, env.Manifest(kind="uv-project", path="pyproject.toml"), work)
    stub = next(work.rglob("*.whl"))
    text = (proj / "uv.lock").read_text()
    assert f"sha256:{hashlib.sha256(stub.read_bytes()).hexdigest()}" in text
    # Every OTHER package stays frozen at exactly what it was resolved to.
    assert "sha256:" + "a" * 64 in text
    assert "sha256:" + "b" * 64 not in text


# --- the Feature's own system libraries -------------------------------------
# `libgym.so` links SDL2, assimp, zeromq, czmq, GLU and libcudart, resolved at LOAD time. These used to
# come from the submitter's `environment.yml` — which is WHY a vlearn Spawn had to be a conda Spawn.
# Now that an Environment carries its own interpreter, the manifest can be anything, and uv has no way
# to install libassimp.so.5. So CCC builds them itself, into a prefix of its own.


def test_a_uv_spawn_asking_for_a_feature_still_needs_micromamba(with_tools, tmp_path):
    """The libraries are a conda prefix whatever the manifest is. A Head with uv but no micromamba
    could build the Environment and not the Feature — and finding that out at BUILD time would cost the
    User an Escrow for a Head that was never fully provisioned."""
    env.MICROMAMBA_BIN.unlink()
    m = env.Manifest(kind="uv-project", path="pyproject.toml")
    env.require_tool(m, [])              # no Feature: uv alone is enough
    env.require_tool(m, ["isaaclab"])    # a Feature with no libraries needs nothing extra
    with pytest.raises(env.BuildError) as e:
        env.require_tool(m, ["vlearn"])
    assert "micromamba" in str(e.value)


def test_a_feature_with_no_libraries_asks_for_nothing_extra(with_tools):
    from ccc_head_api import features as hf
    assert hf.lib_specs(["isaaclab"]) == {}
    assert hf.lib_specs([])  == {}
    assert "cuda-cudart=13" in hf.lib_specs(["vlearn"])["vlearn"]


def test_the_library_prefix_is_built_at_the_path_it_is_later_bound_at(with_tools, tmp_path,
                                                                     monkeypatch):
    """A conda prefix bakes its own path into hundreds of files and does not survive being moved — the
    same invariant the Environment rests on, for the same reason."""
    from ccc_head_api import features as hf

    seen = []
    monkeypatch.setattr(env.subprocess, "run",
                        lambda cmd, **kw: seen.append(cmd) or _ok())
    proj = _project(tmp_path, **{"pyproject.toml": "x"})
    env.build_feature_libs(proj, {"vlearn": ["sdl2"]}, "ccc-u1", tmp_path / "t")
    argv = seen[0]
    assert "--prefix" in argv and f"{hf.FEATLIB_MOUNT}/vlearn" in argv
    bind = argv[argv.index("--bind") + 1]
    assert f"{proj / env.FEATLIB_DIRNAME}:{hf.FEATLIB_MOUNT}" in bind


def test_the_library_prefix_installs_no_interpreter(with_tools):
    """It is bound for its lib/ directory alone and never run. A Python in here would be a second one
    on the box for no reason — and the reason it used to need one (a pip section) is gone."""
    from ccc_head_api import features as hf
    assert not any(s.startswith("python") for s in hf.lib_specs(["vlearn"])["vlearn"])


def _ok():
    class R:
        returncode, stdout, stderr = 0, "", ""
    return R()
