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
        assert H.registry() == config.registry() == "ccc.lab:5000"
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


# --- ccc-launch, Dive mode ---------------------------------------------------------

from cuttle import ccc_launch as L  # noqa: E402

DIVE = {"token": TOK, "user": "bob", "secret": "s" * 43, "head": "100.99.0.1", "main": "proj/a",
        "deps": ["data"], "port": 31005}


class TestLaunchSpec:
    def test_constants_agree_with_the_helper(self):
        assert L.DIVE_FILTERS == H.FILTER_ARGS
        assert L.REL_RE.pattern == H.REL_RE.pattern and L.TOKEN_RE.pattern == H.TOKEN_RE.pattern
        assert L.TIDEPOOL_HELPER == H.HELPER and L.DIVE_RECORDS == H.DIVE_RECORDS
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
        home = L.stage_in(DIVE, tmp_path, 4242)
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
        assert f"{L.ENV_CACHE}:/ccc-cache:idmap" in vols
        assert "-p 100.99.0.7:31005:22" in j and "--device nvidia.com/gpu=GPU-1" in j
        assert "--cgroup-parent /s/job_77/step_batch" in j
        assert "--privileged" not in j and "--network host" not in j
        assert a[-4:] == [spec["image"], "/bin/sh", "-c", "x"]

    def test_a_dot_main_folder_is_never_bound_read_only(self, tmp_path):
        home = tmp_path / "home"
        (home / ".work").mkdir(parents=True)
        (home / ".bashrc").write_text("x")
        assert [d for _, d in L.dotfile_binds(home, "bob", [".work"])] == ["/home/bob/.bashrc"]


class TestHeadDive:
    def test_detected_by_the_helpers_record(self, tmp_path, monkeypatch):
        helper = tmp_path / "helper"
        helper.write_text("")
        monkeypatch.setattr(L, "TIDEPOOL_HELPER", str(helper))
        monkeypatch.setattr(L, "DIVE_RECORDS", tmp_path)
        assert not L.head_dive(TOK)
        (tmp_path / f"{TOK}.json").write_text("{}")
        assert L.head_dive(TOK)

    def test_grant_then_revoke_on_the_way_out(self, monkeypatch):
        calls = []
        monkeypatch.setattr(L.subprocess, "run", lambda a, **k: calls.append(a))
        monkeypatch.setattr(L, "_device_minors", lambda: [0, 1, 2])
        monkeypatch.setattr(L.signal, "signal", lambda *a: None)

        class Ev:
            def set(self): pass
            def wait(self, t): return True
        monkeypatch.setattr(L.threading, "Event", Ev)
        L.run_head_dive(DIVE, "77", {"gres_idx": [1], "cpus": 8, "mem_mb": 32768})
        assert calls == [[L.TIDEPOOL_HELPER, "dive-gpu", "grant", TOK, "77", "1", "8", "32768"],
                         [L.TIDEPOOL_HELPER, "dive-gpu", "revoke", TOK]]
