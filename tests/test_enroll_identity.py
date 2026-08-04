"""`POST /enroll` doesn't trust the request body's `mesh_ip`. It asks Headscale who is actually
registered at the connecting address (`request.client.host`) and refuses a mismatch, on top of
the pre-existing "was this name ever authorized" check. See ccc_head_api.enroll's module
docstring and `_node_named_at`.

`subprocess.run` stands in for `sudo -n headscale nodes list --output json` — the one sudo grant
this service has (cuttle.headapi.install_enroll_sudoers). Real headscale isn't exercised here,
only the trust decision this endpoint makes from its output.
"""

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import enroll

BODY = {"node_name": "arm1", "gpu_devices": ["/dev/nvidia0"], "mesh_ip": "9.9.9.9",  # ignored now
       "vram_mb": 24564}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(enroll, "MUNGE_KEY", tmp_path / "munge.key")
    enroll.MUNGE_KEY.write_bytes(b"the-cluster-secret")
    monkeypatch.setattr(enroll, "REGISTRY", tmp_path / "arms.json")
    monkeypatch.setattr(enroll, "APPTAINER_RECORD", tmp_path / "apptainer.json")
    enroll.APPTAINER_RECORD.write_text(
        json.dumps({"version": "1.5.3", "filename": "apptainer.deb", "sha256": "x"}))
    monkeypatch.setenv("CCC_CTLD_ADDR", "100.99.0.1:6817")
    monkeypatch.setenv("CCC_CLUSTER", "ccc")
    monkeypatch.setenv("CCC_PARTITION", "ccc")
    return enroll.app


def _authorize(name: str, owner: str = "admin") -> None:
    """What `cuttle arm authorize` writes before the token is ever handed out (enroll.py:463-464)."""
    reg = enroll._load()
    reg[name] = {"owner": owner, "gpu_devices": None, "mesh_ip": None, "admitted": False,
                "vram_mb": None, "powers": None, "gpu": None}
    enroll._save(reg)


def _fake_headscale(nodes: dict[str, list[str]], monkeypatch, calls: list | None = None):
    """`nodes`: {given_name: [ip, ...]} — the shape `headscale nodes list --output json` returns,
    trimmed to the two fields `_node_named_at` reads."""
    payload = [{"given_name": name, "ip_addresses": ips} for name, ips in nodes.items()]

    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(enroll.subprocess, "run", run)


class TestIdentityCheck:
    def test_the_real_node_at_that_address_enrolls(self, app, monkeypatch):
        _authorize("arm1")
        _fake_headscale({"arm1": ["100.99.0.5"]}, monkeypatch)
        r = TestClient(app, client=("100.99.0.5", 9)).post("/enroll", json=BODY)
        assert r.status_code == 200
        # The VERIFIED peer address is what lands in the registry, never the request body's claim.
        assert enroll._load()["arm1"]["mesh_ip"] == "100.99.0.5"

    def test_an_admitted_arm_cannot_claim_a_different_arms_name(self, app, monkeypatch):
        """The gap this closes: arm2's owner, really connecting from 100.99.0.6, POSTs
        node_name=arm1 hoping to be handed arm1's key."""
        _authorize("arm1")
        _authorize("arm2")
        _fake_headscale({"arm1": ["100.99.0.5"], "arm2": ["100.99.0.6"]}, monkeypatch)
        r = TestClient(app, client=("100.99.0.6", 9)).post("/enroll", json=BODY)
        assert r.status_code == 403
        assert enroll._load()["arm1"]["mesh_ip"] is None  # arm1's row is untouched

    def test_an_address_headscale_has_no_node_for_is_refused(self, app, monkeypatch):
        _authorize("arm1")
        _fake_headscale({}, monkeypatch)
        r = TestClient(app, client=("100.99.0.5", 9)).post("/enroll", json=BODY)
        assert r.status_code == 403

    def test_an_undeclared_name_is_refused_before_any_headscale_call(self, app, monkeypatch):
        """The cheap, pre-existing gate short-circuits first — no sudo/headscale call is spent on a
        name nobody ever authorized."""
        calls = []
        _fake_headscale({"arm1": ["100.99.0.5"]}, monkeypatch, calls)
        r = TestClient(app, client=("100.99.0.5", 9)).post(
            "/enroll", json={**BODY, "node_name": "ghost"})
        assert r.status_code == 403
        assert calls == []

    def test_a_broken_sudoers_rule_fails_loud_not_silent(self, app, monkeypatch):
        """`sudo -n` exits non-zero (rule missing, or headscale down) — a 503 that says so, never a
        silent bypass of the identity check."""
        _authorize("arm1")
        monkeypatch.setattr(enroll.subprocess, "run",
                           lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="",
                                                                         stderr="not permitted"))
        r = TestClient(app, client=("100.99.0.5", 9)).post("/enroll", json=BODY)
        assert r.status_code == 503
