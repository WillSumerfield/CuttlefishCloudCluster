"""Dives (phase 4): the root helper's Dive verbs — folder checks, rsync filters (real rsync), modules,
locks, the Head-GPU grant/revoke — plus the templates and constants they share.

NOT tested: Podman commit/push, nsenter, systemd device policy, a real Arm — hardware list.
"""

import json
import subprocess

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
        assert H.REGISTRY == config.REGISTRY
        assert H.CA_CERT == str(config.HEAD_CA_CERT)
        assert H.HELPER == str(config.TIDEPOOL_HELPER_BIN)
        assert H.CDI_SPEC == config.CDI_SPEC


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
        assert set(mods) == {f"d{TOK}-main", f"d{TOK}-dep0", f"d{TOK}-sys"}
        main, dep, sysm = mods[f"d{TOK}-main"], mods[f"d{TOK}-dep0"], mods[f"d{TOK}-sys"]
        assert main["read only"] == "false" and dep["read only"] == sysm["read only"] == "true"
        assert main["uid"] == dep["uid"] == str(u)
        assert main["path"] == str(home / "proj") and dep["path"] == str(home / "data")
        assert main["pre-xfer exec"] == f"/usr/local/sbin/ccc-tidepool dive-lock {TOK}"
        assert "pre-xfer exec" not in dep
        assert sysm["path"] == str(H.DIVE_STATE / TOK)
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
                                 "image": "img@sha256:" + "c" * 64})
        assert (H.DIVE_RSYNCD / f"{TOK}.secrets").read_text() == f"d{TOK}:s3\n"
        assert (H.DIVE_STATE / TOK / "sys" / "ssh_host_ed25519_key").exists()
        assert H.dive_record(TOK)["main"] == "proj"
        deleted = []
        monkeypatch.setattr(H, "delete_manifest", deleted.append)
        H.dive_close(TOK)
        assert deleted == ["img@sha256:" + "c" * 64]
        assert not list(H.DIVE_RSYNCD.iterdir()) and not list(H.DIVE_RECORDS.iterdir())
        assert not (H.DIVE_STATE / TOK).exists()

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


class TestHeadGpu:
    S = {"cpus": 4, "mem_gb": 16, "total_cpus": 32, "total_mem_gb": 128}

    @pytest.fixture
    def ran(self, helper_dirs, monkeypatch):
        calls = []
        monkeypatch.setattr(H, "sh", lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(
            a, 0, "", ""))
        monkeypatch.setattr(H, "settings", lambda: self.S)
        monkeypatch.setattr(H, "scope", lambda u: "libpod-x.scope")
        monkeypatch.setattr(H, "kill_gpu_holders", lambda u: calls.append(("killed", u)) or 1)
        H.save_dive(TOK, {"token": TOK, "user": "bob", "uid": 60003, "main": "proj", "deps": [],
                          "locked": False, "head": None, "image": "i"})
        return calls

    def _allow(self, calls):
        c = [c for c in calls if "set-property" in c and "libpod-x.scope" in c][-1]
        return [a.split("=", 1)[1] for a in c if a.startswith("DeviceAllow=") and a != "DeviceAllow="]

    def test_grant_allows_only_the_allocated_card_and_raises_caps(self, ran):
        H.dive_gpu_grant(TOK, "77", [1], 16, 65536)
        allow = self._allow(ran)
        assert "/dev/nvidia1 rw" in allow and "/dev/nvidiactl rw" in allow
        assert "/dev/nvidia0 rw" not in allow
        upd = next(c for c in ran if "update" in c)
        assert upd[upd.index("--cpus") + 1] == "16" and upd[upd.index("--memory") + 1] == "65536m"
        sl = next(c for c in ran if config.TIDEPOOL_SLICE in c)
        assert "CPUQuota=4800%" in sl and f"MemoryMax={128 * 1024 + 65536}M" in sl

    def test_revoke_denies_kills_holders_and_restores(self, ran):
        H.dive_gpu_grant(TOK, "77", [1], 16, 65536)
        ran.clear()
        H.dive_gpu_revoke(TOK)
        assert not any("nvidia" in a for a in self._allow(ran))
        assert ("killed", "bob") in ran
        upd = next(c for c in ran if "update" in c)
        assert upd[upd.index("--cpus") + 1] == "4" and upd[upd.index("--memory") + 1] == "16384m"
        assert H.dive_record(TOK)["head"] is None

    def test_epilog_backstop_finds_the_dive_by_job_id(self, ran):
        H.dive_gpu_grant(TOK, "77", [0], 4, 1024)
        H.dive_gpu_revoke_job("78")
        assert H.dive_record(TOK)["head"]
        H.dive_gpu_revoke_job("77")
        assert H.dive_record(TOK)["head"] is None

    def test_restart_and_reset_refused_while_a_head_dive_holds_the_gpu(self, ran):
        H.dive_gpu_grant(TOK, "77", [0], 4, 1024)
        assert H.main(["restart", "bob"]) == 2
        assert H.main(["reset", "bob"]) == 2

    def test_epilog_calls_the_backstop(self):
        assert "dive-gpu revoke-job \"$SLURM_JOB_ID\"" in templates.render_epilog()


class TestRegistry:
    def test_push_registry_allows_delete_pull_does_not(self):
        assert "enabled: true" in templates.render_registry_config("127.0.0.1", readonly=False) \
            .split("delete:")[1].split("\n")[1]
        assert "enabled: false" in templates.render_registry_config("100.99.0.1", readonly=True) \
            .split("delete:")[1].split("\n")[1]

    def test_gc_is_a_nightly_helper_run(self):
        assert "ExecStart=/usr/local/sbin/ccc-tidepool registry-gc" in templates.render_registry_gc_service()
        assert "OnCalendar=" in templates.REGISTRY_GC_TIMER_TMPL
