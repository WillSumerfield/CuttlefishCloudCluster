"""slurm.arms(): each node's own GPU count, never its sinfo group's."""

from ccc_head_api import slurm


def test_grouped_nodes_keep_their_own_gpu_count(monkeypatch):
    out = {"sinfo": {"sinfo": [{"node": {"state": ["IDLE"]}, "gres": {"total": "gpu:2"},
                                "nodes": {"nodes": ["dex", "willpc"]}}]},
           "scontrol": {"nodes": [{"name": "dex", "gres": "gpu:2"}, {"name": "willpc", "gres": "gpu:1"}]}}
    monkeypatch.setattr(slurm, "_slurm_json", lambda *cmd: out[cmd[0]])
    monkeypatch.setattr(slurm, "load_registry", lambda: {})
    assert {a["name"]: (a["gpus"], a["state"]) for a in slurm.arms()} == {"dex": (2, "idle"), "willpc": (1, "idle")}
