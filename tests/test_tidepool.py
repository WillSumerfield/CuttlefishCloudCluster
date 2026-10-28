"""Tidepool (phase 3): the root helper's argv and refusals, the shared templates, the Slurm reserve, the
`Host lab` alias, and the Head API's status/keys/restart routes.

NOT tested: a real loop image, Podman, sshd or systemd — hardware list (temp/2.0-plan.md).
"""

import json
import re
from pathlib import Path

import pytest

from ccc_head_api import admin
from ccc_head_api import db
from ccc_head_api import tidepool as head_tp
from cuttle import ccc_launch as L
from cuttle import ccc_tidepool as H
from cuttle import config, sshconfig, system, templates
from cuttle import tidepool as cli_tp


class TestConstantsAgree:
    """Three packages that do not import each other: the helper, the CLI, the Head API."""

    def test_helper_matches_config(self):
        assert str(H.AUTH_FILE) == str(config.REGISTRY_AUTH)
        assert (H.SETTINGS, H.IMAGES_RECORD, H.MNT_ROOT, H.KEYS_ROOT, H.FILES_DIR, H.RECORDS) == (
            config.TIDEPOOL_SETTINGS, config.IMAGES_RECORD, config.TIDEPOOL_MNT_ROOT,
            config.TIDEPOOL_KEYS_ROOT, config.TIDEPOOL_FILES_DIR, config.TIDEPOOL_RECORDS)
        assert (H.SLICE, H.SERVICE, H.API_USER) == (config.TIDEPOOL_SLICE, config.TIDEPOOL_SERVICE,
                                                     config.HEAD_API_USER)
        assert (H.UID_FLOOR, H.UID_CEIL, H.SSH_PORT_BASE) == (config.CCC_UID, config.GUISE_UID_MAX,
                                                               config.TIDEPOOL_SSH_PORT_BASE)

    def test_head_api_matches_config(self):
        assert (str(head_tp.SETTINGS), str(head_tp.MNT_ROOT), str(head_tp.KEYS_ROOT), head_tp.HELPER) \
            == (str(config.TIDEPOOL_SETTINGS), str(config.TIDEPOOL_MNT_ROOT),
                str(config.TIDEPOOL_KEYS_ROOT), str(config.TIDEPOOL_HELPER_BIN))
        assert head_tp.cluster_name() == config.cluster_name() == "lab"
        assert head_tp.ssh_port(60002) == H.ssh_port(60002) == 32002

    def test_username_rule_is_one_rule(self):
        assert H.USER_RE.pattern == admin.USERNAME_RE.pattern
        assert "[a-z][a-z0-9_-]{0,31}" in templates.render_tidepool_sudoers()

    def test_id_range_is_ccc_launchs(self):
        assert H.id_base(60002) == L.id_base(60002)
        with pytest.raises(H.Refused):
            H.id_base(60000)   # the Mantle has no Tidepool


class TestHelperArgv:
    S = {"cpus": 4, "mem_gb": 16}
    BASE = "ccc.lab:5000/ccc/base@sha256:" + "a" * 64

    def argv(self, user="bob", uid=60003):
        return H.create_argv(user, uid, self.S, self.BASE, "100.99.0.1")

    def test_container_is_unprivileged_and_gpu_free(self):
        a = " ".join(self.argv())
        b = H.id_base(60003)
        assert f"--uidmap 0:{b}:65536 --gidmap 0:{b}:65536" in a
        for bad in ("--privileged", "--device", "nvidia", "--network host", "--pid", "--ipc"):
            assert bad not in a

    def test_a_gpu_head_gives_every_gpu_via_cdi(self):
        a = H.create_argv("bob", 60003, self.S, self.BASE, "100.99.0.1", gpus=True)
        assert a[a.index("--device") + 1] == "nvidia.com/gpu=all"
        assert f"ccc.tidepool.spec={H.SPEC_VERSION}" in a

    def test_caps_port_network_and_slice(self):
        a = " ".join(self.argv())
        assert "--cpus 4 --memory 16g --memory-swap 16g" in a
        assert "-p 32003:22" in a and "--network ccc-tp-bob" in a
        assert f"--cgroup-parent {config.TIDEPOOL_SLICE}" in a and "--init" in a

    def test_mounts(self):
        a = self.argv()
        mounts = [a[i + 1] for i, x in enumerate(a) if x == "-v"]
        assert mounts == [f"{config.TIDEPOOL_MNT_ROOT}/bob/home:/home/bob",
                          f"{config.TIDEPOOL_MNT_ROOT}/bob/ssh:/etc/ccc/hostkeys:ro",
                          f"{config.TIDEPOOL_KEYS_ROOT}/bob:/etc/ccc/keys:ro",
                          f"{config.TIDEPOOL_FILES_DIR}:/etc/ccc/tidepool:ro",
                          f"{config.CONTAINER_CUTTLE}:{config.CONTAINER_CUTTLE}:ro",
                          f"{config.CLUSTER_NAME_FILE}:/etc/ccc/cluster-name:ro",
                          f"{config.HEAD_CA_CERT}:{config.CONTAINER_CA}:ro"]
        assert a[a.index("--add-host") + 1] == "ccc.lab:100.99.0.1"
        assert a[-2:] == [self.BASE, "/etc/ccc/tidepool/init"]

    def test_own_store_inside_the_image(self):
        p = H.podman("bob")
        assert p[p.index("--root") + 1] == f"{config.TIDEPOOL_MNT_ROOT}/bob/storage"
        assert "additionalimagestore=/var/lib/containers/storage" in p

    def test_unit_runs_the_same_podman(self):
        assert " ".join(H.podman("%i")).replace("podman", "/usr/bin/podman", 1) == \
            templates.tidepool_podman()

    def test_mount_unit_name_is_systemd_escaped(self):
        assert H.mount_unit("bob") == "var-lib-ccc-tidepools-bob.mount"
        assert H.mount_unit("a-b") == "var-lib-ccc-tidepools-a\\x2db.mount"


class TestHelperRefuses:
    @pytest.fixture(autouse=True)
    def _no_side_effects(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError(f"ran {a}")
        monkeypatch.setattr(H, "sh", boom)
        monkeypatch.setattr(H, "settings", lambda: {"image_dir": "/x", "size_gb": 200})

    @pytest.mark.parametrize("argv", [["create", "Bob", "60003"], ["create", "bob", "60000"],
                                      ["create", "bob", "x"], ["reset", "../etc"], ["wipe", "bob"],
                                      ["teardown", "--force"], []])
    def test_bad_input_exits_2(self, argv):
        assert H.main(argv) == 2

    def test_a_full_disk_is_refused_before_fallocate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(H, "free_bytes", lambda p: 100 * 2**30)
        with pytest.raises(H.Refused, match="not enough for another 200 GB"):
            H.ensure_image("bob", {"image_dir": str(tmp_path), "size_gb": 200})

    def test_restart_of_a_user_with_no_tidepool(self, monkeypatch, tmp_path):
        monkeypatch.setattr(H, "RECORDS", tmp_path)
        assert H.main(["restart", "bob"]) == 2


class TestImageReservation:
    """The image's space is taken up front, and stays taken."""

    @pytest.fixture
    def ran(self, monkeypatch):
        cmds = []
        def sh(*a, **k):
            cmds.append(list(a))
            if a[0] == "fallocate":
                Path(a[-1]).touch()
        monkeypatch.setattr(H, "sh", sh)
        monkeypatch.setattr(H, "free_bytes", lambda p: 2**50)
        return cmds

    def test_mkfs_does_not_discard_the_fallocated_blocks(self, ran, tmp_path):
        H.ensure_image("bob", {"image_dir": str(tmp_path), "size_gb": 200})
        assert ran[0][0] == "fallocate"
        mkfs = next(c for c in ran if c[0] == "mkfs.ext4")
        assert mkfs[mkfs.index("-E") + 1] == "nodiscard"

    def test_an_existing_image_has_its_holes_refilled_at_its_own_size(self, ran, tmp_path):
        img = tmp_path / "bob.img"
        img.write_bytes(b"x" * 4096)
        H.ensure_image("bob", {"image_dir": str(tmp_path), "size_gb": 200})
        assert ran == [["fallocate", "-l", "4096", str(img)]]


class TestEnsureContainer:
    BASE = TestHelperArgv.BASE

    @pytest.fixture
    def ran(self, monkeypatch, tmp_path):
        rec = tmp_path / "images.json"
        rec.write_text(json.dumps({"base": self.BASE}))
        monkeypatch.setattr(H, "IMAGES_RECORD", rec)
        self.exists = False     # does `container exists` say yes (images always do)
        monkeypatch.setattr(H, "ok", lambda *a: self.exists if "container" in a else True)
        calls = []
        monkeypatch.setattr(H, "sh", lambda *a, **k: calls.append(a))
        return calls

    def verbs(self, calls):
        return [next(x for x in c if x in ("update", "rm", "create", "stop")) for c in calls]

    def test_same_base_only_recaps(self, ran, monkeypatch):
        monkeypatch.setattr(H, "_container_image", lambda u: f"{self.BASE} {H.SPEC_VERSION}")
        H.ensure_container("bob", 60003, TestHelperArgv.S)
        assert self.verbs(ran) == ["update"]

    def test_new_release_base_recreates(self, ran, monkeypatch):
        monkeypatch.setattr(H, "_container_image", lambda u: f"old@sha256:{'b' * 64} {H.SPEC_VERSION}")
        H.ensure_container("bob", 60003, TestHelperArgv.S)
        assert self.verbs(ran) == ["stop", "rm", "create"]

    def test_new_create_spec_recreates(self, ran, monkeypatch):
        monkeypatch.setattr(H, "_container_image", lambda u: f"{self.BASE} 1")
        H.ensure_container("bob", 60003, TestHelperArgv.S)
        assert self.verbs(ran) == ["stop", "rm", "create"]

    def test_none_yet_creates(self, ran, monkeypatch):
        monkeypatch.setattr(H, "_container_image", lambda u: None)
        H.ensure_container("bob", 60003, TestHelperArgv.S)
        assert self.verbs(ran) == ["create"]

    def test_a_container_whose_image_was_wiped_is_recreated(self, ran, monkeypatch):
        """inspect fails ('image not known') yet the name is still taken."""
        monkeypatch.setattr(H, "_container_image", lambda u: None)
        self.exists = True
        H.ensure_container("bob", 60003, TestHelperArgv.S)
        assert self.verbs(ran) == ["stop", "rm", "create"]


class TestTemplates:
    def test_sudoers_is_restart_reset_dive_and_fs_verbs_only(self):
        rules = [l.split("ccc-tidepool ", 1)[1] for l in templates.render_tidepool_sudoers().splitlines()
                 if l.startswith("ccc ")]
        assert rules == ["^(restart|reset) [a-z][a-z0-9_-]{0,31}$",
                         "^dive-(prep|snap|open) [a-z][a-z0-9_-]{0,31} [0-9a-f]{32}$",
                         "^dive-close [0-9a-f]{32}$",
                         "^fs [a-z][a-z0-9_-]{0,31} (list|stat|get|zip|put|mkdir|delete|rename) .+$"]
        # the root-only verbs never reach the API
        for verb in ("dive-lock", "dive-gpu", "started", "registry-gc", "create", "remove", "teardown"):
            assert not any(re.match(r, f"{verb} bob") for r in rules)

    def test_slice_caps_the_total(self):
        s = templates.render_tidepool_slice(32, 128)
        assert "CPUQuota=3200%" in s and "MemoryMax=128G" in s

    def test_unit_starts_the_created_container(self):
        u = templates.render_tidepool_unit()
        assert "start --attach tp-%i" in u and "Restart=always" in u
        assert f"RequiresMountsFor={config.TIDEPOOL_MNT_ROOT}/%i" in u
        assert " run " not in u and "--rm" not in u   # the apt layer must survive a restart
        assert "ExecStartPost=-/usr/local/sbin/ccc-tidepool started %i" in u

    def test_init_makes_uid_1000_with_sudo_then_execs_sshd(self):
        s = templates.render_tidepool_init()
        assert "--uid 1000" in s and "NOPASSWD:ALL" in s
        assert s.rstrip().splitlines()[-1].startswith("exec /usr/sbin/sshd -D -e")

    def test_sshd_is_key_only(self):
        c = dict(l.split(" ", 1) for l in templates.TIDEPOOL_SSHD_CONFIG.splitlines()
                 if l and not l.startswith("#"))
        assert c["PasswordAuthentication"] == c["KbdInteractiveAuthentication"] == "no"
        assert c["PermitRootLogin"] == "no"
        assert c["AuthorizedKeysFile"].split() == [".ssh/authorized_keys", "/etc/ccc/keys/authorized_keys"]

    def test_base_drops_ubuntus_uid_1000(self):
        assert "userdel -r ubuntu" in templates.render_base_containerfile()


class TestSlurmReserve:
    @pytest.fixture(autouse=True)
    def _box(self, monkeypatch):
        monkeypatch.setattr(system, "hostname", lambda: "dex")
        monkeypatch.setattr(system, "cpu_topology", lambda: (2, 32, 2))
        monkeypatch.setattr(system, "real_memory_mb", lambda: 514000)
        monkeypatch.setattr(system, "gpu_vram_mb", lambda: 97887)
        monkeypatch.setattr(system, "gpu_devices", lambda: ["/dev/nvidia0", "/dev/nvidia1"])

    def node(self, reserve):
        conf = templates.render_slurm_conf("100.99.0.1", reserve=reserve)
        return next(l for l in conf.splitlines() if l.startswith("NodeName="))

    def test_dex_gives_32_cpus_and_128_gb(self):
        n = self.node((32, 128 * 1024))
        assert "CoresPerSocket=24" in n and f"RealMemory={514000 - 131072}" in n

    def test_partial_cores_round_up(self):
        assert "CoresPerSocket=31" in self.node((3, 0))

    def test_no_tidepools_no_change(self):
        assert "CoresPerSocket=32" in self.node((0, 0)) and "RealMemory=514000" in self.node((0, 0))


class TestSettings:
    def test_defaults_scale_with_the_box(self, monkeypatch):
        monkeypatch.setattr(system, "cpu_count", lambda: 128)
        monkeypatch.setattr(system, "real_memory_mb", lambda: 514000)
        d = cli_tp.default_settings()
        assert (d["size_gb"], d["cpus"], d["mem_gb"], d["total_cpus"]) == (200, 4, 16, 32)

    def test_image_dir_under_ccc_lib_is_refused(self, monkeypatch):
        answers = iter(["/var/lib/ccc/imgs"])
        monkeypatch.setattr(cli_tp.ui, "ask_text", lambda *a, **k: next(answers))
        monkeypatch.setattr(cli_tp.ui, "ask_int", lambda *a, **k: 1)
        monkeypatch.setattr(cli_tp.ui, "abort", lambda msg, **k: RuntimeError(msg))
        with pytest.raises(RuntimeError, match="outside"):
            cli_tp.prompt_settings(None)

    def test_no_settings_no_reserve(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "TIDEPOOL_SETTINGS", tmp_path / "none.json")
        assert cli_tp.slurm_reserve() == (0, 0)


class TestTheAlias:
    def test_no_block_before_login(self, ccc_home):
        assert sshconfig.tidepool_block() == ""

    def test_block_pins_the_host_key(self, ccc_home):
        config.ensure_ccc_home()
        config.TIDEPOOL_SSH_META.write_text(json.dumps({"host": "dex.example", "port": 32002,
                                                        "user": "alice"}))
        sshconfig.refresh()
        text = config.SSH_CCC_CONFIG.read_text()
        assert "Host lab\n  HostName dex.example\n  Port 32002\n  User alice\n" in text
        assert "StrictHostKeyChecking yes" in text and "StrictHostKeyChecking no" not in text
        assert f"HostKeyAlias {config.TIDEPOOL_HOSTKEY_ALIAS}" in text
        assert f"UserKnownHostsFile {config.TIDEPOOL_KNOWN_HOSTS}" in text


PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBlobxxxxxxxxxxxxxxxxxxxxxxxxxxxx ccc@laptop"


def _login(head, name="alice", pw="hunter2"):
    return {"Authorization": "Bearer " + head.post("/login", json={"username": name, "password": pw})
            .json()["token"]}


@pytest.fixture
def keys_root(monkeypatch, tmp_path):
    root = tmp_path / "keys"
    (root / "alice").mkdir(parents=True)
    (root / "alice" / "host_ed25519.pub").write_text("ssh-ed25519 AAAAhost tidepool-alice\n")
    monkeypatch.setattr(head_tp, "KEYS_ROOT", root)
    monkeypatch.setattr(head_tp, "state", lambda u: "active")
    return root


class TestHeadApi:
    def test_status_is_the_callers_own(self, head, keys_root):
        r = head.get("/tidepool", headers=_login(head)).json()
        with db.connect() as conn:
            uid = db.guise_uid(conn, db.get_user(conn, "alice")["id"])
        assert (r["user"], r["port"], r["state"]) == ("alice", 32000 + uid - 60000, "active")
        assert r["host_key"] == "ssh-ed25519 AAAAhost" and r["alias"] == "lab"

    def test_keys_round_trip_into_authorized_keys(self, head, keys_root):
        h = _login(head)
        assert head.post("/ssh-keys", json={"pubkey": PUB, "name": "laptop"}, headers=h).json() \
            == {"added": True}
        assert head.post("/ssh-keys", json={"pubkey": PUB}, headers=h).json() == {"added": False}
        assert (keys_root / "alice" / "authorized_keys").read_text() == PUB + "\n"
        [k] = head.get("/ssh-keys", headers=h).json()
        assert head.delete(f"/ssh-keys/{k['id']}", headers=h).status_code == 200
        assert (keys_root / "alice" / "authorized_keys").read_text() == ""

    @pytest.mark.parametrize("bad", ["not a key", PUB + "\ncommand=\"sh\" " + PUB,
                                     'command="sh" ' + PUB, "ssh-dss AAAA"])
    def test_only_a_plain_key_line_is_accepted(self, head, keys_root, bad):
        assert head.post("/ssh-keys", json={"pubkey": bad}, headers=_login(head)).status_code == 422

    def test_another_users_key_cannot_be_removed(self, head, keys_root):
        head.post("/ssh-keys", json={"pubkey": PUB}, headers=_login(head))
        with db.connect() as conn:
            kid = db.ssh_keys(conn, db.get_user(conn, "alice")["id"])[0]["id"]
        assert head.delete(f"/ssh-keys/{kid}", headers=_login(head, "root", "toor")).status_code == 404

    def test_restart_names_only_the_caller(self, head, keys_root, monkeypatch):
        calls = []
        monkeypatch.setattr(head_tp, "act", lambda verb, user: calls.append((verb, user)))
        h = _login(head)
        assert head.post("/tidepool/reset", headers=h).status_code == 200
        assert head.post("/tidepool/remove", headers=h).status_code == 404
        assert calls == [("reset", "alice")]

    def test_act_goes_through_sudo_n(self, monkeypatch):
        seen = []
        monkeypatch.setattr(head_tp.subprocess, "run", lambda argv, **k: seen.append(argv))
        head_tp.act("restart", "alice")
        assert seen == [["sudo", "-n", "/usr/local/sbin/ccc-tidepool", "restart", "alice"]]


class TestLoginConnects:
    def test_login_uploads_the_key_and_writes_the_alias(self, cli, keys_root, monkeypatch):
        r = cli("login", "--head", "http://head", input="alice\nhunter2\n")
        assert r.exit_code == 0, r.output
        pub = config.TIDEPOOL_KEY.with_suffix(".pub").read_text().strip()
        assert (keys_root / "alice" / "authorized_keys").read_text() == pub + "\n"
        assert config.TIDEPOOL_KNOWN_HOSTS.read_text() == "ccc-tidepool ssh-ed25519 AAAAhost\n"
        assert re.search(r"Host lab\n  HostName head\n  Port 320\d\d\n  User alice\n",
                         config.SSH_CCC_CONFIG.read_text())
        # a second login re-uses the key, uploads nothing new
        assert cli("login", input="alice\nhunter2\n").exit_code == 0
        assert (keys_root / "alice" / "authorized_keys").read_text() == pub + "\n"


class TestBridgeDns:
    """ufw's default deny drops a Tidepool's DNS query to its bridge gateway (host INPUT)."""

    def test_rule_added_when_ufw_enforces_and_deleted_by_the_same_spec(self, monkeypatch):
        ran = []
        monkeypatch.setattr(system, "run", lambda cmd, **k: ran.append(cmd))
        monkeypatch.setattr(system, "ufw_active", lambda: True)
        monkeypatch.setattr(system.shutil, "which", lambda b: "/usr/sbin/ufw")
        system.allow_dns_from("10.89.0.0/16", "c")
        system.deny_dns_from("10.89.0.0/16")
        add, rm = ran
        assert add[:add.index("comment")] == ["ufw", "allow", "in", "from", "10.89.0.0/16", "to", "any",
                                               "port", "53"]
        assert rm == ["ufw", "delete", *add[1:add.index("comment")]]

    def test_left_alone_without_ufw(self, monkeypatch, capsys):
        monkeypatch.setattr(system, "run", lambda cmd, **k: (_ for _ in ()).throw(AssertionError(cmd)))
        monkeypatch.setattr(system, "ufw_active", lambda: False)
        system.allow_dns_from("10.89.0.0/16", "c")
