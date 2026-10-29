"""Dives (phase 4): the root helper's Dive verbs — folder checks, rsync filters (real rsync), modules,
locks, the snapshot id shift — plus the templates and constants they share.

NOT tested: Podman commit/push, a real registry, nsenter, systemd device policy, a real Arm — hardware list.
"""

import gzip
import hashlib
import io
import json
import subprocess
import tarfile

import pytest

from cuttle import ccc_tidepool as H
from cuttle import config, templates

TOK = "ab" * 16


@pytest.fixture
def helper_dirs(monkeypatch, tmp_path):
    for name in ("DIVE_RECORDS", "DIVE_RSYNCD", "DIVE_STATE", "MNT_ROOT", "RECORDS", "KEYS_ROOT"):
        d = tmp_path / name.lower()
        d.mkdir()
        monkeypatch.setattr(H, name, d)
    (H.RECORDS / "bob.json").write_text(json.dumps({"uid": 60003}))
    (H.MNT_ROOT / "bob" / "home").mkdir(parents=True)
    return tmp_path


class TestConstantsAgree:
    def test_helper_matches_config(self):
        assert (H.DIVE_RECORDS, H.DIVE_RSYNCD, H.DIVE_STATE) == (
            config.DIVE_RECORDS, config.DIVE_RSYNCD, config.DIVE_STATE)
        assert H.registry() == config.registry() == "ccc.lab:5000"
        assert H.CA_CERT == str(config.HEAD_CA_CERT)
        assert H.HELPER == str(config.TIDEPOOL_HELPER_BIN)


class TestFolderPaths:
    @pytest.mark.parametrize("rel", ["/etc", "../x", "a/../b", "a/./b", "", "a\nb", "-rf", "a;b"])
    def test_bad_paths_refused(self, rel):
        with pytest.raises(H.Refused):
            H.check_rel(rel)

    @pytest.mark.parametrize("rel", ["proj", "proj/a-b_c.1", "My Data/x", "proj/"])
    def test_good_paths(self, rel):
        assert H.check_rel(rel) == rel.rstrip("/")

    def test_a_symlink_component_is_refused(self, helper_dirs):
        home = H.MNT_ROOT / "bob" / "home"
        (home / "real").mkdir()
        (home / "link").symlink_to("/etc")
        assert H.host_dir("bob", "real") == home / "real"
        with pytest.raises(H.Refused, match="symlink"):
            H.host_dir("bob", "link")
        with pytest.raises(H.Refused, match="not a folder"):
            H.host_dir("bob", "missing")

    def test_folders_cannot_repeat_or_nest(self):
        assert H.check_folders("proj", ["data", "lib"]) == ("proj", ["data", "lib"])
        with pytest.raises(H.Refused, match="twice"):
            H.check_folders("proj", ["proj"])
        with pytest.raises(H.Refused, match="inside"):
            H.check_folders("proj", ["proj/data"])
        with pytest.raises(H.Refused, match="inside"):
            H.check_folders("proj/a", ["proj"])
        H.check_folders("proj", ["project"])      # a shared prefix is not nesting


def _rsync(src, dst, *args):
    subprocess.run(["rsync", "-a", *args, f"{src}/", f"{dst}/"], check=True, capture_output=True)
    return sorted(str(p.relative_to(dst)) for p in dst.rglob("*") if p.is_file())


class TestRsyncRules:
    """The real rsync, with the helper's own rules."""

    def test_dotfile_allowlist_never_takes_keys_or_credentials(self, tmp_path):
        home, out = tmp_path / "home", tmp_path / "out"
        files = [".bashrc", ".zshrc", ".gitconfig", ".config/git/config", ".config/nvim/init.lua",
                 ".oh-my-zsh/x/y.zsh", ".ssh/config", ".ssh/known_hosts", ".ssh/id_ed25519.pub",
                 ".ssh/id_ed25519", ".ssh/authorized_keys", ".netrc", ".git-credentials",
                 ".config/gh/hosts.yml", ".aws/credentials", "proj/main.py", ".cache/big"]
        for f in files:
            (home / f).parent.mkdir(parents=True, exist_ok=True)
            (home / f).write_text("x")
        out.mkdir()
        got = _rsync(home, out, "-m", *(f"--filter={r}" for r in H.DOTFILE_RULES))
        assert got == sorted([".bashrc", ".zshrc", ".gitconfig", ".config/git/config",
                              ".config/nvim/init.lua", ".oh-my-zsh/x/y.zsh", ".ssh/config",
                              ".ssh/known_hosts", ".ssh/id_ed25519.pub"])

    def test_denylist_and_cuttleignore(self, tmp_path):
        src, out = tmp_path / "proj", tmp_path / "out"
        for f in ["train.py", ".git/HEAD", ".venv/bin/python", "pkg/__pycache__/a.pyc", "ckpt/big.pt",
                  "data/keep.csv", "node_modules/x.js"]:
            (src / f).parent.mkdir(parents=True, exist_ok=True)
            (src / f).write_text("x")
        (src / ".cuttleignore").write_text("ckpt/\n")
        out.mkdir()
        got = _rsync(src, out, *H.FILTER_ARGS)
        assert got == [".cuttleignore", ".git/HEAD", "data/keep.csv", "train.py"]

    def test_the_mirror_never_deletes_what_was_never_staged(self, tmp_path):
        """Head-side .venv and ignored checkpoints survive the Arm's --delete mirror."""
        head, arm = tmp_path / "head", tmp_path / "arm"
        for f in ["train.py", "old.py", ".venv/bin/python", "ckpt/big.pt"]:
            (head / f).parent.mkdir(parents=True, exist_ok=True)
            (head / f).write_text("x")
        (head / ".cuttleignore").write_text("ckpt/\n")
        arm.mkdir()
        _rsync(head, arm, *H.FILTER_ARGS)
        (arm / "old.py").unlink()
        (arm / "new.py").write_text("y")
        got = _rsync(arm, head, "--delete", *H.FILTER_ARGS)
        assert got == [".cuttleignore", ".venv/bin/python", "ckpt/big.pt", "new.py", "train.py"]

    def test_staged_bytes_parses_rsyncs_total(self, monkeypatch):
        out = "Number of files: 3\nTotal file size: 1,234,567 bytes\nTotal transferred file size: 0\n"
        monkeypatch.setattr(H, "sh", lambda *a, **k: subprocess.CompletedProcess(a, 0, out, ""))
        assert H.staged_bytes(60003, H.MNT_ROOT) == 1234567


class TestModules:
    def test_main_writable_deps_read_only_as_the_users_uid(self, helper_dirs):
        home = H.MNT_ROOT / "bob" / "home"
        for d in ("proj", "data"):
            (home / d).mkdir()
        d = {"user": "bob", "uid": 60003, "main": "proj", "deps": ["data"]}
        conf = H.render_modules(TOK, d)
        u = H.id_base(60003) + 1000
        mods = {}
        cur = None
        for line in conf.splitlines():
            if line.startswith("["):
                cur = mods.setdefault(line.strip("[]"), {})
            elif cur is not None and "=" in line:
                k, v = line.split("=", 1)
                cur[k.strip()] = v.strip()
        assert set(mods) == {f"d{TOK}-main", f"d{TOK}-dep0", f"d{TOK}-dot", f"d{TOK}-sys"}
        main, dep, sysm = mods[f"d{TOK}-main"], mods[f"d{TOK}-dep0"], mods[f"d{TOK}-sys"]
        dot = mods[f"d{TOK}-dot"]
        assert main["read only"] == "false" and dep["read only"] == sysm["read only"] == "true"
        assert main["uid"] == dep["uid"] == dot["uid"] == str(u)
        # a User's symlinks travel as they are; only the root-run module keeps rsync's munging
        assert main["munge symlinks"] == dep["munge symlinks"] == dot["munge symlinks"] == "no"
        assert sysm["uid"] == "root" and "munge symlinks" not in sysm
        assert main["path"] == str(home / "proj") and dep["path"] == str(home / "data")
        assert main["pre-xfer exec"] == f"/usr/local/sbin/ccc-tidepool dive-lock {TOK}"
        assert "pre-xfer exec" not in dep
        assert sysm["path"] == str(H.DIVE_STATE / TOK / "sys")
        assert dot["path"] == str(H.DIVE_STATE / TOK / "home")
        assert {m["auth users"] for m in mods.values()} == {f"d{TOK}"}

    def test_rsyncd_includes_the_root_owned_dir(self):
        assert f"&include {config.DIVE_RSYNCD}" in templates.render_rsyncd_conf("100.99.0.1")


class TestOpenAndClose:
    def test_open_then_close_leaves_nothing(self, helper_dirs, monkeypatch):
        monkeypatch.setattr(H, "sh", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
        monkeypatch.setattr(H.os, "chown", lambda *a: None)
        (H.MNT_ROOT / "bob" / "home" / "proj").mkdir()
        (H.MNT_ROOT / "bob" / "ssh").mkdir()
        for f in ("ssh_host_ed25519_key", "ssh_host_ed25519_key.pub"):
            (H.MNT_ROOT / "bob" / "ssh" / f).write_text("k")
        (H.DIVE_STATE / TOK / "home").mkdir(parents=True)
        H.dive_open("bob", TOK, {"main": "proj", "deps": [], "toml": None, "secret": "s3",
                                 "image": "img@sha256:" + "c" * 64, "session": "t" * 43})
        assert (H.DIVE_RSYNCD / f"{TOK}.secrets").read_text() == f"d{TOK}:s3\n"
        assert (H.DIVE_STATE / TOK / "sys" / "ssh_host_ed25519_key").exists()
        sess = H.DIVE_STATE / TOK / "sys" / "session"
        assert sess.read_text() == "t" * 43 + "\n" and sess.stat().st_mode & 0o777 == 0o400
        assert H.dive_record(TOK)["main"] == "proj"
        deleted = []
        monkeypatch.setattr(H, "delete_manifest", deleted.append)
        H.dive_close(TOK)
        assert deleted == ["img@sha256:" + "c" * 64]
        assert not list(H.DIVE_RSYNCD.iterdir()) and not list(H.DIVE_RECORDS.iterdir())
        assert not (H.DIVE_STATE / TOK).exists()

    def test_open_refuses_a_bad_session(self, helper_dirs, monkeypatch):
        monkeypatch.setattr(H.os, "chown", lambda *a: None)
        monkeypatch.setattr(H, "record", lambda u: {"uid": 60003})
        (H.MNT_ROOT / "bob" / "home" / "proj").mkdir(parents=True, exist_ok=True)
        (H.MNT_ROOT / "bob" / "ssh").mkdir(parents=True, exist_ok=True)
        for f in ("ssh_host_ed25519_key", "ssh_host_ed25519_key.pub"):
            (H.MNT_ROOT / "bob" / "ssh" / f).write_text("k")
        (H.DIVE_STATE / TOK / "home").mkdir(parents=True)
        with pytest.raises(H.Refused, match="session"):
            H.dive_open("bob", TOK, {"main": "proj", "secret": "s", "image": "i", "session": "x\ny"})

    def test_open_without_prep_is_refused(self, helper_dirs):
        (H.MNT_ROOT / "bob" / "home" / "proj").mkdir()
        with pytest.raises(H.Refused, match="never prepared"):
            H.dive_open("bob", TOK, {"main": "proj", "secret": "s", "image": "i"})

    def test_lock_happens_once(self, helper_dirs, monkeypatch):
        H.save_dive(TOK, {"token": TOK, "user": "bob", "uid": 60003, "main": "proj", "deps": [],
                          "locked": False, "head": None, "image": "i"})
        applied = []
        monkeypatch.setattr(H, "apply_lock", applied.append)
        H.dive_lock(TOK)
        H.dive_lock(TOK)   # every mirror runs pre-xfer exec; a User's umount inside is left alone
        assert len(applied) == 1 and H.dive_record(TOK)["locked"]

    @pytest.mark.parametrize("argv", [["dive-close", "x"], ["dive-lock", "../a"],
                                      ["dive-prep", "Bob", TOK], ["dive-gpu", "grant", TOK]])
    def test_bad_input_exits_2(self, argv, helper_dirs):
        assert H.main(argv) == 2




class TestRegistry:
    def test_push_registry_allows_delete_pull_does_not(self):
        assert "enabled: true" in templates.render_registry_config("127.0.0.1", readonly=False) \
            .split("delete:")[1].split("\n")[1]
        assert "enabled: false" in templates.render_registry_config("100.99.0.1", readonly=True) \
            .split("delete:")[1].split("\n")[1]

    def test_gc_is_a_nightly_helper_run(self):
        assert "ExecStart=/usr/local/sbin/ccc-tidepool registry-gc" in templates.render_registry_gc_service()
        assert "OnCalendar=" in templates.REGISTRY_GC_TIMER_TMPL


BASE = H.id_base(60003)


def _layer(entries) -> bytes:
    """entries: (name, type, uid, data, xattrs)"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
        for name, typ, uid, data, xattrs in entries:
            ti = tarfile.TarInfo(name)
            ti.type, ti.uid, ti.gid = typ, uid, uid
            ti.pax_headers = {f"SCHILY.xattr.{k}": v for k, v in xattrs.items()}
            if typ == tarfile.LNKTYPE:
                ti.linkname = "etc/f"
            ti.size = len(data) if typ == tarfile.REGTYPE else 0
            t.addfile(ti, io.BytesIO(data) if typ == tarfile.REGTYPE else None)
    return buf.getvalue()


LAYER = [("etc/ccc", tarfile.DIRTYPE, BASE, b"", {}),
         ("etc/f", tarfile.REGTYPE, BASE + 1000, b"hello", {"user.k": "v"}),
         ("etc/h", tarfile.LNKTYPE, BASE, b"", {}),
         ("etc/.wh.gone", tarfile.REGTYPE, BASE, b"", {}),
         ("etc/odd", tarfile.REGTYPE, 5, b"x", {})]


class TestShiftSnapshot:
    """`podman commit` of a Tidepool records host ids (id_base+n): in a Dive (mapped 0→id_base) those
    are unmapped, so /etc/ccc was unwritable and a Head Dive's crun mkdir failed."""

    def test_layer_ids_shift_to_container_ids(self):
        out = io.BytesIO()
        diff_id = H.shift_layer(io.BytesIO(_layer(LAYER)), out, BASE)
        raw = gzip.decompress(out.getvalue())
        assert diff_id == "sha256:" + hashlib.sha256(raw).hexdigest()
        with tarfile.open(fileobj=io.BytesIO(raw)) as t:
            got = {m.name: m for m in t.getmembers()}
            assert {n: (m.uid, m.gid) for n, m in got.items()} == {
                "etc/ccc": (0, 0), "etc/f": (1000, 1000), "etc/h": (0, 0), "etc/.wh.gone": (0, 0),
                "etc/odd": (5, 5)}
            assert t.extractfile("etc/f").read() == b"hello"
            assert got["etc/f"].pax_headers["SCHILY.xattr.user.k"] == "v"
            assert got["etc/h"].islnk() and got["etc/h"].linkname == "etc/f"

    def test_only_the_top_layer_is_replaced_and_old_manifest_dropped(self, helper_dirs, monkeypatch):
        monkeypatch.setattr(H, "registry", lambda: "ccc.c:5000")
        base_layer = {"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": "sha256:base",
                      "size": 9}
        top = gzip.compress(_layer(LAYER))
        m = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
             "config": {"digest": "sha256:cfg", "size": 1},
             "layers": [base_layer, {**base_layer, "digest": "sha256:top", "size": len(top)}]}
        blobs = {"sha256:cfg": json.dumps({"rootfs": {"diff_ids": ["sha256:b0", "sha256:t0"]}}).encode(),
                 "sha256:top": top}
        put, calls = {}, []

        class R(io.BytesIO):
            def __init__(self, body, headers):
                super().__init__(body)
                self.headers = headers

        def reg(method, path, data=None, headers=None):
            calls.append((method, path))
            if method == "GET" and "/manifests/" in path:
                return R(json.dumps(m).encode(), {"Content-Type": m["mediaType"]})
            if method == "GET":
                return R(blobs[path.rsplit("/", 1)[1]], {})
            if method == "POST":
                return R(b"", {"Location": "/v2/up/1?state=x"})
            body = data if isinstance(data, bytes) else data.read()
            put[path] = body
            return R(b"", {})

        monkeypatch.setattr(H, "_reg", reg)
        deleted = []
        monkeypatch.setattr(H, "delete_manifest", deleted.append)
        ref = H.shift_snapshot("ccc/u-bob", TOK, "sha256:old", BASE)

        newm = put[f"ccc/u-bob/manifests/{TOK}"]
        assert ref == "ccc.c:5000/ccc/u-bob@sha256:" + hashlib.sha256(newm).hexdigest()
        newm = json.loads(newm)
        assert newm["layers"][0] == base_layer
        newtop = put[f"/v2/up/1?state=x&digest={newm['layers'][1]['digest']}"]
        assert newm["layers"][1]["size"] == len(newtop)
        cfg = json.loads(put[f"/v2/up/1?state=x&digest={newm['config']['digest']}"])
        assert cfg["rootfs"]["diff_ids"] == [
            "sha256:b0", "sha256:" + hashlib.sha256(gzip.decompress(newtop)).hexdigest()]
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(newtop))) as t:
            assert t.getmember("etc/ccc").uid == 0
        assert deleted == ["ccc.c:5000/ccc/u-bob@sha256:old"]


# --- ccc-launch, Dive mode ---------------------------------------------------------

from cuttle import ccc_launch as L  # noqa: E402

DIVE = {"token": TOK, "user": "bob", "secret": "s" * 43, "head": "100.99.0.1", "main": "proj/a",
        "deps": ["data"], "port": 31005}


class TestLaunchSpec:
    def test_constants_agree_with_the_helper(self):
        assert L.DIVE_FILTERS == H.FILTER_ARGS
        assert L.REL_RE.pattern == H.REL_RE.pattern and L.TOKEN_RE.pattern == H.TOKEN_RE.pattern
        assert L.TIDEPOOL_HELPER == H.HELPER
        assert L.ENV_CACHE == config.NODE_ENV_CACHE_DIR and L.MESH_IFACE == config.CCC_TS_TUN

    def test_a_good_dive_spec_passes(self):
        L.validate_dive(dict(DIVE))

    @pytest.mark.parametrize("bad", [{"main": "../etc"}, {"main": "/etc"}, {"deps": ["a/../../b"]},
                                     {"token": "x"}, {"user": "Root"}, {"head": "evil.com"},
                                     {"port": 22}, {"secret": "short"}, {"extra": 1}])
    def test_bad_dive_specs_refused(self, bad):
        with pytest.raises(L.Refused):
            L.validate_dive({**DIVE, **bad})


class TestArmDive:
    def test_stage_in_pulls_main_last_and_owns_new_dirs(self, tmp_path, monkeypatch):
        calls, chowned = [], []
        monkeypatch.setattr(L.subprocess, "run", lambda a, **k: calls.append(a))
        monkeypatch.setattr(L.os, "chown", lambda p, u, g: chowned.append((str(p), u)))
        (tmp_path / "sys").mkdir(mode=0o700)      # as rsync leaves it: the Head's root 0700
        home = L.stage_in(DIVE, tmp_path, 4242)
        assert (str(tmp_path / "sys"), 4242 - L.INNER_UID) in chowned     # container root's
        assert (tmp_path / "sys").stat().st_mode & 0o777 == 0o711
        chowned.remove((str(tmp_path / "sys"), 4242 - L.INNER_UID))
        srcs = [c[-2] for c in calls]
        assert srcs == [f"rsync://d{TOK}@100.99.0.1/d{TOK}-sys/",
                        f"rsync://d{TOK}@100.99.0.1/d{TOK}-dot/",
                        f"rsync://d{TOK}@100.99.0.1/d{TOK}-dep0/",
                        f"rsync://d{TOK}@100.99.0.1/d{TOK}-main/"]   # main last: it locks
        assert all("--numeric-ids" in c for c in calls)
        assert calls[-1][-1] == f"{home}/proj/a/"
        assert {u for _, u in chowned} == {4242}
        assert {p for p, _ in chowned} >= {str(home), str(home / "proj"), str(home / "proj/a")}

    def test_mirror_is_main_only_with_delete_and_the_filters(self, monkeypatch, tmp_path):
        seen = []
        monkeypatch.setattr(L.subprocess, "run",
                            lambda a, **k: seen.append(a) or subprocess.CompletedProcess(a, 0))
        assert L.mirror(DIVE, tmp_path)
        a = seen[0]
        assert a[-2:] == [f"{tmp_path}/proj/a/", f"rsync://d{TOK}@100.99.0.1/d{TOK}-main/"]
        assert "--delete" in a and "--filter=:- .cuttleignore" in a and "--exclude=.venv" in a

    def test_container_argv(self, tmp_path):
        home = tmp_path / "home"
        for p in ("proj/a", "data", ".config/git", ".config/gh-not-staged-anyway", ".ssh"):
            (home / p).mkdir(parents=True)
        for f in (".zshrc", ".ssh/config"):
            (home / f).write_text("x")
        spec = {"image": "ccc.lab:5000/ccc/u-bob@sha256:" + "a" * 64, "argv": ["/bin/sh", "-c", "x"],
                "env": {"CCC_USER": "bob"}, "network": True, "dive": DIVE}
        a = L.dive_argv(spec, "77", 2**31, "/s/job_77/step_batch", ["GPU-1"], ["GPU-1"], tmp_path, home,
                        ["data"], "100.99.0.7")
        j = " ".join(a)
        vols = [a[i + 1] for i, x in enumerate(a) if x == "-v"]
        assert f"{home}:/home/bob" in vols and f"{home}/data:/home/bob/data:ro" in vols
        assert f"{home}/.zshrc:/home/bob/.zshrc:ro" in vols
        assert f"{home}/.config/git:/home/bob/.config/git:ro" in vols
        assert f"{home}/.ssh/config:/home/bob/.ssh/config:ro" in vols
        assert not any(v.startswith(f"{home}/proj") for v in vols)
        assert f"{tmp_path}/sys:/etc/ccc/dive:ro" in vols
        assert "--log-driver=journald" in a    # why a Dive died outlives the job's private /tmp
        assert f"{L.ENV_CACHE}:/ccc-cache:idmap" in vols
        assert {f"{config.CONTAINER_CUTTLE}:{config.CONTAINER_CUTTLE}:ro",
                f"{config.CLUSTER_NAME_FILE}:/etc/ccc/cluster-name:ro",
                f"{config.HEAD_CA_TRUST_PATH}:{config.CONTAINER_CA}:ro"} <= set(vols)
        assert a[a.index("--add-host") + 1] == f"ccc.lab:{DIVE['head']}"
        assert "-p 100.99.0.7:31005:22" in j and "--device nvidia.com/gpu=GPU-1" in j
        assert "--cgroup-parent /s/job_77/step_batch" in j
        assert "--privileged" not in j and "--network host" not in j
        assert a[-4:] == [spec["image"], "/bin/sh", "-c", "x"]

    def test_launch_leaves_the_jobs_tmp_first(self, monkeypatch):
        """sbatch --chdir=/tmp is the job's private tmpfs, removed at scancel: the final mirror's rsync
        died on getcwd() there (Dive 9 on dex lost its last write)."""
        went = []
        monkeypatch.setattr(L.os, "chdir", went.append)
        with pytest.raises(L.Refused):
            L.main(["ccc-launch"])
        assert went == ["/"]

    def test_the_snapshot_image_goes_after_the_final_mirror(self, tmp_path, monkeypatch):
        order = []
        spec = {"image": "ccc.lab:5000/ccc/u-bob@sha256:" + "a" * 64, "dive": DIVE}
        monkeypatch.setattr(L, "JOBS_DIR", tmp_path / "jobs")
        monkeypatch.setattr(L, "ENV_CACHE", tmp_path / "cache")
        monkeypatch.setattr(L, "stage_in", lambda d, j, u: tmp_path)
        monkeypatch.setattr(L, "gpu_ids", lambda g: ["GPU-1"])
        monkeypatch.setattr(L, "ensure_cdi", lambda names: None)
        monkeypatch.setattr(L, "mesh_ip", lambda: "100.99.0.7")
        monkeypatch.setattr(L, "dive_argv", lambda *a: ["true"])
        monkeypatch.setattr(L, "gpu_names", lambda u: ["RTX"])
        monkeypatch.setattr(L, "write_shell_info", lambda *a: None)
        monkeypatch.setattr(L, "on_head", lambda: False)
        monkeypatch.setattr(L, "mirror", lambda d, h: order.append("mirror") or True)
        monkeypatch.setattr(L.subprocess, "run", lambda a, **k: order.append(a))
        monkeypatch.setattr(L.signal, "signal", lambda *a: None)
        job = {"gres_idx": [0], "node": "arm1", "end": "2026-10-29T14:30:00"}
        assert L.run_dive(spec, "77", job, "/s", 2**31) == 0
        assert order[-2:] == ["mirror", ["/usr/bin/nsenter", "--mount=/proc/1/ns/mnt", "--",
                                         "/usr/bin/podman", "rmi", spec["image"]]]

    def test_a_dot_main_folder_is_never_bound_read_only(self, tmp_path):
        home = tmp_path / "home"
        (home / ".work").mkdir(parents=True)
        (home / ".bashrc").write_text("x")
        assert [d for _, d in L.dotfile_binds(home, "bob", [".work"])] == ["/home/bob/.bashrc"]


class TestOneDivePath:
    """A Dive on the Head is the same container as on an Arm; only its port and its name differ."""

    def test_the_head_publishes_on_every_address(self, tmp_path):
        spec = {"image": "x", "dive": DIVE}
        (tmp_path / "home").mkdir()
        a = L.dive_argv(spec, "77", 2**31, "/s", [], [], tmp_path, tmp_path / "home", [], "")
        assert a[a.index("-p") + 1] == "31005:22"

    def test_where_is_the_cluster_name_on_the_head_else_the_node(self, tmp_path, monkeypatch):
        (tmp_path / "name").write_text("glados\n")
        monkeypatch.setattr(L, "CLUSTER_NAME_FILE", tmp_path / "name")
        monkeypatch.setattr(L, "on_head", lambda: True)
        assert L.dive_where("dex") == "glados"
        monkeypatch.setattr(L, "on_head", lambda: False)
        assert L.dive_where("willpc") == "willpc"

    def test_banner(self):
        from datetime import datetime
        now = datetime(2026, 10, 29, 12, 0)
        b = L.dive_banner("4", "glados", [0], ["RTX PRO 6000"], "2026-10-29T14:30:00", "Fractals", now)
        assert b == ("Dive 4 on glados · GPU 0 (RTX PRO 6000) · ends 14:30 · "
                     "~/Fractals mirrors back to your Tidepool every minute")
        assert "ends Oct 30 09:00" in L.dive_banner("4", "x", [], [], "2026-10-30T09:00:00", "m", now)

    def test_shell_info_files_are_readable_inside(self, tmp_path):
        L.write_shell_info(tmp_path, "willpc", "proj/a", "Dive 4")
        assert [(tmp_path / n).read_text() for n in ("where", "main", "banner")] == \
            ["willpc\n", "proj/a\n", "Dive 4\n"]
        assert all((tmp_path / n).stat().st_mode & 0o777 == 0o644 for n in ("where", "main", "banner"))

    def test_the_epilog_has_no_gpu_lend_backstop(self):
        assert "dive-gpu" not in templates.render_epilog()

    def test_the_shell_says_where_it_is(self):
        z = templates.render_tidepool_zshrc()
        assert "in a dive · %n@$(</etc/ccc/dive/where)" in z
        assert "in a tidepool · %n@$(</etc/ccc/cluster-name)" in z
        assert 'cd -- "$_ccc_main"' in z


class TestMainLockNamespaces:
    """The lock runs `mount` inside the Tidepool: it needs every ns. Without the user ns the bind is
    refused; without the pid ns /proc/self is missing and `mount -o remount` fails."""

    @pytest.mark.parametrize("fn", ["apply_lock", "release_lock"])
    def test_enters_all_ns(self, monkeypatch, fn):
        ran = []
        monkeypatch.setattr(H, "_ctr_pid", lambda u: 4242)
        monkeypatch.setattr(H, "_inner", lambda d: "/home/bob/proj")
        monkeypatch.setattr(H, "sh", lambda *a, **k: ran.append(a))
        getattr(H, fn)({"user": "bob"})
        assert ran[0][:5] == ("nsenter", "-t", "4242", "-a", "--")
