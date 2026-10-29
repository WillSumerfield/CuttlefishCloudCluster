"""WSL Arms: one GPU behind /dev/dxg, cgroup v2 + systemd required, keep-alive printed for Windows."""

import re

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import enroll as head_enroll
from cuttle import ccc_launch as L
from cuttle import config, setup, system, templates
from test_enroll_identity import BODY, _authorize, _fake_headscale, app  # noqa: F401 (fixture)


class TestDevice:
    def test_a_wsl_box_reports_dxg(self, monkeypatch):
        monkeypatch.setattr(system, "is_wsl", lambda: True)
        monkeypatch.setattr(system.Path, "exists", lambda self: str(self) == "/dev/dxg")
        assert system.gpu_devices() == ["/dev/dxg"]

    def test_both_regexes_accept_dxg_and_nothing_looser(self):
        for rx in (re.compile(config.GPU_DEVICE_RE), head_enroll.GPU_DEVICE_RE):
            assert rx.match("/dev/dxg") and rx.match("/dev/nvidia3")
            assert not rx.match("/dev/dxg0") and not rx.match("/dev/nvidiactl")

    def test_gres_conf_line(self):
        assert "File=/dev/dxg" in templates.GRES_CONF_LINE.format(node="w", device="/dev/dxg")


class TestPreflight:
    @pytest.fixture(autouse=True)
    def _abort(self, monkeypatch):
        monkeypatch.setattr(setup.ui, "abort", lambda msg, next_step=None, **k:
                            RuntimeError(f"{msg} | {next_step}"))

    @pytest.mark.parametrize("cards,pid1,v2,msg", [
        (2, "systemd", True, "exactly one GPU"),
        (1, "init", True, "without systemd"),
        (1, "systemd", False, "cgroup v1"),
    ])
    def test_refused_before_anything_changes(self, cards, pid1, v2, msg):
        with pytest.raises(Exception, match=msg):
            setup._preflight_wsl(cards, pid1, v2)

    def test_cgroup_refusal_names_the_windows_fix(self):
        with pytest.raises(Exception) as e:
            setup._preflight_wsl(1, "systemd", False)
        assert "kernelCommandLine = cgroup_no_v1=all" in str(e.value) and "wsl --shutdown" in str(e.value)

    def test_a_ready_box_passes(self):
        setup._preflight_wsl(1, "systemd", True)


def test_keepalive_task_create_and_delete(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    create, delete = system.wsl_keepalive_cmds()
    assert "/sc onlogon" in create and "wsl.exe -d Ubuntu-24.04 --exec sleep infinity" in create
    assert f'/delete /tn "{config.WSL_KEEPALIVE_TASK}"' in delete


class TestHeadEnroll:
    def test_dxg_alone_enrolls(self, app, monkeypatch):  # noqa: F811
        _authorize("arm1")
        _fake_headscale({"arm1": ["100.99.0.5"]}, monkeypatch)
        r = TestClient(app, client=("100.99.0.5", 9)).post(
            "/enroll", json={**BODY, "gpu_devices": ["/dev/dxg"]})
        assert r.status_code == 200

    def test_dxg_beside_another_device_is_refused(self, app):  # noqa: F811
        r = TestClient(app, client=("100.99.0.5", 9)).post(
            "/enroll", json={**BODY, "gpu_devices": ["/dev/dxg", "/dev/nvidia0"]})
        assert r.status_code == 422 and "only device" in r.text


class TestLaunch:
    def test_wsl_cdi_name_is_all(self):
        assert L.cdi_names(["GPU-aaa"], wsl=True) == ["all"]
        assert L.cdi_names([], wsl=True) == []
        assert L.cdi_names(["GPU-aaa"], wsl=False) == ["GPU-aaa"]

    def test_a_name_missing_from_the_cdi_spec_regenerates_it(self, tmp_path, monkeypatch):
        """nvidia-cdi-refresh runs at boot / driver change only: MIG switched on later left the
        instance's UUID unresolvable (dex Dive 20)."""
        spec = tmp_path / "nvidia.yaml"
        spec.write_text('devices:\n    - name: "0"\n    - name: GPU-aaa\n    - name: all\n')
        monkeypatch.setattr(L, "CDI_SPEC", spec)
        ran = []
        monkeypatch.setattr(L.subprocess, "run", lambda argv, **kw: ran.append(argv))
        L.ensure_cdi(["GPU-aaa"])
        L.ensure_cdi(["all"])
        L.ensure_cdi([])
        assert ran == []
        L.ensure_cdi(["GPU-aaa", "MIG-bbb"])
        assert ran == [["/usr/bin/systemctl", "start", "nvidia-cdi-refresh.service"],
                       [L.CDI_PATCH_BIN, str(spec)]]

    def test_argv_uses_cdi_names_but_ccc_gpus_keeps_the_uuid(self):
        argv = L.podman_argv({"image": "x"}, "1", 2**31, "/s", ["GPU-aaa"], L.Path("/j"), [],
                             cdi=["all"])
        s = " ".join(argv)
        assert "--device nvidia.com/gpu=all" in s and "nvidia.com/gpu=GPU-aaa" not in s
        assert "-e CCC_GPUS=GPU-aaa" in s

    def test_a_wsl_job_holding_a_second_index_is_refused(self, monkeypatch):
        monkeypatch.setattr(L, "WSL_DXG", type("P", (), {"exists": lambda self: True})())
        monkeypatch.setattr(L, "_device_minors", lambda: [])
        with pytest.raises(L.Refused):
            L.gpu_ids([1])


def test_mig_shell_skips_a_box_with_no_nvidiaN():
    assert "compgen -G '/dev/nvidia[0-9]*' >/dev/null || return 0" in templates.render_epilog()


def test_launcher_finds_wsls_nvidia_smi(monkeypatch):
    monkeypatch.setattr(L.os.path, "exists", lambda p: p == "/usr/lib/wsl/lib/nvidia-smi")
    assert L.nvidia_smi() == "/usr/lib/wsl/lib/nvidia-smi"
    monkeypatch.setattr(L.os.path, "exists", lambda p: True)
    assert L.nvidia_smi() == "/usr/bin/nvidia-smi"


class TestDiskFloor:
    @pytest.fixture(autouse=True)
    def _abort(self, monkeypatch):
        monkeypatch.setattr(setup.ui, "abort", lambda msg, next_step=None, **k: RuntimeError(msg))

    def _free(self, monkeypatch, wsl, free):
        monkeypatch.setattr(system, "is_wsl", lambda: wsl)
        monkeypatch.setattr(setup, "_free_gb", lambda p: free[str(p)])

    def test_a_roomy_native_box_passes_without_checking_windows(self, monkeypatch):
        self._free(monkeypatch, False, {str(config.CONTAINERS_STORE): 100})
        setup.preflight_disk()

    def test_a_full_store_is_refused(self, monkeypatch):
        self._free(monkeypatch, False, {str(config.CONTAINERS_STORE): 10})
        with pytest.raises(RuntimeError, match="only 10 GB free"):
            setup.preflight_disk()

    def test_wsl_checks_the_windows_drive_behind_the_vhdx(self, monkeypatch):
        self._free(monkeypatch, True, {str(config.CONTAINERS_STORE): 900,
                                       str(config.WSL_WINDOWS_DRIVE): 5})
        with pytest.raises(RuntimeError, match="/mnt/c"):
            setup.preflight_disk()


def test_free_gb_walks_up_to_an_existing_dir(tmp_path):
    assert setup._free_gb(tmp_path / "not" / "yet") > 0
