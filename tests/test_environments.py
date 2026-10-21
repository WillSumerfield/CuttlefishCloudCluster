"""Per-Spawn **Environment** declaration and build-command rendering (docs/elements/environments.md).

Uncalled since 2.0 (nothing declares an Environment until Dives), but still the one tested place that
says what a manifest means. Pure functions with remote consequences (CONTRIBUTING.md): nothing here
needs apptainer, a Guise, or a network, but each decides what would run on a donor's box. Worth
pinning: naming a manifest is mandatory and nothing is ever detected, an env inherits nothing and
carries its own interpreter, and every build targets the SAME absolute path it's later run at.
"""

import io

import pytest

from ccc_head_api import environments as env


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
    """What the job script names as `$py`. It has to resolve inside the Pod, against the bind —
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


def test_a_spawn_installing_its_dependencies_is_simply_running(monkeypatch):
    """squeue is the whole truth again. A Spawn used to be held while the Head built for it, reported
    as PENDING, and CCC had to keep a `build_state` to tell that apart from waiting for a card. The
    build happens inside the allocation now, so Slurm can see it and there is nothing left to override."""
    from ccc_head_api import slurm
    monkeypatch.setattr(slurm, "_slurm_json", lambda *a, **kw: {"jobs": [
        {"job_id": 4242, "partition": slurm.PARTITION, "job_state": ["RUNNING"], "name": "t",
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


def test_the_library_prefix_installs_no_interpreter(with_tools):
    """It is bound for its lib/ directory alone and never run. A Python in here would be a second one
    on the box for no reason — and the reason it used to need one (a pip section) is gone."""
    from ccc_head_api import features as hf
    assert not any(s.startswith("python") for s in hf.lib_specs(["vlearn"])["vlearn"])


def _ok():
    class R:
        returncode, stdout, stderr = 0, "", ""
    return R()
