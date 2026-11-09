"""`cuttle dive start --detach` / `cuttle dive wait`: the no-shell path, for scripts and agents.
Detach must never open ssh; wait must write the alias and exit, and fail when the Dive is gone."""

import json

import pytest
import typer

from cuttle import dive, sshconfig, tidepool

QUEUED = {"id": 9, "main": "proj", "deps": [], "gpus": 1, "minutes": 240, "size_gb": 0.1, "notes": []}
QUEUE = [{"id": 3, "state": "running"}, {"id": 5, "state": "pending"}, {"id": 7, "state": "pending"},
         {"id": 9, "state": "pending"}]


class TestAhead:
    def test_counts_pending_dives_before_it(self):
        assert dive.ahead(9, QUEUE) == 2

    def test_running_or_absent_is_zero(self):
        assert dive.ahead(3, QUEUE) == 0
        assert dive.ahead(42, QUEUE) == 0


@pytest.fixture
def api(monkeypatch):
    calls = []

    def fake(method, path, **k):
        calls.append((method, path))
        return QUEUED if (method, path) == ("POST", "/dives") else QUEUE

    monkeypatch.setattr(dive.auth, "_api", fake)
    monkeypatch.setattr(dive, "in_container", lambda: False)
    monkeypatch.setattr(dive, "in_tidepool", lambda: False)
    monkeypatch.setattr(tidepool, "connect", lambda: None)
    monkeypatch.setattr(dive, "attach", lambda *a, **k: calls.append(("attach",)))
    return calls


class TestDetach:
    def test_queues_without_attaching(self, api):
        dive.request("proj", None, 1, 24, "4h", None, None, detach=True)
        assert ("attach",) not in api and ("POST", "/dives") in api

    def test_json_is_the_request_plus_queue_position(self, api, capsys):
        dive.request("proj", None, 1, 24, "4h", None, None, detach=True, as_json=True)
        out = json.loads(capsys.readouterr().out)
        assert out["id"] == 9 and out["ahead"] == 2

    def test_without_detach_it_still_attaches(self, api):
        dive.request("proj", None, 1, 24, "4h", None, None)
        assert ("attach",) in api

    def test_detach_with_vscode_is_refused_before_queueing(self, api):
        with pytest.raises(typer.Exit):
            dive.request("proj", None, 1, 24, "4h", None, None, vscode=True, detach=True)
        assert api == []


class TestWait:
    def test_writes_the_alias_and_names_it(self, monkeypatch, capsys):
        monkeypatch.setattr(dive, "in_container", lambda: False)
        monkeypatch.setattr(dive, "ensure_alias", lambda i: (f"lab-{i}", {"node": "arm1"}))
        dive.wait(9)
        assert "ssh lab-9" in capsys.readouterr().err

    def test_an_ended_dive_is_an_error_exit(self, monkeypatch):
        monkeypatch.setattr(dive, "in_container", lambda: False)
        monkeypatch.setattr(dive.auth, "_api", lambda *a, **k: {"ready": False, "done": True})
        monkeypatch.setattr(sshconfig, "record", lambda *a, **k: pytest.fail("no alias for an ended Dive"))
        with pytest.raises(typer.Exit) as e:
            dive.wait(9)
        assert e.value.exit_code != 0
