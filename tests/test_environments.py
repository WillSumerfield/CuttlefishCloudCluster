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

import base64
import io
import shlex
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
        with pytest.raises(env.BuildError, match="declares no Environment"):
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
    """This path is joined onto a directory the Head owns, and `..` must not be a way out of it."""
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
    Spawn ran — which is not a freeze."""
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


def test_every_uv_env_is_seeded_with_pip_without_overriding_a_declared_version(tmp_path):
    """A package no manifest asked for: `pip`, because uv does not seed one and a **Tide Pool**
    promises `pip install` at the shell works.

    UNPINNED: an already-satisfied requirement is a no-op, so a User who declared their own version
    keeps it — as they did when the image supplied one."""
    for name in ("uv.lock", "requirements.txt"):
        cmds = env.render_build(env.detect(_project(tmp_path / name, **{name: ""}), name))
        seed = next(c for c in cmds if "pip" in c and "install" in c)
        assert not any("==" in part for part in seed), name


def test_pip_is_seeded_with_uv_not_with_pip(tmp_path):
    """The bootstrap order that makes it possible at all: `python -m pip install pip` cannot run in an
    env that has no pip yet. `uv pip` needs nothing in the target env."""
    cmds = env.render_build(env.detect(_project(tmp_path, **{"uv.lock": ""}), "uv.lock"))
    seed = next(c for c in cmds if "pip" in c and "install" in c)
    assert seed[0] == str(env.UV_BIN) and "-m" not in seed


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
    return spawns._render(staging=Path("/tmp/u"), gpu=1, script="train.py", args=[], files=[],
                          token="a3f9", secret="s3cr3t", **kw)


def _declared(**kw):
    kw.setdefault("declared", env.Manifest("uv-requirements", "requirements.txt"))
    return _script(**kw)


def test_the_build_block_appears_exactly_once():
    """Substitution is a plain `.replace()`, which does not know a comment from code. Naming the build
    block's token in JOB_SCRIPT's own PROSE therefore splices a whole extra copy in wherever it is
    mentioned — with that copy's first line commented out by the `# ` in front of it and every later
    line live.

    That shipped. The commented-out line was the `mkdir` that creates `$scratch/env`, so every declared
    Spawn reached `apptainer --bind` with a source that did not exist and died on
    `mount source ... doesn't exist`. Found on hardware 2026-08-08 (Spawn 2, the vlearn activation)."""
    body = _declared()
    assert body.count('mkdir -p "$scratch/env"') == 1
    assert body.count("ccc_build() {") == 1
    # And the mkdir is LIVE, not commented — the exact shape of the bug.
    assert any(ln.startswith('mkdir -p "$scratch/env"') for ln in body.splitlines())


def test_no_substitution_token_survives_rendering():
    """A token left in the script is a literal `%%NAME%%` reaching bash. The build block is the risky
    one — it is spliced in before the main pass, so anything it introduces must be substituted by a
    later key rather than left behind."""
    import re
    for body in (_script(), _declared(),
                 _script(tidepool_port=22001, tidepool_pubkey="k",
                         declared=env.Manifest("conda", "environment.yml"))):
        assert re.findall(r"%%[A-Z_]+%%", body) == []


def test_a_spawn_that_declared_nothing_renders_no_build_at_all():
    """The Head knows at submit whether there is anything to build, so the no-env case is ABSENT rather
    than an inert run-time branch. `$py` stays empty and there is nothing to degrade to — the image has
    no interpreter (cuttle.templates.BASE_DEF_TMPL)."""
    body = _script()
    assert 'py=""; envbind=""; envmount=""' in body
    assert "ccc_build_or_die" not in body
    assert 'py="python"' not in body, "the image's python came back — nothing supplies one now"


def test_the_environment_is_built_outside_the_project():
    """$proj is the User's project; CCC's own env stays out of it. Built straight into $scratch."""
    body = _declared()
    assert 'mkdir -p "$scratch/env" "$scratch/tmp"' in body
    assert f"$scratch/env:{env.ENV_MOUNT}" in body


def test_environment_is_bound_at_the_path_it_was_built_at():
    """The one property the design rests on — see test_every_build_targets_the_one_fixed_mount. Built
    and run on the same box in the same job now, so the two binds are the same string."""
    body = _declared()
    assert f'envbind=",$scratch/env:{env.ENV_MOUNT}"' in body
    assert f'--bind "$proj,$scratch/env:{env.ENV_MOUNT},' in body, "the build binds it at the same path"


def test_payload_runs_the_environments_interpreter_and_binds_it_together():
    """`$py` and `$envbind` are set in the same branch, so the interpreter named can never be one the
    bind did not provide — a Pod that mounted no env but ran /opt/ccc/env/bin/python fails with a
    file-not-found the User cannot act on."""
    body = _script()
    assert '"$sif" $py train.py' in body
    assert '--bind "$proj,$scratch/tmp:/ccc-tmp$envbind$licbind$featbind$migbind"' in body


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


def test_the_temp_scratch_is_outside_the_project():
    """Temp files are CCC's, not the User's project's."""
    body = _script()
    assert '"$scratch/tmp' in body and '"$proj/tmp' not in body


def test_the_remote_stage_in_recurses():
    """`--files-from` implies `--dirs` but NOT `--recursive` (rsync(1): `-a` doesn't imply `-r` here),
    so any bare directory name in the list would arrive EMPTY. Nothing built is named that way any more,
    but a project of the User's own can still contain a directory, and losing its contents silently is
    the same bug. Remote Arms only; the Head-local branch (`cp -a`) is unaffected."""
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
    # The PATH line only, not the whole script: the build Pod's own env names VENV_DIR unconditionally
    # (it is inert on the conda path), and what must not be wrong is what the shell puts on PATH.
    path_line = next(ln for ln in conda.splitlines() if "export PATH" in ln)
    assert f"{env.ENV_MOUNT}/bin" in path_line
    assert env.VENV_DIR not in path_line


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
    """A node that has both build tools installed, and a download cache it may write to."""
    d = tmp_path / "tools"
    d.mkdir(parents=True, exist_ok=True)
    for name in ("uv", "micromamba"):
        (d / name).write_text("#!/bin/sh\n")
    monkeypatch.setattr(env, "UV_BIN", d / "uv")
    monkeypatch.setattr(env, "MICROMAMBA_BIN", d / "micromamba")
    monkeypatch.setattr(env, "TOOLS_DIR", d)
    monkeypatch.setattr(env, "CACHE_DIR", tmp_path / "cache")


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


# --- the build Pod, as rendered into the job script -------------------------
# The build runs on the ARM now, inside the Spawn's own job, so what there is to check is the shell the
# Head renders — not a subprocess it launches. Nothing here executes anything.

def _block(kind="uv-requirements", path="requirements.txt", features=(), files=(), binds=(), log=(),
           declared=...):
    """The %%ENV_BUILD%% text for one declaration.

    The three script-wide tokens are substituted here the way `_render`'s own pass does a moment later —
    they are written once for the whole job script, and a test reading `%%ENV_MOUNT%%` back would be
    pinning the seam rather than the behaviour."""
    from ccc_head_api import features as _hf
    m = env.Manifest(kind, path) if declared is ... else declared
    text = spawns._render_env_build(m, Path("/tmp/u"), list(features), list(files), list(binds),
                                    list(log))
    for tok, val in (("%%ENV_MOUNT%%", env.ENV_MOUNT),
                     ("%%FEATLIB_MOUNT%%", _hf.FEATLIB_MOUNT),
                     ("%%ENV_INTERPRETER%%", m.interpreter if m else "")):
        text = text.replace(tok, val)
    return text


def _pod_binds(block):
    """The --bind argument of the build Pod, as one string."""
    line = next(ln for ln in block.splitlines() if ln.strip().startswith("--bind"))
    return line.strip()


def test_the_build_can_reach_its_tools_inside_the_pod(with_tools):
    """--contain gives the build nothing of the node's filesystem but what is bound. uv and micromamba
    are installed beside base.sif, not inside it, so without this bind the argv names a binary that does
    not exist in the Pod — and every declared Environment fails on a node provisioned correctly."""
    assert f"{env.TOOLS_DIR}:{env.TOOLS_DIR}:ro" in _pod_binds(_block())


def test_the_tools_are_bound_read_only(with_tools):
    """A build runs the submitter's own code as their **Guise**. It may not rewrite the binaries the
    next Spawn's build will run."""
    assert f"{env.TOOLS_DIR}:{env.TOOLS_DIR}:ro" in _pod_binds(_block())


def test_the_build_gets_the_same_walls_the_payload_gets(with_tools):
    """A manifest is code — build backends, sdist setup.py, conda post-link scripts all execute — so it
    is contained exactly as the work is, not by a weaker isolation invented for it. No --nv: a build
    needs no GPU, even though this job already holds one."""
    block = _block()
    assert 'apptainer exec --contain --pid --pwd "$proj"' in block
    assert "--nv" not in block


class TestTheBuildScratchIsOnDisk:
    """`--contain` gives the Pod a /tmp carved from Apptainer's session directory — a RAM tmpfs
    capped by `sessiondir max size` (64 MB on this fleet). pip, run by an environment.yml's
    `pip:` section, downloads into TMPDIR; a wheel bigger than what's left of that 64 MB dies
    with ENOSPC (nvidia-cudnn-cu13 alone is 366 MB).

    Worth pinning precisely because the symptom lies: pip reports the ENOSPC as "Download failed
    after 6 attempts ... not pip", reading as flaky networking that retries can never fix."""

    def test_tmpdir_is_moved_off_the_session_tmpfs(self, with_tools):
        assert f"TMPDIR={env.POD_TMP}" in _block("conda", "environment.yml")

    def test_the_scratch_is_actually_bound(self, with_tools):
        """TMPDIR naming a path that is not bound would be worse than leaving it alone — the tools
        would fail on a missing directory instead of a full one."""
        assert f"$scratch/tmp:{env.POD_TMP}" in _pod_binds(_block("conda", "environment.yml"))

    def test_pips_download_cache_is_a_cache_not_scratch(self, with_tools):
        """Under the node's shared cache, which persists between Spawns — not under the per-Spawn
        scratch, which dies with the job. Otherwise pip re-downloads every wheel every time."""
        assert f"PIP_CACHE_DIR={env.POD_CACHE}/pip" in _block("conda", "environment.yml")

    def test_the_scratch_dies_with_the_spawn(self, with_tools):
        """Per-Spawn, inside $scratch, which the job script's EXIT trap already rm -rf's. A build that
        times out or fails leaves the biggest partial download behind, and nothing looks here again."""
        assert "$scratch/tmp" in _block()

    def test_the_scratch_is_not_inside_the_cache(self, with_tools):
        """The prune empties the WHOLE cache subtree at the cap. With this Spawn's scratch inside it, a
        later build of the same User's would delete a half-downloaded wheel out from under it."""
        assert not str(env.CACHE_DIR).startswith("$scratch")
        assert f"$scratch/tmp:{env.POD_TMP}" in _pod_binds(_block())
        assert f"$ccc_cache:{env.POD_CACHE}" in _pod_binds(_block())


class TestTheNodeCacheIsWhatMakesThisAffordable:
    """Every Spawn rebuilds. That is only tolerable because the DOWNLOADS are kept: uv and micromamba
    hard-link out of this tree, so the second Spawn to want a given torch fetches nothing."""

    def test_the_cache_is_bound_into_every_build(self, with_tools):
        assert f"$ccc_cache:{env.POD_CACHE}" in _pod_binds(_block())

    def test_every_tool_is_pointed_at_it(self, with_tools):
        block = _block()
        for var in (f"UV_CACHE_DIR={env.POD_CACHE}/uv",
                    f"MAMBA_ROOT_PREFIX={env.POD_CACHE}/mamba",
                    f"PIP_CACHE_DIR={env.POD_CACHE}/pip"):
            assert var in block

    def test_it_is_pruned_before_the_build_not_after(self, with_tools):
        """After would throw away exactly what this Spawn just downloaded, on the Spawn that paid for
        it. Whole-subtree rather than per-entry: both tools hard-link out, so evicting one entry safely
        would need their own bookkeeping."""
        block = _block()
        prune = block.index('rm -rf "$ccc_cache"/*')
        assert prune < block.index("ccc_build_or_die")
        assert str(env.CACHE_MAX_GB * 1024**3) in block

    def test_each_guise_gets_its_own_subtree(self, with_tools):
        """One shared cache fails twice. uv builds its tree with the CREATOR's ownership and a 0755
        umask, so the first uid to build locks every later one out — that shipped, and every declared
        Spawn on the box died with "failed to open file `/ccc-cache/uv/sdists-v9/.git`: Permission
        denied" (hardware, 2026-08-08). And a cache two Users can both write is a route for one to
        plant a wheel the other's build hard-links into their Environment.

        `id -un`, not a name rendered by the Head: the job already RUNS as the Guise, so the directory
        cannot disagree with the uid writing into it."""
        block = _block()
        assert f'ccc_cache="{env.CACHE_DIR}/$(id -un)"' in block
        assert 'mkdir -p "$ccc_cache"' in block
        # The bare parent is never handed to a build — only the per-Guise subtree under it.
        assert f"{env.CACHE_DIR}:{env.POD_CACHE}" not in block

    def test_the_prune_only_ever_touches_this_guises_own_cache(self, with_tools):
        """A prune reaching the shared parent would let one User empty everyone else's cache on that
        box — costing them nothing and every other User a cold rebuild."""
        block = _block()
        assert f'rm -rf "{env.CACHE_DIR}"' not in block
        assert f'rm -rf "{env.CACHE_DIR}/' not in block


def test_uv_sync_is_pointed_at_the_mount_by_variable_not_by_active(with_tools):
    """`--active` only means "use $VIRTUAL_ENV", and nothing in a build Pod sets it — so uv falls
    back to the `.venv` it makes beside the manifest, inside the Mantle-owned project a Guise may
    only read, and every pyproject.toml/uv.lock Spawn dies on "Permission denied".
    UV_PROJECT_ENVIRONMENT names the path outright, as --python already did on the uv-pip branch."""
    cmds = env.render_build(env.Manifest("uv-project", "pyproject.toml"))
    assert not any("--active" in part for cmd in cmds for part in cmd)
    assert f"UV_PROJECT_ENVIRONMENT={env.VENV_DIR}" in _block("uv-project", "pyproject.toml")


class TestTheEnvCarriesItsOwnInterpreter:
    """The mechanism the whole restructure rests on. Both settings are on the build Pod rather than in
    the argv because all three build paths need them, including the one that runs no uv command at all
    until `uv sync` decides for itself which CPython to fetch."""

    def test_uv_may_not_adopt_an_interpreter_it_did_not_install(self, with_tools):
        """Without only-managed, uv happily picks up a system python of the right version — one that
        exists on this box and not inside the Pod, so the venv points at nothing runnable. Confirmed
        against uv 0.12.1: `uv venv --python 3.12` took the host's 3.12.3 until this was set."""
        assert "UV_PYTHON_PREFERENCE=only-managed" in _block("uv-project", "pyproject.toml")

    def test_the_downloaded_interpreter_lands_inside_the_shipped_tree(self, with_tools):
        """Otherwise it goes to $HOME — which a **Guise** has not got — or to the Pod's 64MB session
        tmpfs, and either way the env has no interpreter to run."""
        assert f"UV_PYTHON_INSTALL_DIR={env.PY_INSTALL_DIR}" in _block("uv-project", "pyproject.toml")
        assert env.PY_INSTALL_DIR.startswith(env.ENV_MOUNT + "/")


class TestAFailedBuildIsAFailedSpawn:
    """The deliberate cost of building on the Arm. The Head used to refuse a broken manifest before the
    Spawn existed; now it is accepted, Escrowed, queued and placed, and the failure happens there."""

    def test_it_ends_the_spawn_the_way_every_other_early_exit_does(self, with_tools):
        """The same four things a failed stage-in does: say why in `err`, record a non-zero `exit`,
        stop the periodic pusher, and stage out. Anything less is a Spawn the Head reports as lost."""
        block = _block()
        fail = block[block.index("ccc_build_or_die() {"):]
        for line in ('>> "$err"', 'echo 1 > "$exitf"', 'kill "$loop_pid" 2>/dev/null',
                     "stage_out", "exit 1"):
            assert line in fail

    def test_the_failure_names_which_of_the_two_builds_failed(self, with_tools):
        """A Feature's conda solve and the submitter's own manifest fail in the same function, and
        blaming the manifest for CCC's own prefix would send a User editing a file that is fine."""
        block = _block(features=["vlearn"])
        assert "ccc_building='the environment declared in requirements.txt'" in block
        feat = next(ln for ln in block.splitlines()
                    if ln.startswith("ccc_building=") and "requirements" not in ln)
        assert shlex.split(feat.split("=", 1)[1])[0] == "the 'vlearn' Arm Feature's system libraries"

    def test_the_build_output_is_the_spawns_own_stderr(self, with_tools):
        """No separate build log to fetch. It is the answer to "why did my Spawn get that version" on
        success and to "why did it not run" on failure, and one place to look beats two — `cuttle logs`
        and the Spawn page both already show it, live."""
        assert '"$sif" "$@" >> "$err" 2>&1' in _block()


# --- submit: what the Head decides, and what it hands the Arm ----------------
# There is no hold and no build thread any more. What `submit` still owes is every REFUSAL that can be
# decided from the declaration, made before the Spawn exists — and a job script that carries the build
# the Arm will run.

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
    return spawns.submit(user_id=1, gpu=1, args=[],
                         tar_path=_tar(tmp_path, "train.py", *names), minutes=60,
                         constraint="", **kw)


def _sbatch(calls):
    return next(c for c in calls if "sbatch" in c)


def _body(calls):
    """The job script `submit` fed sbatch on stdin — captured through _render, since the fake shell
    never sees the input."""
    return calls  # placeholder; see _rendered below


def test_a_spawn_that_declares_nothing_never_reaches_sbatch(head, tmp_path):
    """Refused, not degraded — and refused before sbatch, so there is no Spawn to Escrow, nothing in
    the queue, and nothing for a reaper to come back for. This is the one class of build problem that
    is still caught for free, and it is why detection stayed on the Head."""
    with pytest.raises(env.BuildError, match="declares no Environment"):
        _submit(tmp_path)
    assert not [c for c in head if "sbatch" in c]


def test_no_spawn_is_ever_held(head, tmp_path):
    """The hold existed to keep Slurm from placing a Spawn before the Head had finished building its
    env. The Arm builds inside its own allocation now, so a held Spawn would be pure latency — and one
    with nothing alive to release it."""
    _submit(tmp_path, "requirements.txt")
    assert "--hold" not in _sbatch(head)


def test_a_tide_pool_may_decline(head, tmp_path):
    """The one exemption. A bare Tide Pool uploads an empty tar — there is no file in it to name — and
    it is a shell rather than a run."""
    jid = _submit(tmp_path, tidepool=True, script=None)
    assert "--hold" not in _sbatch(head)
    assert "none declared" in spawns.environment_note(jid)


def test_a_tide_pool_that_declares_one_gets_it_built(head, tmp_path):
    """The exemption is from the REQUIREMENT, not from the feature. A Tide Pool opened over a project
    is how you debug the thing that failed, and that is only true if the shell holds the env the Spawn
    would have run in — so naming a manifest here builds exactly like any other Spawn."""
    jid = _submit(tmp_path, "requirements.txt", tidepool=True, script=None)
    assert "requirements.txt" in spawns.environment_note(jid)


def _rendered(monkeypatch):
    """Capture what `submit` passed to `_render`, and what came back."""
    seen = {}
    real = spawns._render

    def watch(*a, **kw):
        seen["files"] = a[4]          # the 5th positional arg — see spawns.submit's call
        seen["body"] = real(*a, **kw)
        return seen["body"]

    monkeypatch.setattr(spawns, "_render", watch)
    return seen


def test_the_job_script_carries_the_build(head, tmp_path, monkeypatch):
    """The whole handover. The Head resolved the manifest into commands; the Arm runs them."""
    seen = _rendered(monkeypatch)
    _submit(tmp_path, "requirements.txt")
    assert "ccc_build_or_die" in seen["body"]
    assert "requirements.txt" in seen["body"]


def test_nothing_built_is_in_what_a_remote_arm_stages_in(head, tmp_path, monkeypatch):
    """The Environment used to ride the project transfer as a bare directory entry — several GB across
    the mesh on every Spawn, which is the whole reason the build moved. Nothing built is named here now;
    the Arm makes it in its own node-local scratch."""
    seen = _rendered(monkeypatch)
    _submit(tmp_path, "requirements.txt")
    assert sorted(seen["files"]) == ["requirements.txt", "train.py"]


def test_a_node_without_the_tools_refuses_before_sbatch(head, tmp_path, monkeypatch):
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


def test_sbatch_does_not_inherit_the_operators_directory(head, tmp_path, monkeypatch):
    """It runs as a **Guise**, with no business in the directory `cuttle` was invoked from — on the
    Head-local path, the operator's own project, unreadable to a Guise. Slurm takes the job's WorkDir
    from the submitting process, so inheriting it strands the Spawn one step later."""
    seen = {}
    real = spawns.subprocess.run

    def watch(argv, **kw):
        if "sbatch" in argv:
            seen["sbatch"] = kw.get("cwd")
        return real(argv, **kw)

    monkeypatch.setattr(spawns.subprocess, "run", watch)
    _submit(tmp_path, "requirements.txt")
    assert seen == {"sbatch": "/"}, f"inherited the caller's cwd: {seen}"


def test_a_spawn_installing_its_dependencies_is_simply_running(head, tmp_path, monkeypatch):
    """squeue is the whole truth again. A Spawn used to be held while the Head built for it, reported
    as PENDING, and CCC had to keep a `build_state` to tell that apart from waiting for a card. The
    build happens inside the allocation now, so Slurm can see it and there is nothing left to override."""
    from ccc_head_api import slurm
    jid = _submit(tmp_path, "requirements.txt")
    monkeypatch.setattr(slurm, "_slurm_json", lambda *a, **kw: {"jobs": [
        {"job_id": jid, "partition": slurm.PARTITION, "job_state": ["RUNNING"], "name": "t",
         "nodes": "arm1", "user_name": "ccc-u60002", "time_limit": 60},
    ]})
    assert slurm.spawns()[0]["state"] == "running"
    assert slurm.queue()[0]["state"] == "RUNNING"


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


def test_a_path_source_may_not_reach_out_of_the_spawns_own_scratch(tmp_path):
    """The stub is BOUND at `$proj/<pod_rel>`, and `$proj`'s parent is the Spawn's node-local scratch.
    One `..` reaches that scratch; without this rule a `path` of `../../../etc` would name a directory
    outside the Spawn altogether as the mountpoint."""
    proj = _sourced(tmp_path, path="../../../../etc/vlearn-0.3.9-py3-none-any.whl")
    with pytest.raises(env.BuildError) as e:
        env.check_supplied(proj, env.Manifest(kind="uv-project", path="pyproject.toml"), ["vlearn"])
    assert "outside" in str(e.value)


def test_the_depth_rule_is_read_off_the_declaration_not_off_the_head(tmp_path):
    """Stated against what the User wrote, so it means the same thing wherever the tree sits."""
    proj = _sourced(tmp_path, path="../wheels/vlearn-0.3.9-cp311-cp311-linux_x86_64.whl")
    m = env.Manifest(kind="uv-project", path="pyproject.toml")
    assert env.supplied(proj, m)[0].pod_rel == "../wheels/vlearn-0.3.9-cp311-cp311-linux_x86_64.whl"
    # The same declaration, read out of a tree at a completely different depth, says the same thing.
    ws = tmp_path / "elsewhere" / "3" / "transformerrl"
    ws.mkdir(parents=True)
    (ws / "pyproject.toml").write_text((proj / "pyproject.toml").read_text())
    assert env.supplied(ws, m)[0].pod_rel == env.supplied(proj, m)[0].pod_rel
    env.check_supplied(ws, m, ["vlearn"])


def test_an_absolute_path_source_is_refused(tmp_path):
    """`$proj` is the only root the Arm has. An absolute path names the Head's disk, or the donor's."""
    proj = _sourced(tmp_path, path="/opt/vlearn/vlearn-0.3.9-cp311-cp311-linux_x86_64.whl")
    with pytest.raises(env.BuildError) as e:
        env.check_supplied(proj, env.Manifest(kind="uv-project", path="pyproject.toml"), ["vlearn"])
    assert "outside" in str(e.value)


def test_a_path_source_that_could_be_shell_is_refused(tmp_path):
    """The declared path is rendered into the job script's `--bind` list, inside a double-quoted shell
    string. It is User text, so it is held to an alphabet — a `$` or a backtick there would be shell
    the submitter wrote and the Head ran, and a comma is apptainer's own bind separator.

    A quote is not in the list because it never gets this far: it makes the inline table unparseable,
    and `_uv_sources` leaves a broken `pyproject.toml` to uv, which says something far better about it
    than this could."""
    for i, bad in enumerate(("../$(id)/vlearn-0.3.9-py3-none-any.whl",
                             "../a,b/vlearn-0.3.9-py3-none-any.whl",
                             "../a`id`b/vlearn-0.3.9-py3-none-any.whl")):
        proj = _sourced(tmp_path / str(i), path=bad)
        with pytest.raises(env.BuildError) as e:
            env.check_supplied(proj, env.Manifest(kind="uv-project", path="pyproject.toml"),
                               ["vlearn"])
        assert "will not use as a path" in str(e.value)


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
        files, binds, log = env.substitute_supplied(
            proj, env.Manifest(kind="uv-project", path="pyproject.toml"))
        wheel = next(f for f in files if f.rel.endswith(".whl"))
        return proj, files, binds, log, wheel

    def test_the_package_it_installs_is_empty(self, tmp_path):
        *_, wheel = self._built(tmp_path)
        with zipfile.ZipFile(io.BytesIO(wheel.data)) as z:
            assert z.read("vlearn/__init__.py") == b""
            assert not any(n.endswith(".so") for n in z.namelist())

    def test_it_answers_to_the_name_and_version_the_user_declared(self, tmp_path):
        """Read out of the FILENAME, never out of the real wheel — the Head has never seen that file.
        It is on the submitter's own machine and is not uploaded, which is why this is synthesized
        rather than emptied."""
        *_, wheel = self._built(tmp_path)
        with zipfile.ZipFile(io.BytesIO(wheel.data)) as z:
            meta = z.read("vlearn-0.3.9.dist-info/METADATA").decode()
            assert "Name: vlearn" in meta and "Version: 0.3.9" in meta
            # The compatibility tag has to match too, or uv rejects the wheel as unusable here.
            assert "Tag: cp311-cp311-linux_x86_64" in z.read("vlearn-0.3.9.dist-info/WHEEL").decode()

    def test_nothing_is_written_into_the_project(self, tmp_path):
        """The whole reason this is bytes and binds rather than files: the project is the User's."""
        proj, *_ = self._built(tmp_path)
        assert [p.name for p in proj.iterdir()] == ["pyproject.toml"]

    def test_a_wheel_outside_the_project_is_bound_in_at_the_path_the_declaration_names(self, tmp_path):
        """PROJECT-RELATIVE, because only the Arm knows where the project landed on its own disk — and
        `$proj/../wheels` is the Spawn's own scratch on both branches. The old form recorded the HEAD's
        resolved path, which named a directory no Arm has."""
        _, _, binds, _, wheel = self._built(tmp_path)
        assert [(b.src, b.dest, b.over_project) for b in binds] == [("src0", "../wheels", False)]
        assert wheel.rel == "src0/vlearn-0.3.9-cp311-cp311-linux_x86_64.whl"

    def test_the_bind_is_rendered_against_the_arms_own_project(self, tmp_path):
        """`$proj` is the only name for it that is true on both branches — the Head's co-located Arm
        copies into node-local scratch, a remote one rsyncs into /tmp. `proj` is `$scratch/proj` in
        both, so `$proj/../wheels` is this Spawn's scratch and nothing else's."""
        _, files, binds, _, _ = self._built(tmp_path)
        block = _block("uv-project", "pyproject.toml", files=files, binds=binds)
        assert f"$scratch/{env.STUB_ROOT}/src0:$proj/../wheels" in block

    def test_the_arm_writes_it_out_before_the_build_reads_it(self, tmp_path):
        """The bytes travel in the job script itself — there is no transfer of a stub any more, because
        there is no file on the Head to transfer."""
        _, files, binds, _, wheel = self._built(tmp_path)
        block = _block("uv-project", "pyproject.toml", files=files, binds=binds)
        assert f'base64 -d > "$scratch/{env.STUB_ROOT}/{wheel.rel}"' in block
        assert base64.b64encode(wheel.data)[:32].decode() in block.replace("\n", "")
        assert block.index("base64 -d") < block.index("ccc_build_or_die /")

    def test_the_substitution_is_said_out_loud_where_the_user_will_see_it(self, tmp_path):
        """A User who declared a wheel and got someone else's build has to be able to find out why. It
        is decided on the Head and SAID on the Arm, in the Spawn's own `err`, beside the build it
        changed — there is no separate build log to go looking in."""
        _, _, _, log, _ = self._built(tmp_path)
        assert any("vlearn" in ln and "Arm Feature" in ln for ln in log)
        block = _block("uv-project", "pyproject.toml", log=log)
        assert all(shlex.quote(ln) in block for ln in log)
        assert block.index(shlex.quote(log[0])) < block.index("ccc_build_or_die /")


def test_a_wheel_inside_the_project_is_bound_over_not_overwritten(tmp_path):
    """The project carried a real wheel. It is covered by a FILE bind, and the copy on disk is left
    exactly as its owner wrote it."""
    proj = _sourced(tmp_path, path="wheels/vlearn-0.3.9-cp311-cp311-linux_x86_64.whl")
    real = proj / "wheels" / "vlearn-0.3.9-cp311-cp311-linux_x86_64.whl"
    real.parent.mkdir()
    real.write_bytes(b"the user's real, licensed wheel")
    files, binds, _ = env.substitute_supplied(
        proj, env.Manifest(kind="uv-project", path="pyproject.toml"))
    assert [(b.dest, b.over_project) for b in binds] == [
        ("wheels/vlearn-0.3.9-cp311-cp311-linux_x86_64.whl", True)]
    assert zipfile.is_zipfile(io.BytesIO(files[0].data))
    assert real.read_bytes() == b"the user's real, licensed wheel"


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
    before = (proj / "uv.lock").read_text()
    files, binds, _ = env.substitute_supplied(proj, env.Manifest(kind="uv-project",
                                                                 path="pyproject.toml"))
    stub = next(f for f in files if f.rel.endswith(".whl"))
    lock = next(f for f in files if f.rel == "uv.lock")
    text = lock.data.decode()
    assert f"sha256:{hashlib.sha256(stub.data).hexdigest()}" in text
    # Every OTHER package stays frozen at exactly what it was resolved to.
    assert "sha256:" + "a" * 64 in text
    assert "sha256:" + "b" * 64 not in text
    # Bound over the User's own lock for the build, never written to it. Theirs still records the wheel
    # they actually hold — a Head that rewrote it would leave them a hash matching nothing they have.
    assert (proj / "uv.lock").read_text() == before
    assert env.StubBind("uv.lock", "uv.lock", over_project=True) in binds


def test_a_lock_that_names_no_supplied_package_is_left_alone(tmp_path):
    """No bind, no copy, nothing shipped. The lock only enters this at all because uv verifies a
    path-sourced wheel's hash — a lock that records none has nothing to disagree with."""
    proj = _sourced(tmp_path)
    (proj / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "pyfiglet"\nversion = "1.0.4"\n')
    files, binds, _ = env.substitute_supplied(proj, env.Manifest(kind="uv-project",
                                                                 path="pyproject.toml"))
    assert not any(f.rel == "uv.lock" for f in files)
    assert not any(b.dest == "uv.lock" for b in binds)


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


def test_the_library_prefix_is_built_at_the_path_it_is_later_bound_at(with_tools):
    """A conda prefix bakes its own path into hundreds of files and does not survive being moved — the
    same invariant the Environment rests on, for the same reason. Built and bound in the same job on the
    same box, so the two cannot disagree."""
    from ccc_head_api import features as hf

    (argv,) = env.render_featlib_build({"vlearn": ["sdl2"]})
    assert "--prefix" in argv and f"{hf.FEATLIB_MOUNT}/vlearn" in argv
    block = _block(features=["vlearn"])
    assert f"$scratch/featlibs:{hf.FEATLIB_MOUNT}" in _pod_binds(block), "writable for the build"
    assert f'featbind=",$scratch/featlibs:{hf.FEATLIB_MOUNT}:ro"' in block, "read-only for the payload"


def test_the_feature_libraries_are_owed_even_to_a_spawn_that_declared_nothing(with_tools):
    """They are CCC's, not the submitter's. A **Tide Pool** may decline to declare a manifest and still
    asked for the Feature — and a Feature whose shared objects cannot dlopen their dependencies is a
    Feature that does not work. Previously this needed a manifest, because the libraries were built by
    the same Head-side call that built the env."""
    from ccc_head_api import features as hf
    block = _block(features=["vlearn"], declared=None)
    assert f"{hf.FEATLIB_MOUNT}/vlearn" in block
    assert 'py="' not in block, "but it still gets no interpreter — it declared none"


def test_the_library_prefix_installs_no_interpreter(with_tools):
    """It is bound for its lib/ directory alone and never run. A Python in here would be a second one
    on the box for no reason — and the reason it used to need one (a pip section) is gone."""
    from ccc_head_api import features as hf
    assert not any(s.startswith("python") for s in hf.lib_specs(["vlearn"])["vlearn"])


def _ok():
    class R:
        returncode, stdout, stderr = 0, "", ""
    return R()
