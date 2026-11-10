"""Arm **Feature** runtimes (temp/FEATURES_BUILTIN.md) — the profile that turns `--feature vlearn`
from a placement constraint into a working Dive.

Two halves, split the way the code is: the Head's `Profile` (`ccc_head_api.features`) — the description
`cuttle feature show` prints, the canonical library specs, and which packages a submitter must not
install — and what an Arm must have on disk before it may declare the Feature at all (`cuttle.features`),
which also prepares that state and activates a licensed Feature Arm-local at `setup --arm`. The per-Dive
arrangement is a third thing, in-container and after the Environment build (`cuttle.feature_hook`), and
is tested in test_feature_hook.py.

The two packages never import each other — the channels between them are `/etc/ccc/features.env` and the
duplicated spec lists — so what is worth pinning here is that both ends name the same variables and
agree on the specs.
"""

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from ccc_head_api import features as hf
from cuttle import config
from cuttle import enroll as cuttle_enroll
from cuttle import features as af


# --- the submit-time gate ---------------------------------------------------

def test_only_a_profiled_feature_needs_an_environment():
    """`profiled()` is what the submit-time gate asks (dive.request). A profiled Feature attaches its
    software to the Dive's **Environment**, so one asked for by a Dive that declared none has nothing to
    attach to. An unprofiled Feature is placement plus `$<NAME>_PATH` — a bare Dive may still ask for it.

    Sorted, so a refusal names them the same way however the User typed the request."""
    assert hf.profiled(["vlearn"]) == ["vlearn"]
    assert hf.profiled(["isaaclab"]) == []
    assert hf.profiled(["isaaclab", "vlearn"]) == ["vlearn"]
    assert hf.profiled([]) == []


# --- what `cuttle feature show` describes ------------------------------------

def test_the_runtime_summary_names_every_system_library_ccc_builds():
    """The one canonical list. A library added for a new vlearn release has to reach both the prefix
    CCC builds and the answer a submitter gets when they ask what a Feature needs."""
    text = hf.runtime_summary("vlearn")
    # Every library libgym.so links, which resolve at LOAD time — a missing one is an ImportError.
    for lib in ("sdl2", "assimp=5", "zeromq", "czmq", "libglu", "mesalib"):
        assert lib in text, lib
    assert "cuda-cudart=13" in text, "libcudart.so.13 has no other source"
    assert "Python 3.11" in text, "vlearn ships binary wheels only"


def test_the_runtime_summary_is_not_a_file_to_copy_into_a_project():
    """It used to be a starter `environment.yml`, back when those libraries had to be in the
    submitter's own manifest. CCC builds them now, so copying this list into a project would install a
    second copy of it — the text must not read as something to paste."""
    text = hf.runtime_summary("vlearn")
    assert "dependencies:" not in text and "channels:" not in text
    assert "Do not declare them" in text


def test_the_runtime_summary_does_not_ship_vlearn_itself():
    """The whole point of the profile: the package comes from the Arm, on PYTHONPATH. A submitter who
    installed their own would be running an unactivated build against someone else's licence."""
    text = hf.runtime_summary("vlearn")
    assert ".whl" not in text
    assert "\n  vlearn" not in text.replace(f"\n{hf.VLEARN.name} —", "")


def test_the_feature_description_lists_both_majors():
    """`cuttle feature show vlearn` is the canonical answer to "what does CCC install for me". An
    alternate prefix that only appeared in the job script would be half a gigabyte nothing explains."""
    text = hf.runtime_summary("vlearn")
    assert "cuda-cudart=13" in text and "cuda-cudart=12" in text


def test_a_feature_with_no_profile_has_no_runtime_to_describe():
    with pytest.raises(KeyError):
        hf.runtime_summary("isaaclab")


# --- the Arm's side ---------------------------------------------------------

def _fake_install(root: Path, *, abis=("cp311",), missing=()) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for d in ("assets", "shaders", "cache"):
        if d not in missing:
            (root / d).mkdir()
    if "TurboActivate.dat" not in missing:
        (root / "TurboActivate.dat").write_text("dat")
    for abi in abis:
        whl = root / f"vlearn-0.3.9-{abi}-{abi}-linux_x86_64.whl"
        with zipfile.ZipFile(whl, "w") as z:
            z.writestr("vlearn/__init__.py", "")
            z.writestr(f"vlearn_bindings.{abi}-x86_64-linux-gnu.so", "")
            z.writestr("vlearn-0.3.9.dist-info/METADATA", "")
    return root


def test_a_good_install_passes_and_reports_its_abis(tmp_path):
    root = _fake_install(tmp_path / "vlearn", abis=("cp310", "cp311"))
    assert af.check_install("vlearn", str(root)) == []
    assert af.abi_tags("vlearn", str(root)) == ["cp310", "cp311"]


def test_a_missing_piece_is_named_at_the_prompt(tmp_path):
    """Checked when the owner types the path, not when a stranger's Dive lands. Left to run time this
    surfaces hours later, inside libgym.so, on a box nobody is watching."""
    root = _fake_install(tmp_path / "vlearn", missing=("assets", "TurboActivate.dat"))
    missing = af.check_install("vlearn", str(root))
    assert any(m.startswith("assets") for m in missing)
    assert any(m.startswith("TurboActivate.dat") for m in missing)
    assert not any(m.startswith("shaders") for m in missing)


def test_an_install_with_no_linux_wheel_is_rejected(tmp_path):
    """A Windows-only checkout is a real thing to have, and it cannot serve a Dive."""
    root = _fake_install(tmp_path / "vlearn", abis=())
    (root / "vlearn-0.3.9-cp311-cp311-win_amd64.whl").write_text("")
    assert any("linux" in m or "Linux" in m for m in af.check_install("vlearn", str(root)))


def test_ccc_has_no_opinion_about_a_feature_with_no_profile(tmp_path):
    """`isaaclab` is declared with a path and nothing else. CCC does not know what a valid one looks
    like, so it must not invent a requirement and refuse a true declaration."""
    assert af.check_install("isaaclab", str(tmp_path)) == []
    assert af.abi_tags("isaaclab", str(tmp_path)) == []


def test_unpacking_a_wheel_keeps_the_bindings_beside_the_package(tmp_path, monkeypatch):
    """The bug this exists to prevent: a wheel has three top-level entries and the extension module is
    a SIBLING of the package. Unpacking only `vlearn/` gives a directory that imports on nobody's
    machine."""
    root = _fake_install(tmp_path / "vlearn")
    dest = tmp_path / "pkgs"
    monkeypatch.setattr(af, "FEATURE_PKG_DIR", dest)
    # No sudo in a test: the profile's shell-outs are `rm -rf`, `install -d`, `cp -a`, `chmod`, all of
    # which work unprivileged against a tmp_path.
    monkeypatch.setattr(af, "run", lambda cmd, sudo=False, **kw: subprocess.run(cmd, **kw))
    landed = af.unpack_packages([{"name": "vlearn", "path": str(root)}])
    assert landed == {"vlearn": ["cp311"]}
    out = dest / "vlearn" / "cp311"
    assert (out / "vlearn" / "__init__.py").exists()
    assert list(out.glob("vlearn_bindings.*.so")), "the sibling extension module is what import needs"


def test_a_spawn_is_never_pointed_at_the_owners_own_directory(tmp_path, monkeypatch):
    """A container runs as a **Guise**, which has no relationship to the donor and cannot traverse a 0750
    home — and one un-traversable ancestor fails the whole container, not just that bind. So what a
    Dive is handed is CCC's mirror, wherever the owner keeps the real thing.

    An install under /home/<owner>/Projects can take the activation Dive down with "container
    creation failed: ... permission denied" — reads as a CCC bug, but is really a fact about
    where someone keeps their files."""
    root = _fake_install(tmp_path / "vlearn")
    (root / "assets" / "scene.bin").write_text("x")
    dest = tmp_path / "pkgs"
    monkeypatch.setattr(af, "FEATURE_PKG_DIR", dest)
    monkeypatch.setattr(af, "run", lambda cmd, sudo=False, **kw: subprocess.run(cmd, **kw))
    f = {"name": "vlearn", "path": str(root)}
    af.prepare([f])

    mirror = dest / "vlearn" / af.INSTALL_DIRNAME
    assert af.install_path(f) == str(mirror)
    # Every path the profile's bash opens, and no more — the vendor's README puts the licence key at
    # the checkout root, and a world-readable mirror of it is exactly what must not happen.
    assert {p.name for p in mirror.iterdir()} == {"assets", "shaders", "cache", "TurboActivate.dat"}
    assert (mirror / "assets" / "scene.bin").read_text() == "x"


def test_a_feature_with_no_profile_keeps_the_path_its_owner_declared():
    """`isaaclab` is placement only: CCC mirrors nothing for it, so there is nothing else to name."""
    assert af.install_path({"name": "isaaclab", "path": "/opt/isaaclab"}) == "/opt/isaaclab"


def test_the_descriptor_binds_every_abi_and_the_install_never_the_key(tmp_path, monkeypatch):
    """What ccc-launch reads: one read-only bind per held ABI (the hook picks one in-container) plus
    the mirrored runtime data — and nothing else under the Feature's tree, least of all the key."""
    monkeypatch.setattr(af, "FEATURE_PKG_DIR", tmp_path)
    binds = af.descriptor_binds("vlearn", ["cp310", "cp311"])
    srcs = {b["src"] for b in binds}
    assert str(tmp_path / "vlearn" / "cp310") in srcs and str(tmp_path / "vlearn" / "cp311") in srcs
    assert str(tmp_path / "vlearn" / af.INSTALL_DIRNAME) in srcs
    # Both system-library prefixes' lib/ (main + the conflicting CUDA major), by the same rule both ship.
    assert str(tmp_path / "vlearn" / "libs" / "lib") in srcs
    assert str(tmp_path / "vlearn" / "libs-cuda12" / "lib") in srcs
    assert all(b["ro"] and b["dst"].startswith(f"{af.FEATURE_MOUNT}/vlearn/") for b in binds)
    assert not any(af.LICENCE_KEY_NAME in b["src"] for b in binds)


def test_a_feature_with_no_profile_gets_no_descriptor(tmp_path, monkeypatch):
    monkeypatch.setattr(af, "FEATURE_PKG_DIR", tmp_path)
    assert af.descriptor_binds("isaaclab", ["cp311"]) == []
    af.write_descriptor("isaaclab", ["cp311"])   # a no-op, writes nothing
    assert not (tmp_path / "isaaclab" / af.DESCRIPTOR_NAME).exists()


def test_the_descriptor_records_the_licence_directory_only_when_there_is_one(tmp_path, monkeypatch):
    """ccc-launch binds the Licence directory into a Dive's home (where TurboActivate reads the
    activation); it learns the path from here. Recorded only for a licensed Feature with a directory —
    never for an Arm that named none."""
    import json
    monkeypatch.setattr(af, "FEATURE_PKG_DIR", tmp_path)
    monkeypatch.setattr(af, "run", lambda cmd, sudo=False, **kw: subprocess.run(cmd, **kw))
    af.write_descriptor("vlearn", ["cp311"], "/opt/ccc/licence")
    desc = json.loads((tmp_path / "vlearn" / af.DESCRIPTOR_NAME).read_text())
    assert desc["licence_home"] == "/opt/ccc/licence"
    af.write_descriptor("vlearn", ["cp311"], "")   # no Licence directory
    desc = json.loads((tmp_path / "vlearn" / af.DESCRIPTOR_NAME).read_text())
    assert "licence_home" not in desc


def test_prepare_writes_a_descriptor_ccc_launch_can_apply(tmp_path, monkeypatch):
    """The whole round-trip: setup writes profile.json, ccc-launch's validator accepts the binds it
    lists (the two never import each other, so this is where they are proved to agree)."""
    from cuttle import ccc_launch as L
    root = _fake_install(tmp_path / "vlearn")
    dest = tmp_path / "pkgs"
    monkeypatch.setattr(af, "FEATURE_PKG_DIR", dest)
    monkeypatch.setattr(af, "run", lambda cmd, sudo=False, **kw: subprocess.run(cmd, **kw))
    # The lib prefixes are a real conda-forge solve (build_libs is tested on its own); the descriptor
    # names their paths without needing them to exist, which is the round-trip under test here.
    monkeypatch.setattr(af, "build_libs", lambda feats: None)
    af.prepare([{"name": "vlearn", "path": str(root)}])
    desc = dest / "vlearn" / af.DESCRIPTOR_NAME
    assert desc.exists()
    monkeypatch.setattr(L, "FEATURE_STATE", dest)
    binds, _env = L.feature_binds_env(["vlearn"])
    assert any(dst == f"{af.FEATURE_MOUNT}/vlearn/cp311" for _src, dst, _ro in binds)


def test_build_libs_creates_a_prefix_for_each_cuda_major(tmp_path, monkeypatch):
    """Both majors ship — which one this Arm's wheel wants is a property of the build, so the loader is
    given both and takes whichever SONAME the binary names. Built once at setup, into the Feature's own
    state tree (not FEATLIB_MOUNT per Dive). The conda solve is not run here — the argv is."""
    monkeypatch.setattr(af, "FEATURE_PKG_DIR", tmp_path)
    seen = []
    monkeypatch.setattr(af, "run", lambda cmd, sudo=False, **kw: seen.append(list(cmd)))
    af.build_libs([{"name": "vlearn", "path": "/wherever"}])
    creates = [c for c in seen if "create" in c]
    prefixes = {c[c.index("--prefix") + 1] for c in creates}
    assert str(tmp_path / "vlearn" / "libs") in prefixes
    assert str(tmp_path / "vlearn" / "libs-cuda12") in prefixes
    main = next(c for c in creates if c[c.index("--prefix") + 1].endswith("/libs"))
    alt = next(c for c in creates if c[c.index("--prefix") + 1].endswith("/libs-cuda12"))
    assert "cuda-cudart=13" in main and "assimp=5" in main
    assert alt[-1] == "cuda-cudart=12" and "cuda-cudart=13" not in alt
    assert all(str(config.ENV_TOOLS_DIR / "micromamba") == c[0] for c in creates)
    # No interpreter in the prefix: it is bound for its lib/ alone and never run, and the reason it
    # once needed a Python (a pip section) is gone.
    assert not any(s.startswith("python") for c in creates for s in c)


def test_the_arm_mirrors_the_heads_library_specs():
    """The two packages never import each other, so nothing at runtime proves the duplicated spec lists
    agree — this test is that proof. A library added Head-side but not Arm-side is an ImportError in a
    Dive on this Arm; added Arm-side but not Head-side is a prefix `feature show` never explains."""
    assert list(af.VLEARN.libs) == [s for s, _why in hf.VLEARN.libs]
    assert {suffix: list(extra) for suffix, extra in af.VLEARN.alt_libs} == \
           {suffix: [s for s, _why in extra] for suffix, extra in hf.VLEARN.alt_libs}


def test_build_libs_does_nothing_for_a_feature_with_no_libraries(tmp_path, monkeypatch):
    """`isaaclab` has no ArmProfile and no system libraries — no prefix, no conda solve at setup."""
    monkeypatch.setattr(af, "FEATURE_PKG_DIR", tmp_path)
    seen = []
    monkeypatch.setattr(af, "run", lambda cmd, sudo=False, **kw: seen.append(list(cmd)))
    af.build_libs([{"name": "isaaclab", "path": "/opt/isaaclab"}])
    assert not any("create" in c for c in seen)


def test_the_mirrored_licence_key_is_not_world_readable(tmp_path, monkeypatch):
    """The one thing here that is a credential. `--contain` means the real gate is that no ordinary
    Dive ever BINDS it, but the mode is the protection that survives a mistake in the bind — and it
    is why the key is not simply mirrored into the install beside everything else."""
    root = _fake_install(tmp_path / "vlearn")
    key = root / "License.key"
    key.write_text("not-a-real-key")
    dest = tmp_path / "pkgs"
    monkeypatch.setattr(af, "FEATURE_PKG_DIR", dest)
    # `install -o root -g ccc-guises` needs privilege a test does not have, so the mode and ownership
    # are asserted off the argv rather than off the resulting file.
    seen = []
    monkeypatch.setattr(af, "run", lambda cmd, sudo=False, **kw: seen.append(list(cmd)))
    af.mirror_licence_key({"name": "vlearn", "path": str(root), "key": str(key)})
    argv = next(c for c in seen if c[0] == "install")
    assert argv == ["install", "-D", "-m", "0640", "-o", "root", "-g", config.GUISE_GROUP,
                    str(key), str(dest / "vlearn" / af.LICENCE_KEY_NAME)]


def test_the_shared_group_exists_before_the_licence_key_is_mirrored():
    """The mirrored key is `0640 root:ccc-guises`, and `setup` records this box's Features long before
    it provisions anyone's Unix identity — so on a first run the group is not there yet and `install`
    fails with "invalid group 'ccc-guises'", taking the whole setup with it.

    Asserted against the source because the fact under test is an ORDER within `setup_head`, and the
    steps around it install packages and start daemons."""
    import inspect

    from cuttle import setup as cuttle_setup
    src = inspect.getsource(cuttle_setup)
    for block in src.split("feature_profiles.prepare(")[:-1]:
        assert "ensure_guise_group()" in block.rsplit("with ui.step(", 1)[-1], \
            "prepare() mirrors a group-owned file — the group has to be ensured in the same step"


def test_a_re_setup_re_copies_from_the_owners_key_not_from_cccs_copy(tmp_path):
    """features.env is the Head's own Arm's only durable record, and it names both: the mirror a Dive
    binds, and the source to copy again next time. Offering the mirror back as its own source would
    pin whatever was copied once and never see a key the owner rotated."""
    text = cuttle_enroll.render_features_env(
        [{"name": "vlearn", "path": "/opt/vlearn", "key": "/home/donor/vlearn/License.key"}])
    assert f"export VLEARN_LICENCE_KEY={af.FEATURE_PKG_DIR / 'vlearn' / af.LICENCE_KEY_NAME}" in text
    assert "export VLEARN_LICENCE_KEY_SRC=/home/donor/vlearn/License.key" in text


def test_unpacking_replaces_rather_than_merges(tmp_path, monkeypatch):
    """A donor who upgraded the software in place would otherwise leave the previous release's files
    under the same ABI tag, mixed with the new one's, and every Dive would import the result."""
    root = _fake_install(tmp_path / "vlearn")
    dest = tmp_path / "pkgs"
    monkeypatch.setattr(af, "FEATURE_PKG_DIR", dest)
    monkeypatch.setattr(af, "run", lambda cmd, sudo=False, **kw: subprocess.run(cmd, **kw))
    af.unpack_packages([{"name": "vlearn", "path": str(root)}])
    stale = dest / "vlearn" / "cp311" / "vlearn" / "gone.py"
    stale.write_text("from the previous release")
    af.unpack_packages([{"name": "vlearn", "path": str(root)}])
    assert not stale.exists()


def test_un_declaring_a_feature_erases_its_mirror(tmp_path, monkeypatch):
    """Shrinking a declaration has to be as real as growing one. A re-run that drops a Feature
    leaves the registry and Slurm's `Feature=` line correct, so the only thing still claiming the
    box serves it is the mirrored gigabyte — and the copy of the Licence key beside it."""
    dest = tmp_path / "pkgs"
    (dest / "vlearn" / "install").mkdir(parents=True)
    (dest / "vlearn" / "licence.key").write_text("key")
    (dest / "isaaclab").mkdir()
    monkeypatch.setattr(af, "FEATURE_PKG_DIR", dest)
    monkeypatch.setattr(af, "run", lambda cmd, sudo=False, **kw: subprocess.run(cmd, **kw))

    af.forget_undeclared([{"name": "isaaclab"}])
    assert not (dest / "vlearn").exists()
    assert (dest / "isaaclab").exists()

    af.forget_undeclared([])
    assert list(dest.iterdir()) == []


# --- the one channel between the two sides ---------------------------------

def test_features_env_carries_everything_the_profile_reads(tmp_path):
    """/etc/ccc/features.env is the ONLY channel from an Arm's setup to a Dive placed on it — the Head
    renders one script for the whole fleet and cannot know where a donor put anything. So every
    variable a Feature's runtime reads has to be a variable this file writes."""
    text = cuttle_enroll.render_features_env(
        [{"name": "vlearn", "path": "/opt/vlearn"}], licence_home="/opt/ccc/licence")
    # CCC's mirror, not /opt/vlearn — a container runs as a Guise and cannot be assumed able to reach the
    # owner's directory at all (see test_a_spawn_is_never_pointed_at_the_owners_own_directory).
    assert f"export VLEARN_PATH={af.FEATURE_PKG_DIR / 'vlearn' / af.INSTALL_DIRNAME}" in text
    assert f"export VLEARN_PKG_ROOT={af.FEATURE_PKG_DIR / 'vlearn'}" in text
    assert f"export {config.LICENCE_HOME_VAR}=/opt/ccc/licence" in text


def test_a_feature_with_no_packages_adds_no_extra_lines():
    """`isaaclab` gets its `<NAME>_PATH` and nothing else — a variable pointing at a directory CCC
    never creates would be a promise the Arm cannot keep."""
    text = cuttle_enroll.render_features_env([{"name": "isaaclab", "path": "/opt/isaaclab"}])
    assert "export ISAACLAB_PATH=/opt/isaaclab" in text
    assert "PKG_ROOT" not in text


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_the_features_env_is_sourceable(tmp_path):
    """It is `.`-sourced by the job script with no `set -a`, so a path with a space in it would be a
    syntax error on the Arm rather than a quoting nuisance on the Head."""
    text = cuttle_enroll.render_features_env(
        [{"name": "vlearn", "path": "/opt/vlearn"}], licence_home="/opt/ccc/licence")
    f = tmp_path / "features.env"
    f.write_text(text)
    r = subprocess.run(["bash", "-c", f'. "{f}" && echo "$VLEARN_PKG_ROOT"'],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(af.FEATURE_PKG_DIR / "vlearn")


# --- activation (Arm-local, once per machine) ------------------------------

def test_a_licensed_feature_never_gets_past_setup_without_a_licence_directory(monkeypatch):
    """Enter on the old "does this box hold a licence?" (default no) left a vlearn Arm with none."""
    import typer
    from cuttle import setup, ui
    vlearn = [{"name": "vlearn", "path": "/opt/vlearn", "key": "/opt/vlearn/License.key"}]
    with pytest.raises(typer.Exit):
        setup._require_licence_home(vlearn, "")
    assert setup._require_licence_home([{"name": "isaaclab", "path": "/opt/i"}], "") == ""

    # Interactive: no yes/no, and an empty answer is re-asked rather than accepted.
    a = ui.Answers("", "/opt/ccc/licence")
    ui.install_answers(a)
    try:
        assert setup._prompt_licence_home(vlearn) == "/opt/ccc/licence"
    finally:
        ui.install_answers(None)
    assert not any("Does this box hold" in m for m in a.asked)


def _run_recorder(seen):
    def _run(cmd, sudo=False, check=True, **kw):
        seen.append(list(cmd))
        rc = 0
        if cmd[0] == "touch":
            Path(cmd[1]).write_text("")                 # the marker becomes real, as the live touch would
        elif cmd[:2] == ["test", "-e"]:
            rc = 0 if Path(cmd[2]).exists() else 1       # marker check -- live code runs this via sudo
        class R:
            returncode = rc
            stdout = ""
            stderr = ""
        return R()
    return _run


def test_held_abis_lists_only_the_unpacked_wheel_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(af, "FEATURE_PKG_DIR", tmp_path)
    for d in ("cp310", "cp311", af.INSTALL_DIRNAME, "libs", "libs-cuda12"):
        (tmp_path / "vlearn" / d).mkdir(parents=True)
    assert af.held_abis("vlearn") == ["cp310", "cp311"]
    assert af.held_abis("never-unpacked") == []


def test_activation_argv_binds_the_feature_read_only_and_lands_in_the_licence_home(tmp_path, monkeypatch):
    """The activation container gets the Feature's own state read-only and writes ONLY into the Licence
    directory — bound at /root because TurboActivate keys its store off getpwuid, not $HOME."""
    monkeypatch.setattr(af, "FEATURE_PKG_DIR", tmp_path)
    argv = af.activation_argv("base:img", "vlearn", "cp311", "/opt/ccc/licence", "/tmp/act.py")
    root = tmp_path / "vlearn"
    assert argv[:4] == ["podman", "run", "--rm", "--authfile"]   # networked: TurboActivate + uv need egress
    assert "none" not in argv                                    # NOT --network none, unlike bench_locally
    assert f"{root / 'cp311'}:{af.FEATURE_MOUNT}/vlearn/cp311:ro" in argv
    assert f"{root / af.INSTALL_DIRNAME}:{af.FEATURE_MOUNT}/vlearn/{af.INSTALL_DIRNAME}:ro" in argv
    assert f"{root / 'libs' / 'lib'}:{af.FEATURE_MOUNT}/vlearn/libs/lib:ro" in argv
    assert f"{root / 'libs-cuda12' / 'lib'}:{af.FEATURE_MOUNT}/vlearn/libs-cuda12/lib:ro" in argv
    assert f"{root / af.LICENCE_KEY_NAME}:/ccc/key/License.key:ro" in argv
    assert "/tmp/act.py:/ccc/activate.py:ro" in argv
    assert "/opt/ccc/licence:/root" in argv          # the ONE writable mount
    assert "uv venv --python 3.11 /ccc/venv" in " ".join(argv)   # interpreter matches the wheel ABI


def test_activation_argv_never_binds_the_licence_key_writable(tmp_path, monkeypatch):
    monkeypatch.setattr(af, "FEATURE_PKG_DIR", tmp_path)
    argv = af.activation_argv("base", "vlearn", "cp310", "/lh", "/a.py")
    key = next(v for v in argv if "/ccc/key/" in v)
    assert key.endswith(":ro")
    assert "uv venv --python 3.10 /ccc/venv" in " ".join(argv)   # cp310 -> 3.10


def test_activate_runs_once_then_marks_and_hands_the_files_to_the_licence_account(tmp_path, monkeypatch):
    monkeypatch.setattr(af, "FEATURE_PKG_DIR", tmp_path)
    (tmp_path / "vlearn" / "cp311").mkdir(parents=True)
    lh = tmp_path / "licence"
    lh.mkdir()
    seen = []
    monkeypatch.setattr(af, "run", _run_recorder(seen))
    feats = [{"name": "vlearn", "path": "/x", "key": "/k"}]
    af.activate(feats, "base:img", str(lh))
    assert any(c[0] == "podman" for c in seen)
    assert (lh / f".{af.ACTIVATED_MARKER}-vlearn").exists()
    assert any(c[0] == "chown" and config.LICENCE_USER in c[2] for c in seen)
    # The marker is probed through sudo (a plain stat would EACCES on the 2770 Licence dir).
    assert any(c[:2] == ["test", "-e"] for c in seen)
    # A re-setup must not spend another TurboActivate seat.
    seen.clear()
    af.activate(feats, "base:img", str(lh))
    assert not any(c[0] == "podman" for c in seen)


def test_activate_skips_without_a_licence_home_a_key_or_a_script(tmp_path, monkeypatch):
    monkeypatch.setattr(af, "FEATURE_PKG_DIR", tmp_path)
    (tmp_path / "vlearn" / "cp311").mkdir(parents=True)
    (tmp_path / "isaaclab" / "cp311").mkdir(parents=True)
    lh = tmp_path / "licence"
    lh.mkdir()
    seen = []
    monkeypatch.setattr(af, "run", _run_recorder(seen))
    af.activate([{"name": "vlearn", "path": "/x", "key": "/k"}], "base", "")           # no Licence dir
    af.activate([{"name": "vlearn", "path": "/x"}], "base", str(lh))                   # no key
    af.activate([{"name": "isaaclab", "path": "/x", "key": "/k"}], "base", str(lh))    # no activation script
    assert not any(c[0] == "podman" for c in seen)
