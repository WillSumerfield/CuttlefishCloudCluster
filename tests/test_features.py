"""Arm **Feature** runtimes (temp/FEATURES_BUILTIN.md) — the profile that turns `--feature vlearn`
from a placement constraint into a working Spawn.

Two halves, split the way the code is: what the Head renders into a job script
(`ccc_head_api.features`), and what an Arm has to have on disk before it may declare the Feature at all
(`cuttle.features`). They never talk to each other directly — the whole channel between them is
`/etc/ccc/features.env` — so what is worth pinning here is that both ends name the same variables.

The rendered bash is checked as bash (`bash -n`), not just as a string containing the right substrings.
This snippet is spliced into a script that only ever runs on someone else's donated box, hours after
anyone could notice a syntax error, and a broken one takes the Spawn with it.
"""

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from ccc_head_api import environments as env
from ccc_head_api import features as hf
from ccc_head_api import spawns
from cuttle import config
from cuttle import enroll as cuttle_enroll
from cuttle import features as af


def _script(**kw):
    kw.setdefault("minutes", 60)
    kw.setdefault("constraint", "")
    kw.setdefault("node", None)
    kw.setdefault("outputs", ["outputs"])
    return spawns._render(staging=Path("/tmp/u"), gpu=1, script="train.py", args=[], files=[],
                          token="a3f9", secret="s3cr3t", **kw)


# --- what a Feature without a profile costs --------------------------------

def test_a_feature_with_no_profile_renders_nothing():
    """`isaaclab` is a real, catalogued Feature that is nothing but placement plus $ISAACLAB_PATH. A
    Spawn asking for it must get the SAME script as a Spawn asking for nothing — a profile is optional,
    and an empty one is not allowed to leave a stray probe or an empty accumulator behind."""
    assert hf.setup_snippet(["isaaclab"]) == ""
    assert _script(features=["isaaclab"]) == _script(features=[])


def test_an_ordinary_spawn_never_mentions_features():
    """The overwhelming majority of Spawns. Nothing here is conditional at run time — the Head decides
    at render, so an ordinary Spawn's script does not contain the probe, the failure helper, or any
    Feature's name."""
    body = _script(features=[])
    assert "feature_probe" not in body
    assert "feature_fail" not in body
    assert "VLEARN_PATH" not in body
    # The accumulators are still declared, because the payload names them unconditionally.
    assert 'featbind=""; featenv=""' in body


# --- the rendered profile ---------------------------------------------------

def test_the_rendered_script_is_valid_bash():
    """Both shapes, since the snippet lands in the same place either way."""
    for feats in ([], ["vlearn"], ["vlearn", "isaaclab"]):
        body = _script(features=feats)
        r = subprocess.run(["bash", "-n"], input=body, text=True, capture_output=True)
        assert r.returncode == 0, f"{feats}: {r.stderr}"


def test_the_payload_gets_the_features_binds_and_env():
    """The two accumulators are what a profile actually contributes, and they are only worth anything
    if the payload appends them. Both payload shapes: a Tide Pool on a vlearn Arm is how someone
    debugs a vlearn Spawn, and it is useless if the runtime is not the same one."""
    script = _script(features=["vlearn"])
    assert '--bind "$proj,$ccc_out,$scratch/tmp:/ccc-tmp$envbind$licbind$featbind"' in script
    # `$featenv` stays LAST in the --env list, whatever else is added before it — it accumulates
    # `,NAME=value` fragments, so anything appended after it would land inside the last value.
    assert _env_line(script).endswith('$featenv" \\')
    pool = _script(features=["vlearn"], tidepool_port=2222, tidepool_pubkey="ssh-ed25519 AAAA")
    assert "$envbind$licbind$featbind" in pool
    assert _env_line(pool).endswith('$featenv" \\')
    assert "CCC_PROJ=$proj" in _env_line(pool)


def _env_line(script: str) -> str:
    """The payload's own `--env` line — not one of the comments above it that mention the flag."""
    return next(ln for ln in script.splitlines() if ln.strip().startswith('--env "$wenv'))


def test_the_arm_package_wins_over_anything_the_submitter_installed():
    """The point of the whole profile. vlearn is licensed per machine and its activation state is tied
    to a release, so the copy that runs has to be the Arm's — PYTHONPATH, ahead of the Environment's
    own site-packages."""
    body = _script(features=["vlearn"])
    assert f"PYTHONPATH={hf.FEATURE_MOUNT}/vlearn" in body
    assert f"$vl_pkg:{hf.FEATURE_MOUNT}/vlearn:ro" in body


def test_the_whole_unpacked_wheel_goes_on_the_path_not_the_package_dir():
    """A wheel unpacks to three top-level entries, and `vlearn_bindings.<abi>.so` sits BESIDE the
    package rather than inside it — binding `vlearn/` alone would leave `import vlearn` failing on a
    box that has everything it needs."""
    body = _script(features=["vlearn"])
    assert f"PYTHONPATH={hf.FEATURE_MOUNT}/vlearn," in body + ","
    assert f"PYTHONPATH={hf.FEATURE_MOUNT}/vlearn/vlearn" not in body


def test_the_working_directory_entries_are_symlinks(tmp_path):
    """Load-bearing, not tidy. $proj is what recover_scratch measures against RECOVERY_MAX_BYTES and
    then rsyncs home; vlearn's assets/ alone is ~1 GB, so a real directory here would silently disable
    the Recovery Window for every vlearn Spawn — the same bug moving the Environment out of $proj
    already fixed once. `du` does not follow symlinks and `rsync -a` copies them as links.

    Run for real rather than asserted as a string: what matters is that the result is a link."""
    body = _script(features=["vlearn"])
    assert 'ln -s "$VLEARN_PATH/assets" "$proj/assets"' in body
    assert 'ln -s "$VLEARN_PATH/shaders" "$proj/shaders"' in body
    assert 'ln -s "$VLEARN_PATH/TurboActivate.dat" "$proj/TurboActivate.dat"' in body

    install = tmp_path / "vlearn"
    (install / "assets").mkdir(parents=True)
    (install / "assets" / "big.bin").write_bytes(b"x" * 4096)
    (install / "shaders").mkdir()
    (install / "TurboActivate.dat").write_text("dat")
    (install / "cache").mkdir()
    (install / "cache" / "warm.bin").write_bytes(b"c")
    proj = tmp_path / "proj"
    proj.mkdir()
    # The three ln lines and the cp, lifted out of the profile so this exercises the real commands.
    lines = [ln for ln in hf.VLEARN_SETUP.splitlines()
             if ln.startswith(("ln -s ", "cp -r ", "rm -rf "))]
    subprocess.run(["bash", "-e", "-c", "\n".join(lines)], check=True,
                   env={"VLEARN_PATH": str(install), "proj": str(proj), "PATH": "/usr/bin:/bin"})
    assert (proj / "assets").is_symlink()
    assert (proj / "TurboActivate.dat").is_symlink()
    # ...and cache is the one real directory, because it is the one vlearn writes.
    assert (proj / "cache").is_dir() and not (proj / "cache").is_symlink()
    assert (proj / "cache" / "warm.bin").exists()
    du = subprocess.run(["du", "-sb", str(proj)], capture_output=True, text=True).stdout.split()[0]
    assert int(du) < 4096, "the symlinked assets must not count against the Recovery Window"


def test_the_install_wins_over_an_upload_that_carried_its_own(tmp_path):
    """One behaviour, not a merge. A submitter who uploaded their own assets/ gets the Arm's — they are
    a gigabyte of vendor data nobody uploads on purpose, and a partial merge is a trap that surfaces as
    a missing scene deep inside a run."""
    install = tmp_path / "vlearn"
    for d in ("assets", "shaders", "cache"):
        (install / d).mkdir(parents=True)
    (install / "assets" / "theirs.bin").write_text("arm")
    (install / "TurboActivate.dat").write_text("dat")
    proj = tmp_path / "proj"
    (proj / "assets").mkdir(parents=True)
    (proj / "assets" / "mine.bin").write_text("upload")
    lines = [ln for ln in hf.VLEARN_SETUP.splitlines()
             if ln.startswith(("ln -s ", "cp -r ", "rm -rf "))]
    subprocess.run(["bash", "-e", "-c", "\n".join(lines)], check=True,
                   env={"VLEARN_PATH": str(install), "proj": str(proj), "PATH": "/usr/bin:/bin"})
    assert (proj / "assets").is_symlink()
    assert (proj / "assets" / "theirs.bin").exists()
    assert not (proj / "assets" / "mine.bin").exists()


def test_the_licence_key_is_never_bound_into_an_ordinary_spawn():
    """The vendor's README puts License.key at the checkout root. Only the individual subpaths a Spawn
    needs are bound, never $VLEARN_PATH itself — so the key stays outside a Pod that has no business
    with it, and the licence's activation Spawn remains the one thing that sees it."""
    body = _script(features=["vlearn"])
    assert '$VLEARN_PATH:' not in body
    assert "License.key" not in body
    for sub in ("assets", "shaders", "TurboActivate.dat"):
        assert f"$VLEARN_PATH/{sub}:$VLEARN_PATH/{sub}:ro" in body


def test_the_environments_lib_dir_is_only_used_when_there_is_an_environment():
    """LD_LIBRARY_PATH is built from `$envmount`, which the Environment branch sets and the base branch
    leaves empty — so a Spawn with no Environment does not get a trailing `/lib` pointing at a mount
    that was never made."""
    body = _script(features=["vlearn"],
                   declared=env.Manifest(kind="uv-requirements", path="requirements.txt"))
    assert 'envbind=""; envmount=""' in body
    assert f'envmount="{env.ENV_MOUNT}"' in body
    assert '[ -n "$envbind" ] && vl_ld="$vl_ld:$envmount/lib"' in body


def test_a_feature_failure_ends_the_spawn_the_way_every_other_failure_does():
    """A Feature that cannot be set up is not a warning. It writes to the Spawn's own `err`, records an
    exit code, stops the periodic pusher and stages out — the same four things every other early exit
    in the job script does, because a Spawn that dies without them reads from the Head as "lost"."""
    body = _script(features=["vlearn"])
    fail = body.split("feature_fail() {", 1)[1].split("}", 1)[0]
    assert '>> "$err"' in fail
    assert 'echo 1 > "$exitf"' in fail
    assert 'kill "$loop_pid"' in fail
    assert "stage_out" in fail
    assert "exit 1" in fail


def test_the_abi_is_asked_of_the_interpreter_rather_than_assumed():
    """vlearn ships binary wheels only, so the Environment's Python has to match one the Arm holds. Both
    halves of that comparison exist only on the Arm — so the check runs there, at job start, and names
    both sides when it fails."""
    body = _script(features=["vlearn"])
    assert 'feature_abi=$(printf %s "$feature_probe" | sed -n 1p)' in body
    assert 'vl_pkg="$VLEARN_PKG_ROOT/$feature_abi"' in body
    assert '[ -d "$vl_pkg" ]' in body
    # The message names what the Spawn asked for AND what the Arm actually has.
    assert "$feature_abi" in body.split('[ -d "$vl_pkg" ]', 1)[1].split("\n#", 1)[0]


def test_the_snippet_order_does_not_depend_on_how_the_user_typed_it():
    """Same request, same script — otherwise two identical Spawns render differently and nothing that
    compares job scripts (or a human reading two of them) can tell them apart."""
    assert hf.setup_snippet(["vlearn", "isaaclab"]) == hf.setup_snippet(["isaaclab", "vlearn"])
    assert hf.setup_snippet(["vlearn", "vlearn"]) == hf.setup_snippet(["vlearn"])


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
    """Checked when the owner types the path, not when a stranger's Spawn lands. Left to run time this
    surfaces hours later, inside libgym.so, on a box nobody is watching."""
    root = _fake_install(tmp_path / "vlearn", missing=("assets", "TurboActivate.dat"))
    missing = af.check_install("vlearn", str(root))
    assert any(m.startswith("assets") for m in missing)
    assert any(m.startswith("TurboActivate.dat") for m in missing)
    assert not any(m.startswith("shaders") for m in missing)


def test_an_install_with_no_linux_wheel_is_rejected(tmp_path):
    """A Windows-only checkout is a real thing to have, and it cannot serve a Spawn."""
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
    """A Pod runs as a **Guise**, which has no relationship to the donor and cannot traverse a 0750
    home — and one un-traversable ancestor fails the whole container, not just that bind. So what a
    Spawn is handed is CCC's mirror, wherever the owner keeps the real thing.

    An install under /home/<owner>/Projects can take the activation Spawn down with "container
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


def test_the_mirrored_licence_key_is_not_world_readable(tmp_path, monkeypatch):
    """The one thing here that is a credential. `--contain` means the real gate is that no ordinary
    Spawn ever BINDS it, but the mode is the protection that survives a mistake in the bind — and it
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
    """features.env is the Head's own Arm's only durable record, and it names both: the mirror a Spawn
    binds, and the source to copy again next time. Offering the mirror back as its own source would
    pin whatever was copied once and never see a key the owner rotated."""
    text = cuttle_enroll.render_features_env(
        [{"name": "vlearn", "path": "/opt/vlearn", "key": "/home/donor/vlearn/License.key"}])
    assert f"export VLEARN_LICENCE_KEY={af.FEATURE_PKG_DIR / 'vlearn' / af.LICENCE_KEY_NAME}" in text
    assert "export VLEARN_LICENCE_KEY_SRC=/home/donor/vlearn/License.key" in text


def test_unpacking_replaces_rather_than_merges(tmp_path, monkeypatch):
    """A donor who upgraded the software in place would otherwise leave the previous release's files
    under the same ABI tag, mixed with the new one's, and every Spawn would import the result."""
    root = _fake_install(tmp_path / "vlearn")
    dest = tmp_path / "pkgs"
    monkeypatch.setattr(af, "FEATURE_PKG_DIR", dest)
    monkeypatch.setattr(af, "run", lambda cmd, sudo=False, **kw: subprocess.run(cmd, **kw))
    af.unpack_packages([{"name": "vlearn", "path": str(root)}])
    stale = dest / "vlearn" / "cp311" / "vlearn" / "gone.py"
    stale.write_text("from the previous release")
    af.unpack_packages([{"name": "vlearn", "path": str(root)}])
    assert not stale.exists()


# --- the one channel between the two sides ---------------------------------

def test_features_env_carries_everything_the_profile_reads(tmp_path):
    """/etc/ccc/features.env is the ONLY channel from an Arm's setup to a Spawn placed on it — the Head
    renders one script for the whole fleet and cannot know where a donor put anything. So every
    variable the rendered profile reads has to be a variable this file writes."""
    text = cuttle_enroll.render_features_env(
        [{"name": "vlearn", "path": "/opt/vlearn"}], licence_home="/opt/ccc/licence")
    # CCC's mirror, not /opt/vlearn — a Pod runs as a Guise and cannot be assumed able to reach the
    # owner's directory at all (see test_a_spawn_is_never_pointed_at_the_owners_own_directory).
    assert f"export VLEARN_PATH={af.FEATURE_PKG_DIR / 'vlearn' / af.INSTALL_DIRNAME}" in text
    assert f"export VLEARN_PKG_ROOT={af.FEATURE_PKG_DIR / 'vlearn'}" in text
    assert f"export {config.LICENCE_HOME_VAR}=/opt/ccc/licence" in text

    body = _script(features=["vlearn"])
    exported = {ln.split("=", 1)[0].removeprefix("export ") for ln in text.splitlines()
                if ln.startswith("export ")}
    for var in ("VLEARN_PATH", "VLEARN_PKG_ROOT"):
        assert f"${var}" in body or f"${{{var}" in body
        assert var in exported


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


# --- activation -------------------------------------------------------------

def test_the_licence_key_is_bound_only_for_an_activation_spawn():
    """The one asymmetry in the whole profile. An ordinary Spawn runs against activation STATE that
    already exists in the Arm's Licence directory; the key that produced it is the credential, and it
    reaches exactly one Pod."""
    ordinary = _script(features=["vlearn"])
    assert "VLEARN_LICENCE_KEY" not in ordinary
    assert "VL_LICENSE_KEY_PATH" not in ordinary

    activating = _script(features=["vlearn"], activation=True)
    assert "$VLEARN_LICENCE_KEY:$VLEARN_LICENCE_KEY:ro" in activating
    assert "VL_LICENSE_KEY_PATH=$VLEARN_LICENCE_KEY" in activating
    # Everything the ordinary Spawn got, it still gets — activation is an addition, not a variant.
    base = (hf.VLEARN_SETUP.replace("%%FEATURE_MOUNT%%", hf.FEATURE_MOUNT)
                           .replace("%%FEATLIB_MOUNT%%", hf.FEATLIB_MOUNT).strip())
    assert base in activating


def test_an_activation_spawn_is_still_valid_bash():
    r = subprocess.run(["bash", "-n"], input=_script(features=["vlearn"], activation=True),
                       text=True, capture_output=True)
    assert r.returncode == 0, r.stderr


def test_the_activation_flag_cannot_arrive_over_http():
    """`--activation` binds an Arm owner's licence key into a Pod. It exists on the Head-local admin
    path, where authorization is standing on the Head, and nowhere a browser or a `cuttle run` can
    reach — a User able to set it could read every donor's key."""
    import inspect

    from ccc_head_api import main as head_main
    src = inspect.getsource(head_main)
    assert "activation" not in src.split("def submit", 1)[-1][:4000]


def test_the_activation_project_declares_only_the_interpreter_vlearn_needs():
    """CCC writes this manifest, so it says the one thing it actually knows: the ABI a binary wheel was
    built for. The system libraries are no longer in here — they are the prefix CCC builds for any
    Spawn asking for the Feature, and this Spawn asks for it like any other."""
    project = hf.activation_project("vlearn")
    assert set(project) == {"pyproject.toml", "activate.py"}
    assert 'requires-python = "==3.11.*"' in project["pyproject.toml"]
    assert "create_gym(with_render=False)" in project["activate.py"]
    # No vlearn in the Environment: the package comes from the Arm's own unpacked wheel, which is the
    # build its licence covers. Installing a second one from an index would activate the wrong thing.
    assert 'dependencies = []' in project["pyproject.toml"]


def test_the_activation_manifest_is_the_one_the_activation_spawn_submits():
    """A manifest CCC generates but does not name would leave the Spawn declaring nothing, which is
    refused outright now that declaring is mandatory."""
    import inspect

    from cuttle import enroll
    src = inspect.getsource(enroll._activate_one)
    named = src.split("manifest=", 1)[1].split(",")[0].strip().strip('"')
    assert named in hf.activation_project("vlearn"), named


def test_a_feature_with_no_activation_is_admitted_without_one():
    assert hf.needs_activation(["isaaclab"]) == []
    assert hf.needs_activation(["vlearn", "isaaclab"]) == ["vlearn"]
    with pytest.raises(KeyError):
        hf.activation_project("isaaclab")


def test_the_activation_project_tars_to_exactly_two_files():
    """CCC's own Spawn has no directory to upload — the project is generated in memory."""
    import io
    import tarfile

    from cuttle import spawns as spawn_client
    tar = spawn_client.tar_from_files(hf.activation_project("vlearn"))
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        assert sorted(tf.getnames()) == ["activate.py", "pyproject.toml"]
        body = tf.extractfile("activate.py").read().decode()
    assert "import vlearn" in body


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


# --- the placeholder is removed on the Arm ----------------------------------
# The Head replaced the submitter's declared vlearn with an empty stub before building the Environment
# (environments.substitute_supplied), because the copy that RUNS has to be the one this Arm's owner
# activated. That leaves an empty `vlearn/` in site-packages, and this is where it goes away again.


def test_the_placeholder_vlearn_is_deleted_from_the_environment():
    """sys.path would already resolve `import vlearn` to the PYTHONPATH entry, which precedes
    site-packages. That ordering is not a thing to rest a LICENSING invariant on — it is subtle, it is
    Python's to change, and a `.pth` in site-packages can reorder it. So the stub is deleted outright
    and there is only one vlearn on the box to find."""
    body = _script(features=["vlearn"])
    assert "$vl_site/vlearn" in body
    for pattern in ("vlearn-*.dist-info", "vlearn_bindings*.so", "*vlearn*.pth"):
        assert pattern in body, pattern


def test_the_deletion_happens_on_the_arms_filesystem_not_inside_a_pod():
    """The Environment is bound READ-ONLY into every Pod, so this could not be done from inside one.
    The probe reports a Pod-side path; the prefix has to be rewritten to the Arm's own."""
    body = _script(features=["vlearn"])
    assert 'vl_site="$scratch/env${feature_purelib#$envmount}"' in body


def test_the_deletion_is_skipped_when_there_is_no_environment_to_delete_from():
    """A **Tide Pool** may decline to declare one. `rm -rf` on a path built from two empty variables
    would be `rm -rf /vlearn`, which is nothing on this box — but only by luck."""
    body = _script(features=["vlearn"])
    guard = next(ln for ln in body.splitlines() if ln.startswith('if [ -n "$envmount" ]'))
    assert '[ -n "$feature_purelib" ]' in guard


def test_the_interpreter_is_asked_where_its_site_packages_is():
    """One probe, three answers, no assumptions: a conda prefix and a uv venv put site-packages at
    different depths, and only the interpreter knows which this is."""
    body = _script(features=["vlearn"])
    assert 'print(sysconfig.get_paths()["purelib"])' in body
    assert "feature_purelib=$(printf %s \"$feature_probe\" | sed -n 3p)" in body


# --- the Feature's system libraries reach the Arm ---------------------------

def test_the_feature_library_prefix_is_mounted_for_the_payload():
    """Seeded into `$featbind`, which is already on both payloads' --bind — so a Tide Pool on a vlearn
    Arm gets exactly the runtime a Script Spawn would, which is the whole point of debugging in one."""
    body = _script(features=["vlearn"])
    assert f'featbind=",$scratch/featlibs:{hf.FEATLIB_MOUNT}:ro"' in body
    assert f"{hf.FEATLIB_MOUNT}/vlearn/lib" in body, "and on LD_LIBRARY_PATH"


def test_the_library_prefix_is_built_outside_the_project():
    """$proj is what recover_scratch() measures against RECOVERY_MAX_BYTES and rsyncs home. Half a
    gigabyte of CCC's own libraries built in there would silently disable recovery for every Spawn that
    asked for a Feature. Built straight into $scratch, so there is nothing to move."""
    body = _script(features=["vlearn"])
    assert 'mkdir -p "$scratch/env" "$scratch/tmp" "$scratch/featlibs"' in body
    assert f"$proj/{env.STUB_DIRNAME}" not in body, "no Feature library ever lands in the project"


def test_a_spawn_that_asked_for_no_feature_never_mounts_one():
    """Nothing is rendered at all now — no mkdir, no build, no bind. The Head knows at submit which
    Features were asked for, so the absent case is absent rather than an inert run-time branch."""
    body = _script(features=[])
    assert "$scratch/featlibs" not in body
    assert f"{hf.FEATLIB_MOUNT}/vlearn/lib" not in body
