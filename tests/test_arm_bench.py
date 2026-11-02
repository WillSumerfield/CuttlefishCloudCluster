"""An Arm prices itself before its slurmd starts, so the Head's admit never waits on a donor's pull."""

import json

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import enroll as head_enroll
from cuttle import ccc_launch, enroll
from test_enroll_identity import BODY, HDR, _authorize, _fake_headscale, app  # noqa: F401 (fixture)


def test_weight_rides_in_conf():
    unit = enroll.slurmd_dropin("a", "h:1", 24564, 1, None, 123)
    assert '--conf "Gres=gpu:1 Feature=vram24 Weight=123"' in unit
    assert "Weight" not in enroll.slurmd_dropin("a", "h:1", 24564, 1)


def test_admit_does_not_benchmark(monkeypatch):
    monkeypatch.setattr(enroll, "save_registry", lambda reg: None)
    monkeypatch.setattr(enroll, "render_gres", lambda reg, w: None)
    monkeypatch.setattr(enroll, "benchmark_arm", lambda n: pytest.fail("admit benchmarked"))
    enroll._admit("a", {"a": {"admitted": False, "gpu_devices": ["/dev/dxg"]}}, None)


def test_reapply_weights_tolerates_an_unregistered_arm(monkeypatch):
    calls = []
    monkeypatch.setattr(enroll, "run", lambda cmd, **kw: calls.append(kw))
    enroll.reapply_weights({"a": {"admitted": True, "powers": [40.0]}})
    assert calls == [{"sudo": True, "check": False}]


@pytest.mark.parametrize("wsl,dev", [(True, "nvidia.com/gpu=all"), (False, "nvidia.com/gpu=GPU-a")])
def test_bench_locally_argv(monkeypatch, wsl, dev):
    monkeypatch.setattr(ccc_launch, "gpu_ids", lambda idx: ["GPU-a"])
    monkeypatch.setattr(enroll.system, "is_wsl", lambda: wsl)
    ran = []

    def run(cmd, **kw):
        ran.append(cmd)
        if cmd[:2] == ["podman", "run"]:
            out = next(a.split(":")[0] for a in cmd if a.endswith(":/out"))
            open(f"{out}/power", "w").write(json.dumps({"powers": [80.0], "gpu": "RTX", "vram_mb": 1}))
        elif cmd[:2] == ["rm", "-rf"]:
            import shutil; shutil.rmtree(cmd[2])
    monkeypatch.setattr(enroll, "run", run)
    assert enroll.bench_locally("img@sha256:x", 1)["powers"] == [80.0]
    s = " ".join(ran[0])
    assert f"--device {dev}" in s and "CCC_GPUS=GPU-a" in s and "--network none" in s


class TestHeadStoresPower:
    def _post(self, body):
        return TestClient(head_enroll.app, client=("100.99.0.5", 9)).post("/enroll", json=body, headers=HDR)

    @pytest.fixture(autouse=True)
    def _arm(self, app, monkeypatch):  # noqa: F811
        _authorize("arm1")
        _fake_headscale({"arm1": ["100.99.0.5"]}, monkeypatch)

    def test_powers_are_stored_and_returned(self):
        assert self._post({**BODY, "powers": [80.0], "gpu": "RTX"}).status_code == 200
        r = self._post(BODY)   # a later POST without powers keeps them
        assert r.json()["powers"] == [80.0] and head_enroll._load()["arm1"]["gpu"] == "RTX"

    @pytest.mark.parametrize("powers", [[1.0, 2.0], [0.0]])
    def test_bad_powers_refused(self, powers):
        assert self._post({**BODY, "powers": powers}).status_code == 422
