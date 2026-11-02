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

**A Dive** (spec key `dive`) is a shell, not a script, and runs the same way on every node, the Head
included: the User's folders are pulled from the Head's per-Dive rsync modules into the job dir
(`--numeric-ids`: same id_base everywhere, so they are uid 1000 inside with no chown), the User's
snapshot image runs sshd, the main folder is mirrored back every MIRROR_S and once more at the end
(Slurm's TERM → the container stops → final mirror, inside KillWait). On an Arm sshd listens on the
mesh address (the Head relays to it); on the Head, where a relay would take the same port, on every
address — clients reach it directly.

Stdlib only, no CCC imports: it runs under the system python as root.
"""

import base64
import json
import os
import pwd
import re
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

CLUSTER_NAME_FILE = Path("/etc/ccc/cluster-name")   # config.CLUSTER_NAME_FILE
REGISTRY_PORT = 5000                                # config.REGISTRY_PORT
JOBS_DIR = Path("/var/lib/ccc/jobs")
AUTH_FILE = "/etc/ccc/registry-auth.json"
SPEC_PREFIX = "# ccc-launch-spec: "
CONTAINER_PREFIX = "ccc-job-"
SPEC_KEYS = {"image", "argv", "env", "binds", "network", "workdir", "dive"}
DIVE_KEYS = {"token", "user", "secret", "head", "main", "deps", "port"}
TIDEPOOL_HELPER = "/usr/local/sbin/ccc-tidepool"   # Head only (config.TIDEPOOL_HELPER_BIN)
ENV_CACHE = Path("/var/lib/ccc/envcache")           # config.NODE_ENV_CACHE_DIR — parent of per-Guise caches
MESH_IFACE = "ccc0"                                 # config.CCC_TS_TUN
CUTTLE_ENV = "/usr/local/lib/ccc/cuttle"            # config.CONTAINER_CUTTLE
HEAD_CA = "/usr/local/share/ca-certificates/ccc-head.crt"   # config.HEAD_CA_TRUST_PATH
CDI_SPEC = Path("/var/run/cdi/nvidia.yaml")         # config.CDI_SPEC
CDI_PATCH_BIN = "/usr/local/sbin/ccc-cdi-patch"     # config.CDI_PATCH_BIN
INNER_UID = 1000
MIRROR_S = 60
# ccc_tidepool.DENYLIST / FILTER_ARGS — never staged, never deleted on the Head by the mirror.
DIVE_FILTERS = ["--exclude=.venv", "--exclude=venv", "--exclude=__pycache__", "--exclude=.cache",
                "--exclude=node_modules", "--exclude=.mypy_cache", "--exclude=.pytest_cache",
                "--exclude=.ipynb_checkpoints", "--filter=:- .cuttleignore"]

# Fixed per-User id range: same formula on every node, so rsync -a keeps ownership across nodes.
ID_BASE = 2**31
ID_COUNT = 65536
UID_FLOOR = 60000     # config.CCC_UID — slot 0; the cluster Guise (60001) is slot 1
UID_CEIL = 61182      # config.GUISE_UID_MAX

# `<node>_slurmstepd.scope` on dex (Slurm 23.11); plain `slurmstepd.scope` on other builds.
CGROUP_RE = re.compile(r"^0::(?P<step>/.*/(?:(?P<node>[^/]+)_)?slurmstepd\.scope/job_(?P<jid>\d+)"
                       r"/step_[^/]+)(/|$)", re.M)
GRES_RE = re.compile(r"\bGRES=\S*?gpu[^(\s]*\(IDX:(?P<idx>[0-9,\-]*)\)")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DST_RE = re.compile(r"^/[A-Za-z0-9._/-]*$")
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def domain() -> str:
    """config.domain(): `ccc.<cluster name>`."""
    return f"ccc.{CLUSTER_NAME_FILE.read_text().strip()}"


def registry() -> str:
    """config.registry()."""
    return f"{domain()}:{REGISTRY_PORT}"
USER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
REL_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._ @+-]*(/[A-Za-z0-9._][A-Za-z0-9._ @+-]*)*$")
HOST_RE = re.compile(r"^[0-9a-fA-F.:]+$")
SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
TRES_MEM_RE = re.compile(r"\bTRES=\S*?\bmem=(?P<n>[\d.]+)(?P<u>[KMGT]?)")


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
    mem = TRES_MEM_RE.search(text)
    mult = {"K": 1 / 1024, "": 1, "M": 1, "G": 1024, "T": 1024 ** 2}
    return {"uid": int(uid[1]) if uid else None,
            "state": fields.get("JobState"),
            "gres_idx": parse_idx(gres["idx"]) if gres else [],
            "cpus": int(fields.get("NumCPUs", "1").split("-")[0]),
            "node": fields.get("NodeList", ""), "end": fields.get("EndTime", ""),
            "mem_mb": int(float(mem["n"]) * mult[mem["u"]]) if mem else 0}


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
    reg = registry()
    if not re.match(rf"^{re.escape(reg)}/[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{{64}}$", spec.get("image", "")):
        raise Refused(f"image must be a {reg}/…@sha256: ref")
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
    if "dive" in spec:
        validate_dive(spec["dive"])
    return spec


def _rel_ok(rel) -> bool:
    return isinstance(rel, str) and bool(REL_RE.match(rel)) and \
        not any(p in (".", "..") for p in rel.split("/"))


def validate_dive(d: dict) -> None:
    if set(d) != DIVE_KEYS:
        raise Refused(f"dive spec keys must be {sorted(DIVE_KEYS)}")
    if not TOKEN_RE.match(d["token"]) or not USER_RE.match(d["user"]) or \
            not SECRET_RE.match(d["secret"]) or not HOST_RE.match(d["head"]):
        raise Refused("bad dive token/user/secret/head")
    if not _rel_ok(d["main"]) or not isinstance(d["deps"], list) or \
            not all(_rel_ok(x) for x in d["deps"]):
        raise Refused("dive folders must be relative paths under the home")
    if not isinstance(d["port"], int) or not 1024 < d["port"] < 65536:
        raise Refused("bad dive port")


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


WSL_DXG = Path("/dev/dxg")
# Fixed root-owned paths, never PATH (this runs under sudo). WSL mounts the Windows driver's tools here.
NVIDIA_SMI_PATHS = ("/usr/bin/nvidia-smi", "/usr/lib/wsl/lib/nvidia-smi")


def nvidia_smi() -> str:
    return next((p for p in NVIDIA_SMI_PATHS if os.path.exists(p)), NVIDIA_SMI_PATHS[0])


def gpu_ids(idx: list[int]) -> list[str]:
    """GRES indices → the CDI-nameable UUID (MIG instance if the card is in MIG mode), GRES order.
    A WSL Arm (no /dev/nvidiaN, one /dev/dxg) has exactly one card: its UUID from nvidia-smi."""
    if not idx:
        return []
    if WSL_DXG.exists() and not _device_minors():
        if idx != [0]:
            raise Refused(f"a WSL Arm has one GPU, job holds GRES {idx}")
        return subprocess.run([nvidia_smi(), "--query-gpu=uuid", "--format=csv,noheader"],
                              capture_output=True, text=True, check=True).stdout.split()[:1]
    minors, uuids = _device_minors(), _uuid_by_minor()
    smi = subprocess.run([nvidia_smi(), "-L"], capture_output=True, text=True).stdout
    out = []
    for i in idx:
        u = uuids[minors[i]]
        out.append(mig_uuid(u, smi) or u)
    return out


def cdi_names(gpus: list[str], wsl: bool) -> list[str]:
    """NVIDIA's WSL-mode CDI spec names one device, `all` (= the box's one GPU); elsewhere, by UUID."""
    return (["all"] if gpus else []) if wsl else gpus


def _cdi_missing(names: list[str], spec: str) -> list[str]:
    have = set(re.findall(r"^\s*- name: \"?([^\"\s]+)\"?\s*$", spec, re.M))
    return [n for n in names if n not in have]


def ensure_cdi(names: list[str]) -> None:
    """nvidia-cdi-refresh rewrites the spec at boot and on a driver change only — not when MIG is
    switched on, so a MIG instance's UUID is unknown to Podman until something regenerates it. A
    missing name: regenerate now (and patch it for Podman 4.9, not waiting on the path unit)."""
    if not names or not _cdi_missing(names, CDI_SPEC.read_text() if CDI_SPEC.exists() else ""):
        return
    subprocess.run(["/usr/bin/systemctl", "start", "nvidia-cdi-refresh.service"], capture_output=True)
    subprocess.run([CDI_PATCH_BIN, str(CDI_SPEC)], capture_output=True)


def podman_argv(spec: dict, jid: str, base: int, step: str, gpus: list[str], jobdir: Path,
                binds: list[tuple[Path, str, bool]], cdi: list[str] | None = None) -> list[str]:
    argv = ["/usr/bin/nsenter", "--mount=/proc/1/ns/mnt", "--",
            "/usr/bin/podman", "--cgroup-manager=cgroupfs", "run", "--rm", "--init",
            "--name", f"{CONTAINER_PREFIX}{jid}", "--cgroup-parent", step,
            "--uidmap", f"0:{base}:{ID_COUNT}", "--gidmap", f"0:{base}:{ID_COUNT}",
            "--pull=missing", "--authfile", AUTH_FILE,
            "--network", "private" if spec.get("network") else "none",
            "-v", f"{jobdir}:/job", "-w", spec.get("workdir", "/job"),
            "-e", f"CCC_GPUS={','.join(gpus)}", "-e", f"CCC_JOB_ID={jid}"]
    for g in gpus if cdi is None else cdi:
        argv += ["--device", f"nvidia.com/gpu={g}"]
    for src, dst, ro in binds:
        argv += ["-v", f"{src}:{dst}" + (":ro" if ro else "")]
    for k, v in spec.get("env", {}).items():
        argv += ["-e", f"{k}={v}"]
    return argv + [spec["image"], *spec.get("argv", [])]


# --- Dives -----------------------------------------------------------------------

def on_head() -> bool:
    return Path(TIDEPOOL_HELPER).exists()


def dive_where(node: str) -> str:
    """The machine a Dive runs on, as Users name it: the cluster name on the Head, else the node."""
    return CLUSTER_NAME_FILE.read_text().strip() if on_head() else node


def _clock(end: str, now: datetime | None = None) -> str:
    """Slurm's EndTime → `14:30`, or `Oct 31 14:30` when not today."""
    try:
        t = datetime.fromisoformat(end)
    except ValueError:
        return "?"
    return t.strftime("%H:%M" if t.date() == (now or datetime.now()).date() else "%b %d %H:%M")


def smi_names(smi_list: str) -> dict[str, str]:
    """UUID → banner name from `nvidia-smi -L`; a MIG instance (what a MIG-mode card is handed out
    as) is its card's name + profile."""
    names, card = {}, ""
    for ln in smi_list.splitlines():
        if m := re.match(r"GPU \d+: (.+) \(UUID: (GPU-[^)]+)\)", ln):
            card = m[1].removeprefix("NVIDIA ")
            names[m[2]] = card
        elif m := re.match(r"\s+MIG (\S+)\s+Device\s+\d+: \(UUID: (MIG-[^)]+)\)", ln):
            names[m[2]] = f"{card} · MIG {m[1]}"
    return names


def gpu_names(uuids: list[str]) -> list[str]:
    names = smi_names(subprocess.run([nvidia_smi(), "-L"], capture_output=True, text=True).stdout)
    return [names.get(u, "GPU") for u in uuids]


def dive_banner(jid: str, where: str, idx: list[int], names: list[str], end: str, main: str,
                now: datetime | None = None) -> str:
    gpus = ", ".join(f"GPU {i} ({n})" for i, n in zip(idx, names)) or "no GPU"
    return (f"Dive {jid} on {where} · {gpus} · ends {_clock(end, now)} · "
            f"~/{main} mirrors back to your Tidepool every minute")


def write_shell_info(sysdir: Path, where: str, main: str, banner: str) -> None:
    """/etc/ccc/dive/{where,main,banner} inside: what the shell's zshrc reads for its prompt
    (`in a dive · <user>@<where>`), its start folder and its login line. Files, not `-e`: an ssh login
    shell never sees the container's environment."""
    for name, text in (("where", where), ("main", main), ("banner", banner)):
        (sysdir / name).write_text(text + "\n")
        (sysdir / name).chmod(0o644)


def module(d: dict, name: str) -> str:
    return f"rsync://d{d['token']}@{d['head']}/d{d['token']}-{name}/"


def rsync_argv(src: str, dst: str, filters: bool = True, delete: bool = False) -> list[str]:
    return ["/usr/bin/rsync", "-a", "--numeric-ids", "--timeout=120",
            *(["--delete"] if delete else []), *(DIVE_FILTERS if filters else []), src, dst]


def _mkdirs(root: Path, rel: str, owner: int) -> Path:
    """root/rel, every new component owned by the User (uid 1000 inside)."""
    p = root
    for part in rel.split("/"):
        p = p / part
        if not p.exists():
            p.mkdir(mode=0o755)
            os.chown(p, owner, owner)
    return p


def stage_in(d: dict, jobdir: Path, owner: int) -> Path:
    """Pull the bundle (host key, keys), dotfiles, dep folders, then the main folder — that first
    pull of the main module is what locks it read-only in the Tidepool (rsyncd pre-xfer exec)."""
    home = jobdir / "home"
    home.mkdir(mode=0o750, exist_ok=True)
    os.chown(home, owner, owner)
    run = lambda argv: subprocess.run(argv, check=True)   # noqa: E731
    run(rsync_argv(module(d, "sys"), f"{jobdir}/sys/", filters=False))
    # rsync -a carries the Head's root 0700 onto the dir: container root (host uid base) could not
    # open it, so sshd found no host key. Its files keep their own owners (key: container root;
    # session: the User), so 0711 lets each reach only its own.
    os.chown(jobdir / "sys", owner - INNER_UID, owner - INNER_UID)
    (jobdir / "sys").chmod(0o711)
    run(rsync_argv(module(d, "dot"), f"{home}/", filters=False))
    for i, dep in enumerate(d["deps"]):
        run(rsync_argv(module(d, f"dep{i}"), f"{_mkdirs(home, dep, owner)}/"))
    run(rsync_argv(module(d, "main"), f"{_mkdirs(home, d['main'], owner)}/"))
    return home


def mirror(d: dict, home: Path) -> bool:
    return subprocess.run(rsync_argv(f"{home / d['main']}/", module(d, "main"), delete=True)).returncode == 0


def dotfile_binds(home: Path, user: str, folders: list[str]) -> list[tuple[Path, str]]:
    """Every staged dotfile, read-only at its home path (`.config`/`.ssh`: their children only, so
    the rest of those dirs stays writable scratch). Never a staged folder (a main folder may be a
    dot-folder too)."""
    tops = {f.split("/")[0] for f in folders}
    out = []
    for p in sorted(home.iterdir()):
        if not p.name.startswith(".") or p.name in tops:
            continue
        kids = sorted(p.iterdir()) if p.name in (".config", ".ssh") and p.is_dir() else [p]
        out += [(k, f"/home/{user}/{k.relative_to(home)}") for k in kids]
    return out


def mesh_ip() -> str:
    out = subprocess.run(["/usr/sbin/ip", "-4", "-o", "addr", "show", "dev", MESH_IFACE],
                         capture_output=True, text=True).stdout
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", out)
    if not m:
        raise Refused(f"no mesh address on {MESH_IFACE}")
    return m[1]


def dive_argv(spec: dict, jid: str, base: int, step: str, cdi: list[str], gpus: list[str], jobdir: Path,
              home: Path, cache: Path, deps: list[str], addr: str) -> list[str]:
    """`addr` "" = publish sshd on every address (the Head: no relay in front of it).

    `cache` is this Guise's OWN cache subtree (run_dive), mounted alone at /ccc-cache — never the
    shared parent, so one User's build cannot read or substitute another's cached wheels (F2)."""
    d = spec["dive"]
    u = d["user"]
    argv = ["/usr/bin/nsenter", "--mount=/proc/1/ns/mnt", "--",
            # No --rm: ccc-launch copies the apt changes out after exit, then removes it (the epilog's
            # `podman rm -f` covers a killed job).
            "/usr/bin/podman", "--cgroup-manager=cgroupfs", "run", "--init",
            # Also kept in the journal (`journalctl CONTAINER_NAME=ccc-job-<id>`): the Slurm log is in
            # the job's private /tmp, gone at job end — with it, why a Dive died. Dives only: a
            # Dive's output is the User's own, and can be large.
            "--log-driver=journald",
            "--name", f"{CONTAINER_PREFIX}{jid}", "--cgroup-parent", step, "--hostname", f"dive-{jid}",
            "--uidmap", f"0:{base}:{ID_COUNT}", "--gidmap", f"0:{base}:{ID_COUNT}",
            "--pull=missing", "--authfile", AUTH_FILE,
            "--network", "private", "-p", f"{addr}:{d['port']}:22" if addr else f"{d['port']}:22",
            "-v", f"{home}:/home/{u}", "-v", f"{jobdir}/sys:/etc/ccc/dive:ro",
            "-v", f"{cache}:/ccc-cache:idmap",
            # `cuttle` inside, reaching the Head with the Dive's ambient session (sys/session)
            "-v", f"{CUTTLE_ENV}:{CUTTLE_ENV}:ro", "-v", f"{CLUSTER_NAME_FILE}:/etc/ccc/cluster-name:ro",
            "-v", f"{HEAD_CA}:/etc/ccc/ca.crt:ro",
            "--add-host", f"{domain()}:{d['head']}",
            "-e", f"CCC_GPUS={','.join(gpus)}", "-e", f"CCC_JOB_ID={jid}"]
    for dep in deps:
        argv += ["-v", f"{home / dep}:/home/{u}/{dep}:ro"]
    for src, dst in dotfile_binds(home, u, [d["main"], *deps]):
        argv += ["-v", f"{src}:{dst}:ro"]
    for g in cdi:
        argv += ["--device", f"nvidia.com/gpu={g}"]
    for k, v in spec.get("env", {}).items():
        argv += ["-e", f"{k}={v}"]
    return argv + [spec["image"], *spec.get("argv", [])]


# What a Dive's apt changes are read from, copied back to the Head for the replay into the Tidepool.
APT_BACK = ("/var/log/apt/history.log", "/var/log/dpkg.log", "/etc/apt/sources.list.d",
            "/etc/apt/keyrings", "/usr/share/keyrings")


PODMAN = ["/usr/bin/nsenter", "--mount=/proc/1/ns/mnt", "--", "/usr/bin/podman"]


def send_back(d: dict, jid: str, jobdir: Path) -> None:
    """Copy the exited container's apt log + sources out, ship them to the Head's `back` module.
    Best effort: a failure only means nothing comes back."""
    back = jobdir / "back"
    back.mkdir(mode=0o700, exist_ok=True)
    for src in APT_BACK:
        subprocess.run([*PODMAN, "cp", f"{CONTAINER_PREFIX}{jid}:{src}", str(back / src.strip("/").replace("/", "_"))],
                       capture_output=True)
    (back / "job").write_text(f"{jid}\n")
    subprocess.run(rsync_argv(f"{back}/", module(d, "back"), filters=False))


def drop_image(ref: str) -> None:
    """Nothing of a User's stays on a donor: their snapshot goes once the container has (`--rm`).
    No `-f` — another Dive still running it keeps it, and `cuttle update`'s prune gets it later."""
    subprocess.run(["/usr/bin/nsenter", "--mount=/proc/1/ns/mnt", "--", "/usr/bin/podman", "rmi", ref],
                   capture_output=True)


def run_dive(spec: dict, jid: str, job: dict, step: str, base: int) -> int:
    d = spec["dive"]
    os.environ["RSYNC_PASSWORD"] = d["secret"]
    JOBS_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    ENV_CACHE.mkdir(mode=0o755, parents=True, exist_ok=True)
    # This Guise's own cache subtree (F2): 0700 and owned by the Dive user (base+INNER_UID, which
    # idmap maps to uid 1000 inside), so no other Guise's build can read or substitute its cached
    # wheels — uv/micromamba hard-link cache hits in, trusted and unverified. Only this subtree is
    # mounted into the Dive, never the shared parent. Keyed by the Guise uid (job["uid"]).
    cache = ENV_CACHE / str(job["uid"])
    if not cache.exists():
        cache.mkdir(mode=0o700)
        os.chown(cache, base + INNER_UID, base + INNER_UID)
    jobdir = JOBS_DIR / jid
    jobdir.mkdir(mode=0o700, exist_ok=True)
    home = stage_in(d, jobdir, base + INNER_UID)
    gpus = gpu_ids(job["gres_idx"])
    wsl = WSL_DXG.exists() and not _device_minors()
    where = dive_where(job["node"])
    write_shell_info(jobdir / "sys", where, d["main"],
                     dive_banner(jid, where, job["gres_idx"], gpu_names(gpus), job["end"], d["main"]))
    cdi = cdi_names(gpus, wsl)
    ensure_cdi(cdi)
    proc = subprocess.Popen(dive_argv(spec, jid, base, step, cdi, gpus, jobdir, home, cache,
                                      d["deps"], "" if on_head() else mesh_ip()))
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: proc.send_signal(signal.SIGTERM))

    def loop():
        while not done.wait(MIRROR_S):
            mirror(d, home)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        rc = proc.wait()
    finally:
        done.set()
        t.join()
        # the last word: whatever the shell wrote before the end, inside Slurm's KillWait
        for _ in range(3):
            if mirror(d, home):
                break
        send_back(d, jid, jobdir)
        subprocess.run([*PODMAN, "rm", "-f", f"{CONTAINER_PREFIX}{jid}"], capture_output=True)
        drop_image(spec["image"])
    return rc


def _chown_tree(root: Path, uid: int, gid: int) -> None:
    """chown without following symlinks — the container wrote this tree."""
    os.lchown(root, uid, gid)
    for d, dirs, files in os.walk(root):
        for n in dirs + files:
            os.lchown(os.path.join(d, n), uid, gid)


def main(argv: list[str]) -> int:
    # sbatch --chdir=/tmp is the job's private tmpfs, gone at scancel while this still runs: a child
    # started then (the final mirror's rsync) dies on getcwd(). / outlives the job.
    os.chdir("/")
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
    if "dive" in spec:
        return run_dive(spec, jid, job, step, base)
    gid = pwd.getpwuid(caller).pw_gid

    JOBS_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    jobdir = JOBS_DIR / jid
    jobdir.mkdir(mode=0o700, exist_ok=True)
    _chown_tree(jobdir, base, base)       # container root owns /job while it runs
    try:
        gpus = gpu_ids(job["gres_idx"])
        cdi = cdi_names(gpus, WSL_DXG.exists() and not _device_minors())
        ensure_cdi(cdi)
        cmd = podman_argv(spec, jid, base, step, gpus, jobdir, resolve_binds(spec, jobdir), cdi)
        return subprocess.run(cmd).returncode
    finally:
        _chown_tree(jobdir, caller, gid)  # handed back to the job's owner


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Refused as e:
        print(f"ccc-launch: refused: {e}", file=sys.stderr)
        sys.exit(2)
