"""ccc-launch: the root launcher's refusals and the podman command it builds."""

import base64
import json
from pathlib import Path

import pytest

from cuttle import ccc_launch as L


@pytest.fixture(autouse=True)
def _stay_put(monkeypatch):
    """main() chdirs to / first; not the test process's cwd."""
    monkeypatch.setattr(L.os, "chdir", lambda p: None)

DIGEST = "ccc.lab:5000/ccc/bench@sha256:" + "a" * 64
CGROUP = "0::/system.slice/dex_slurmstepd.scope/job_42/step_batch/user/task_0\n"


def _script(spec: dict) -> str:
    return ("#!/bin/bash\n#SBATCH --job-name=x\n"
            f"{L.SPEC_PREFIX}{base64.b64encode(json.dumps(spec).encode()).decode()}\n"
            "sudo -n ccc-launch $SLURM_JOB_ID\n")


def test_id_base_fixed_per_uid():
    assert L.id_base(60000) == 2**31
    assert L.id_base(60001) == 2**31 + 65536
    assert L.id_base(61182) + 65536 <= 2**32


@pytest.mark.parametrize("uid", [0, 1000, 59999, 61183])
def test_id_base_refuses_non_ccc_uid(uid):
    with pytest.raises(L.Refused):
        L.id_base(uid)


def test_step_cgroup():
    assert L.step_cgroup(CGROUP, "42") == "/system.slice/dex_slurmstepd.scope/job_42/step_batch"
    plain = "0::/system.slice/slurmstepd.scope/job_7/step_0/user/task_0\n"
    assert L.step_cgroup(plain, "7") == "/system.slice/slurmstepd.scope/job_7/step_0"


@pytest.mark.parametrize("cg,jid", [
    (CGROUP, "43"),                                       # someone else's job
    ("0::/user.slice/user-1000.slice/session-3.scope\n", "42"),  # not in a job at all
    ("0::/system.slice/dex_slurmstepd.scope/job_420/step_0/user\n", "42"),  # prefix collision
])
def test_step_cgroup_refuses(cg, jid):
    with pytest.raises(L.Refused):
        L.step_cgroup(cg, jid)


def test_parse_job():
    text = ("JobId=42 JobName=ccc-bench UserId=ccc-u60001(60001) GroupId=ccc-u60001(60001) "
            "JobState=RUNNING Reason=None EndTime=2026-10-29T14:30:00 NodeList=dex JOB_GRES=gpu:2 "
            "Nodes=dex CPU_IDs=0-1 Mem=0 GRES=gpu:2(IDX:0-1) TresPerNode=gres/gpu:2")
    assert L.parse_job(text) == {"uid": 60001, "state": "RUNNING", "gres_idx": [0, 1], "cpus": 1,
                                 "mem_mb": 0, "node": "dex", "end": "2026-10-29T14:30:00"}


def test_parse_job_cpus_and_memory():
    text = ("UserId=a(60002) JobState=RUNNING NumNodes=1 NumCPUs=8 NumTasks=1 "
            "TRES=cpu=8,mem=32G,node=1,billing=8,gres/gpu=1 GRES=gpu:1(IDX:0)")
    j = L.parse_job(text)
    assert (j["cpus"], j["mem_mb"]) == (8, 32768)
    assert L.parse_job("UserId=a(60002) NumCPUs=2 TRES=cpu=2,mem=1500M")["mem_mb"] == 1500


def test_parse_job_typed_gres_and_no_gpu():
    assert L.parse_job("UserId=a(60002) JobState=RUNNING GRES=gpu:rtx:1(IDX:3)")["gres_idx"] == [3]
    assert L.parse_job("UserId=a(60002) JobState=RUNNING")["gres_idx"] == []


def test_parse_idx():
    assert L.parse_idx("0,2-4,7") == [0, 2, 3, 4, 7]


def test_read_spec_roundtrip():
    spec = {"image": DIGEST, "argv": ["python", "x.py"]}
    assert L.read_spec(_script(spec)) == spec


def test_read_spec_refuses_missing_or_double():
    with pytest.raises(L.Refused):
        L.read_spec("#!/bin/bash\necho hi\n")
    s = _script({"image": DIGEST})
    with pytest.raises(L.Refused):
        L.read_spec(s + s)


@pytest.mark.parametrize("spec", [
    {"image": "docker.io/library/ubuntu@sha256:" + "a" * 64},     # not the Head registry
    {"image": "ccc.lab:5000/ccc/base:latest"},                 # tag, not digest
    {"image": DIGEST, "privileged": True},                        # unknown key
    {"image": DIGEST, "binds": [{"src": "/etc", "dst": "/x"}]},   # absolute host path
    {"image": DIGEST, "binds": [{"src": "../x", "dst": "/x"}]},   # escapes job dir
    {"image": DIGEST, "binds": [{"src": "a", "dst": "/x:rw,z"}]}, # option injection
    {"image": DIGEST, "env": {"A B": "1"}},
    {"image": DIGEST, "argv": "python x.py"},
])
def test_validate_spec_refuses(spec):
    with pytest.raises(L.Refused):
        L.validate_spec(spec)


def test_resolve_binds_refuses_symlink_escape(tmp_path):
    (tmp_path / "out").symlink_to("/etc")
    with pytest.raises(L.Refused):
        L.resolve_binds({"binds": [{"src": "out", "dst": "/x"}]}, tmp_path)
    (tmp_path / "data").mkdir()
    assert L.resolve_binds({"binds": [{"src": "data", "dst": "/d", "ro": True}]}, tmp_path) == \
        [(tmp_path / "data", "/d", True)]


@pytest.mark.parametrize("feats", [
    "vlearn",                 # not a list
    ["Vlearn"],               # not a lowercase name
    ["../etc"],               # path, not a name
    [""],
    [1],
])
def test_validate_spec_refuses_bad_features(feats):
    with pytest.raises(L.Refused):
        L.validate_spec({"image": DIGEST, "features": feats})


def test_validate_spec_allows_feature_names():
    assert L.validate_spec({"image": DIGEST, "features": ["vlearn"]})["features"] == ["vlearn"]


def _descriptor(tmp_path, monkeypatch, name, desc):
    monkeypatch.setattr(L, "FEATURE_STATE", tmp_path)
    root = tmp_path / name
    root.mkdir(parents=True)
    (root / L.FEATURE_DESC).write_text(json.dumps(desc))
    return root


def test_feature_binds_env_applies_the_descriptor(tmp_path, monkeypatch):
    root = _descriptor(tmp_path, monkeypatch, "vlearn", {
        "binds": [{"src": str(tmp_path / "vlearn/cp311"), "dst": "/ccc/feature/vlearn/cp311", "ro": True},
                  {"src": str(tmp_path / "vlearn/install"), "dst": "/ccc/feature/vlearn/install"}],
        "env": {"VL_X": "1"}})
    (root / "cp311").mkdir()
    (root / "install").mkdir()
    binds, env = L.feature_binds_env(["vlearn"])
    assert (root / "cp311", "/ccc/feature/vlearn/cp311", True) in binds
    assert (root / "install", "/ccc/feature/vlearn/install", True) in binds   # ro defaults True
    assert env == {"VL_X": "1"}


def test_feature_binds_env_refuses_a_missing_descriptor(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "FEATURE_STATE", tmp_path)
    with pytest.raises(L.Refused):
        L.feature_binds_env(["vlearn"])


@pytest.mark.parametrize("desc", [
    {"binds": [{"src": "/etc/passwd", "dst": "/ccc/x"}]},                  # outside the state tree
    {"binds": [{"src": "SELF/licence.key", "dst": "/ccc/x"}]},            # the Licence key
    {"binds": [{"src": "SELF/ok", "dst": "/job/x"}]},                     # dst not under /ccc
    {"binds": [{"src": "SELF/ok", "dst": "/ccc/../etc"}]},                # dst escape
    {"env": {"A B": "1"}},                                                 # bad env key
])
def test_feature_binds_env_fences_the_descriptor(tmp_path, monkeypatch, desc):
    root = _descriptor(tmp_path, monkeypatch, "vlearn",
                       json.loads(json.dumps(desc).replace("SELF", str(tmp_path / "vlearn"))))
    (root / "ok").mkdir()
    (root / "licence.key").write_text("secret")
    with pytest.raises(L.Refused):
        L.feature_binds_env(["vlearn"])


SMI = """\
GPU 0: NVIDIA RTX PRO 6000 (UUID: GPU-aaa)
GPU 1: NVIDIA RTX PRO 6000 (UUID: GPU-bbb)
  MIG 7g.96gb     Device  0: (UUID: MIG-ccc)
"""


def test_mig_uuid():
    assert L.mig_uuid("GPU-aaa", SMI) is None
    assert L.mig_uuid("GPU-bbb", SMI) == "MIG-ccc"


def test_podman_argv():
    spec = {"image": DIGEST, "argv": ["python", "/opt/ccc/bench.py"], "env": {"A": "1"}}
    argv = L.podman_argv(spec, "42", 2**31 + 65536, "/s/job_42/step_0", ["GPU-aaa"],
                         Path("/var/lib/ccc/jobs/42"), [(Path("/var/lib/ccc/jobs/42/d"), "/d", True)])
    s = " ".join(argv)
    assert argv[:3] == ["/usr/bin/nsenter", "--mount=/proc/1/ns/mnt", "--"]
    assert "--init" in argv     # PID 1 that passes scancel's SIGTERM to the job
    assert "--cgroup-manager=cgroupfs" in argv and "--cgroup-parent /s/job_42/step_0" in s
    assert "--uidmap 0:2147549184:65536" in s and "--gidmap 0:2147549184:65536" in s
    assert "--network none" in s
    assert "--device nvidia.com/gpu=GPU-aaa" in s
    assert "-v /var/lib/ccc/jobs/42:/job" in s and "-v /var/lib/ccc/jobs/42/d:/d:ro" in s
    assert "-e CCC_GPUS=GPU-aaa" in s and "-e A=1" in s
    assert argv[-3:] == [DIGEST, "python", "/opt/ccc/bench.py"]
    assert not any(a.startswith(("--privileged", "--pid=host", "--ipc=host", "--userns=host"))
                   for a in argv)


def test_main_refuses_other_users_job(monkeypatch):
    monkeypatch.setenv("SUDO_UID", "60002")
    monkeypatch.setattr(Path, "read_text", lambda self: CGROUP)

    class R:
        stdout = "UserId=ccc-u60001(60001) JobState=RUNNING"
    monkeypatch.setattr(L.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(L.Refused, match="not yours"):
        L.main(["ccc-launch", "42"])


@pytest.mark.parametrize("argv", [["ccc-launch"], ["ccc-launch", "42;rm"], ["ccc-launch", "1", "2"]])
def test_main_refuses_bad_argv(argv):
    with pytest.raises(L.Refused):
        L.main(argv)
