#!/usr/bin/python3 -I
"""ccc-launch — start a cluster job's container, as root, inside that job.

Installed root-owned at /usr/local/sbin/ccc-launch on every node by `cuttle setup`; the Guise group may
run it via sudo with one argument, the job id: `sudo -n ccc-launch "$SLURM_JOB_ID"`.

Why root: rootless Podman moves its container into `user@<uid>.service`, out of the Slurm job — it
escapes the GPU fence, the memory cap and the job kill (measured on dex). Rootful Podman with an
explicit `--cgroup-parent` under the job step stays inside all three.

What the caller cannot choose:
- **the job**: the caller's own cgroup must be `…/<node>_slurmstepd.scope/job_<jid>/step_<n>/…`, and
  the job's UserId must be SUDO_UID — so only a process inside that job, as its owner, gets here.
- **the spec**: read back as root from slurmctld's copy of the batch script (a `# ccc-launch-spec:`
  line the Head wrote at submit). Never argv, never env.
- **the image**: a digest ref on the Head registry only.
- **binds**: the job dir (`/job`) and paths under it, nothing else on the host.
- **GPUs**: exactly the job's allocation (scontrol `GRES=…(IDX:…)`), never the spec's say.
- **ids**: container root = an unprivileged host range fixed per job owner (`id_base`).
Never `--privileged`, never a host namespace.

What Slurm does NOT reach (measured on dex, Slurm 23.11 cgroup v2): the container's own device program
replaces the job's, so the GPU fence is the `--device` list built here from the allocation; and
scancel never signals the container. `--init` + Podman's sig-proxy pass scancel's SIGTERM in (a chance
to checkpoint); the epilog's `podman rm -f` is the kill, before the node is handed to the next job.

Stdlib only, no CCC imports: it runs under the system python as root.
"""

import base64
import json
import os
import pwd
import re
import subprocess
import sys
from pathlib import Path

REGISTRY = "ccc.glados:5000"
JOBS_DIR = Path("/var/lib/ccc/jobs")
AUTH_FILE = "/etc/ccc/registry-auth.json"
SPEC_PREFIX = "# ccc-launch-spec: "
CONTAINER_PREFIX = "ccc-job-"
SPEC_KEYS = {"image", "argv", "env", "binds", "network", "workdir"}

# Fixed per-User id range: same formula on every node, so rsync -a keeps ownership across nodes.
ID_BASE = 2**31
ID_COUNT = 65536
UID_FLOOR = 60000     # config.CCC_UID — slot 0; the cluster Guise (60001) is slot 1
UID_CEIL = 61182      # config.GUISE_UID_MAX

IMAGE_RE = re.compile(rf"^{re.escape(REGISTRY)}/[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{{64}}$")
# `<node>_slurmstepd.scope` on dex (Slurm 23.11); plain `slurmstepd.scope` on other builds.
CGROUP_RE = re.compile(r"^0::(?P<step>/.*/(?:(?P<node>[^/]+)_)?slurmstepd\.scope/job_(?P<jid>\d+)"
                       r"/step_[^/]+)(/|$)", re.M)
GRES_RE = re.compile(r"\bGRES=\S*?gpu[^(\s]*\(IDX:(?P<idx>[0-9,\-]*)\)")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DST_RE = re.compile(r"^/[A-Za-z0-9._/-]*$")


class Refused(Exception):
    pass


def id_base(uid: int) -> int:
    if not UID_FLOOR <= uid <= UID_CEIL:
        raise Refused(f"uid {uid} is not a CCC job account")
    return ID_BASE + (uid - UID_FLOOR) * ID_COUNT


def step_cgroup(proc_cgroup: str, jid: str) -> str:
    """The step cgroup path from /proc/self/cgroup, if the caller is inside job `jid`."""
    m = CGROUP_RE.search(proc_cgroup)
    if not m or m["jid"] != jid:
        raise Refused(f"not running inside job {jid}")
    return m["step"]


def parse_job(text: str) -> dict:
    """`scontrol show job -d -o <jid>` → the fields ccc-launch checks."""
    fields = dict(t.split("=", 1) for t in text.split() if "=" in t)
    uid = re.search(r"\((\d+)\)", fields.get("UserId", ""))
    gres = GRES_RE.search(text)
    return {"uid": int(uid[1]) if uid else None,
            "state": fields.get("JobState"),
            "gres_idx": parse_idx(gres["idx"]) if gres else []}


def parse_idx(spec: str) -> list[int]:
    out: list[int] = []
    for part in filter(None, spec.split(",")):
        lo, _, hi = part.partition("-")
        out += range(int(lo), int(hi or lo) + 1)
    return out


def read_spec(script: str) -> dict:
    lines = [ln[len(SPEC_PREFIX):] for ln in script.splitlines() if ln.startswith(SPEC_PREFIX)]
    if len(lines) != 1:
        raise Refused("the job's batch script carries no single ccc-launch spec")
    return json.loads(base64.b64decode(lines[0]))


def validate_spec(spec: dict) -> dict:
    if extra := set(spec) - SPEC_KEYS:
        raise Refused(f"unknown spec keys {sorted(extra)}")
    if not IMAGE_RE.match(spec.get("image", "")):
        raise Refused(f"image must be a {REGISTRY}/…@sha256: ref")
    argv = spec.get("argv", [])
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise Refused("argv must be a list of strings")
    for k, v in spec.get("env", {}).items():
        if not ENV_KEY_RE.match(k) or not isinstance(v, str):
            raise Refused(f"bad env entry {k!r}")
    for b in spec.get("binds", []):
        src, dst = b.get("src", ""), b.get("dst", "")
        if src.startswith("/") or ".." in Path(src).parts or not DST_RE.match(dst) or ".." in dst:
            raise Refused(f"bind {src!r}:{dst!r} must be a relative path under the job dir")
    if not DST_RE.match(spec.get("workdir", "/job")):
        raise Refused("bad workdir")
    return spec


def resolve_binds(spec: dict, jobdir: Path) -> list[tuple[Path, str, bool]]:
    """Every bind's real host path, refused unless it stays under `jobdir` after symlinks."""
    out = []
    for b in spec.get("binds", []):
        src = (jobdir / b["src"]).resolve()
        if src != jobdir and jobdir not in src.parents:
            raise Refused(f"bind {b['src']!r} resolves outside the job dir")
        out.append((src, b["dst"], bool(b.get("ro", False))))
    return out


def _device_minors() -> list[int]:
    """This node's GPUs in gres.conf order (the order a GRES index indexes into)."""
    return sorted(int(p.name[6:]) for p in Path("/dev").glob("nvidia[0-9]*") if p.name[6:].isdigit())


def _uuid_by_minor() -> dict[int, str]:
    out = {}
    for info in Path("/proc/driver/nvidia/gpus").glob("*/information"):
        f = dict(ln.split(":", 1) for ln in info.read_text().splitlines() if ":" in ln)
        out[int(f["Device Minor"].strip())] = f["GPU UUID"].strip()
    return out


def mig_uuid(gpu_uuid: str, smi_list: str) -> str | None:
    """The first MIG device under `gpu_uuid` in `nvidia-smi -L` output (the prolog keeps one
    full-size instance as the resting state), or None for a card not in MIG mode."""
    cur = False
    for ln in smi_list.splitlines():
        if ln.startswith("GPU "):
            cur = f"UUID: {gpu_uuid})" in ln
        elif cur and "MIG" in ln:
            return re.search(r"UUID: (MIG-[^)]+)\)", ln)[1]
    return None


def gpu_ids(idx: list[int]) -> list[str]:
    """GRES indices → the CDI-nameable UUID (MIG instance if the card is in MIG mode), GRES order."""
    if not idx:
        return []
    minors, uuids = _device_minors(), _uuid_by_minor()
    smi = subprocess.run(["/usr/bin/nvidia-smi", "-L"], capture_output=True, text=True).stdout
    out = []
    for i in idx:
        u = uuids[minors[i]]
        out.append(mig_uuid(u, smi) or u)
    return out


def podman_argv(spec: dict, jid: str, base: int, step: str, gpus: list[str], jobdir: Path,
                binds: list[tuple[Path, str, bool]]) -> list[str]:
    argv = ["/usr/bin/nsenter", "--mount=/proc/1/ns/mnt", "--",
            "/usr/bin/podman", "--cgroup-manager=cgroupfs", "run", "--rm", "--init",
            "--name", f"{CONTAINER_PREFIX}{jid}", "--cgroup-parent", step,
            "--uidmap", f"0:{base}:{ID_COUNT}", "--gidmap", f"0:{base}:{ID_COUNT}",
            "--pull=missing", "--authfile", AUTH_FILE,
            "--network", "private" if spec.get("network") else "none",
            "-v", f"{jobdir}:/job", "-w", spec.get("workdir", "/job"),
            "-e", f"CCC_GPUS={','.join(gpus)}", "-e", f"CCC_JOB_ID={jid}"]
    for g in gpus:
        argv += ["--device", f"nvidia.com/gpu={g}"]
    for src, dst, ro in binds:
        argv += ["-v", f"{src}:{dst}" + (":ro" if ro else "")]
    for k, v in spec.get("env", {}).items():
        argv += ["-e", f"{k}={v}"]
    return argv + [spec["image"], *spec.get("argv", [])]


def _chown_tree(root: Path, uid: int, gid: int) -> None:
    """chown without following symlinks — the container wrote this tree."""
    os.lchown(root, uid, gid)
    for d, dirs, files in os.walk(root):
        for n in dirs + files:
            os.lchown(os.path.join(d, n), uid, gid)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].isdigit():
        raise Refused("usage: sudo ccc-launch <jobid>")
    jid = argv[1]
    caller = int(os.environ.get("SUDO_UID", "-1"))
    step = step_cgroup(Path("/proc/self/cgroup").read_text(), jid)
    job = parse_job(subprocess.run(["/usr/bin/scontrol", "show", "job", "-d", "-o", jid],
                                   capture_output=True, text=True, check=True).stdout)
    if job["uid"] != caller:
        raise Refused(f"job {jid} is not yours")
    if job["state"] != "RUNNING":
        raise Refused(f"job {jid} is {job['state']}, not RUNNING")
    script = subprocess.run(["/usr/bin/scontrol", "write", "batch_script", jid, "-"],
                            capture_output=True, text=True, check=True).stdout
    spec = validate_spec(read_spec(script))
    base = id_base(caller)
    gid = pwd.getpwuid(caller).pw_gid

    JOBS_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    jobdir = JOBS_DIR / jid
    jobdir.mkdir(mode=0o700, exist_ok=True)
    _chown_tree(jobdir, base, base)       # container root owns /job while it runs
    try:
        cmd = podman_argv(spec, jid, base, step, gpu_ids(job["gres_idx"]), jobdir,
                          resolve_binds(spec, jobdir))
        return subprocess.run(cmd).returncode
    finally:
        _chown_tree(jobdir, caller, gid)  # handed back to the job's owner


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Refused as e:
        print(f"ccc-launch: refused: {e}", file=sys.stderr)
        sys.exit(2)
