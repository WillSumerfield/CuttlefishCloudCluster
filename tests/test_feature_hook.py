"""The in-container Feature hook (cuttle.feature_hook): the pre-build stub + relock, and the post-build
per-Feature arrangement. All of it runs as the User inside a Dive; here it runs against tmp trees.

NOT tested: a live vlearn import, a GPU, or TurboActivate — those need the real licensed wheel and the
hardware (temp/phase-8-plan.md). These pin the plumbing: the right files move, the env is assembled, and
an ABI that the Arm does not hold fails loudly rather than at `import`.
"""
import base64
import hashlib
import json

import pytest

from cuttle import feature_hook as fh


# --- pre-build: the stub + relock -----------------------------------------------

def test_relock_moves_only_the_named_blocks_hash():
    text = ('[[package]]\nname = "a"\nhash = "sha256:' + "1" * 64 + '"\n\n'
            '[[package]]\nname = "b"\nhash = "sha256:' + "2" * 64 + '"\n')
    out = fh._relock(text, "b", "3" * 64)
    assert "sha256:" + "1" * 64 in out       # every other package stays frozen
    assert "sha256:" + "3" * 64 in out
    assert "sha256:" + "2" * 64 not in out


def test_stub_writes_each_wheel_in_place_and_relocks(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "vlearn"\nversion = "0.3.9"\n'
        'source = { path = "../wheels/vlearn-0.3.9-cp311-cp311-linux_x86_64.whl" }\n'
        'wheels = [{ filename = "vlearn-0.3.9-cp311-cp311-linux_x86_64.whl", '
        'hash = "sha256:' + "b" * 64 + '" }]\n')
    bytes_ = b"stub-wheel-bytes"
    digest = hashlib.sha256(bytes_).hexdigest()
    stubs = tmp_path / "stubs.json"
    stubs.write_text(json.dumps({
        "files": [{"rel": "../wheels/vlearn-0.3.9-cp311-cp311-linux_x86_64.whl",
                   "b64": base64.b64encode(bytes_).decode()}],
        "digests": {"vlearn": digest}, "lock": "uv.lock"}))
    monkeypatch.setattr(fh, "STUBS_FILE", stubs)

    fh.stub(proj)

    # `../wheels` is relative to the project root — the Dive's own scratch beside it.
    assert (tmp_path / "wheels" / "vlearn-0.3.9-cp311-cp311-linux_x86_64.whl").read_bytes() == bytes_
    lock = (proj / "uv.lock").read_text()
    assert "sha256:" + digest in lock and "sha256:" + "b" * 64 not in lock


def test_stub_does_nothing_without_a_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(fh, "STUBS_FILE", tmp_path / "absent.json")
    fh.stub(tmp_path)   # an ordinary Dive: no payload file, nothing to do, no error
    assert list(tmp_path.iterdir()) == []


# --- post-build: per-Feature arrangement ----------------------------------------

def _feature_tree(tmp_path, abis=("cp311",)):
    """A stand-in for what descriptor_binds mounts read-only under FEATURE_MOUNT for vlearn."""
    vl = tmp_path / "feature" / "vlearn"
    for a in abis:
        (vl / a / "vlearn" / "lib").mkdir(parents=True)   # the unpacked wheel's own lib/
    for name in ("assets", "shaders"):
        (vl / "install" / name).mkdir(parents=True)
    (vl / "install" / "TurboActivate.dat").write_text("x")
    (vl / "install" / "cache").mkdir()
    (vl / "install" / "cache" / "warm").write_text("c")
    return tmp_path / "feature"


def _venv(tmp_path):
    sp = tmp_path / "venv" / "lib" / "python3.11" / "site-packages"
    (sp / "vlearn").mkdir(parents=True)                       # the empty stub uv installed
    (sp / "vlearn-0.3.9.dist-info").mkdir()
    (sp / "vlearn_bindings.cpython-311-x86_64-linux-gnu.so").write_text("")
    (sp / "nvidia" / "cublas" / "lib").mkdir(parents=True)    # a torch wheel's bundled libs
    return tmp_path / "venv", sp


def test_setup_vlearn_matches_the_abi_cleans_the_stub_lays_out_vlwd_and_sets_env(tmp_path, monkeypatch):
    feat = _feature_tree(tmp_path)
    vlwd = tmp_path / "vlwd"
    vlwd.mkdir()
    monkeypatch.setattr(fh, "FEATURE_MOUNT", str(feat))
    monkeypatch.setattr(fh, "VLWD_MOUNT", str(vlwd))
    venv, sp = _venv(tmp_path)
    env_file = tmp_path / "featurenv"

    fh.setup_vlearn(venv, env_file, "cp311")

    # The stub is GONE from the venv — only the Arm's build on PYTHONPATH may answer `import vlearn`.
    assert not (sp / "vlearn").exists()
    assert not list(sp.glob("vlearn_bindings*.so")) and not list(sp.glob("vlearn-*.dist-info"))
    # The working dir: three symlinks into the read-only install, cache/ copied (vlearn writes it).
    assert (vlwd / "assets").is_symlink() and (vlwd / "TurboActivate.dat").is_symlink()
    assert (vlwd / "cache" / "warm").read_text() == "c"
    # The runtime env, with the User's own values left reachable behind CCC's.
    body = env_file.read_text()
    assert f'PYTHONPATH="{feat}/vlearn/cp311${{PYTHONPATH:+:$PYTHONPATH}}"' in body
    assert f'VL_WORKING_DIRECTORY="{vlwd}' in body
    assert f"{feat}/vlearn/cp311/vlearn/lib" in body          # the wheel's own lib, first
    assert f"{feat}/vlearn/libs/lib" in body and f"{feat}/vlearn/libs-cuda12/lib" in body
    assert f"{sp}/nvidia/cublas/lib" in body                  # the venv's nvidia wheels, last


def test_setup_vlearn_fails_loudly_when_the_arm_holds_no_wheel_for_this_abi(tmp_path, monkeypatch):
    feat = _feature_tree(tmp_path, abis=("cp310",))            # the Arm has 3.10, the Dive is 3.11
    monkeypatch.setattr(fh, "FEATURE_MOUNT", str(feat))
    monkeypatch.setattr(fh, "VLWD_MOUNT", str(tmp_path / "vlwd"))
    venv, _ = _venv(tmp_path)
    with pytest.raises(SystemExit):
        fh.setup_vlearn(venv, tmp_path / "e", "cp311")


def test_the_mounts_mirror_the_head():
    """The two Feature packages never import each other, so the mount constants are duplicated and must
    agree (same split as FEATURE_NAMES)."""
    from cuttle import features as cf
    from ccc_head_api import features as hf
    assert cf.FEATURE_MOUNT == hf.FEATURE_MOUNT
    assert cf.VLWD_MOUNT == hf.VLWD_MOUNT
