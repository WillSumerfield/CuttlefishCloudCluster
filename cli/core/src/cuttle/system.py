"""Shell + host-detection helpers."""

import platform
import shutil
import socket
import subprocess
import sys


def run(cmd: list[str], sudo: bool = False, check: bool = True, **kw) -> subprocess.CompletedProcess:
    """Run a command, echoing it. Raises on non-zero unless check=False (e.g. teardown).

    The echo goes to stderr: it is progress noise, so commands whose *stdout is data* (`cuttle
    token`) stay pipeable.
    """
    if sudo:
        cmd = ["sudo", *cmd]
    print(f"$ {' '.join(cmd)}", file=sys.stderr)
    return subprocess.run(cmd, check=check, **kw)


def sh(script: str, sudo: bool = False) -> subprocess.CompletedProcess:
    """Run a bash snippet (for pipes/redirects). Echoes it (to stderr; see `run`)."""
    prefix = "sudo " if sudo else ""
    print(f"$ {prefix}bash -c {script!r}", file=sys.stderr)
    argv = (["sudo"] if sudo else []) + ["bash", "-euc", script]
    return subprocess.run(argv, check=True)


def hostname() -> str:
    return socket.gethostname()


def primary_ip() -> str:
    """The host's primary outbound IPv4 (the `src` of the default route). Default advertised
    address for the Headscale coordinator — override with `--address` for a public/DDNS addr."""
    out = subprocess.run(["ip", "-4", "route", "get", "1.1.1.1"],
                         check=True, capture_output=True, text=True).stdout
    return out.split("src", 1)[1].split()[0]


def cpu_count() -> int:
    import os

    return os.cpu_count() or 1


def real_memory_mb() -> int:
    """MemTotal in MB, minus a small margin so Slurm never sees config > detected."""
    with open("/proc/meminfo") as f:
        kb = int(next(l for l in f if l.startswith("MemTotal")).split()[1])
    return kb // 1024 - 1024


def gpu_vram_mb() -> int:
    """This box's GPU memory, in MiB. Becomes the node's `vramN` Slurm feature — the thing a Spawn's
    `--vram` is matched against.

    From nvidia-smi on the host, NOT from the Pod-side Power benchmark, even though that measures VRAM
    too: a dynamic Arm's features must be baked into its `slurmd -Z --conf` at registration, and the
    benchmark cannot run until the node is already registered. Host and Pod agree on this number
    anyway (unlike throughput, which is exactly why Power is measured and VRAM is merely read).
    """
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
        check=True, capture_output=True, text=True).stdout
    return int(out.split("\n")[0].strip())


def installed_packages() -> set[str]:
    """Set of currently-installed dpkg package names."""
    out = subprocess.run(
        ["dpkg-query", "-W", "-f=${Package}\n"], check=True, capture_output=True, text=True
    ).stdout
    return set(out.split())


def cpu_topology() -> tuple[int, int, int]:
    """(sockets, cores_per_socket, threads_per_core) from lscpu — Slurm derives CPUs from these."""
    out = subprocess.run(["lscpu"], check=True, capture_output=True, text=True).stdout
    vals = {}
    for line in out.splitlines():
        for key, field in (("Socket(s):", "sockets"),
                           ("Core(s) per socket:", "cores"),
                           ("Thread(s) per core:", "threads")):
            if line.strip().startswith(key):
                vals[field] = int(line.split(":")[1])
    return vals["sockets"], vals["cores"], vals["threads"]


def os_release() -> dict[str, str]:
    d = {}
    with open("/etc/os-release") as f:
        for line in f:
            if "=" in line:
                k, v = line.rstrip().split("=", 1)
                d[k] = v.strip('"')
    return d


def is_native_linux_x86() -> bool:
    return platform.system() == "Linux" and platform.machine() == "x86_64"


def is_wsl() -> bool:
    """WSL2 satisfies is_native_linux_x86() completely, so that check cannot see it. The kernel
    release is the giveaway: `...-microsoft-standard-WSL2`."""
    with open("/proc/sys/kernel/osrelease") as f:
        return "microsoft" in f.read().lower()


# --- firewall ---------------------------------------------------------------

def ufw_active() -> bool:
    """True only if ufw is installed AND enforcing. Absent/inactive is not a problem to solve.

    Raises if `ufw status` itself fails (e.g. sudo unavailable): an unreadable firewall must never
    be reported as an absent one, or setup silently skips the mesh rule and says it wasn't needed.
    """
    if not shutil.which("ufw"):
        return False
    r = run(["ufw", "status"], sudo=True, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"`ufw status` failed (rc={r.returncode}) — cannot tell if it is active")
    return r.stdout.strip().startswith("Status: active")


def allow_mesh_iface(iface: str) -> None:
    """Open inbound on the mesh interface — required on BOTH roles, not just the Head: slurmctld
    dials each slurmd on 6818, so a firewalled Arm registers and then goes DOWN minutes later.

    Interface-scoped, not port-scoped: srun allocates ephemeral ports for task I/O, and multi-node
    jobs need Arm<->Arm traffic. Grants the mesh nothing it didn't have — mesh membership already
    means full cluster trust (docs/TODO.md).

    Only if ufw is already enforcing. *Enabling* a default-deny firewall on a donor's box is exactly
    the host-global mutation the isolated tailscaled exists to avoid.
    """
    if not ufw_active():
        print(f"ufw inactive or absent — leaving the host firewall alone (nothing blocks {iface})")
        return
    run(["ufw", "allow", "in", "on", iface, "comment", "ccc mesh (slurm + head api)"], sudo=True)


def deny_mesh_iface(iface: str) -> None:
    """Reverse of allow_mesh_iface. Same rule spec, so ufw matches and removes the one we added.

    Doesn't consult ufw_active(): teardown must tolerate partial state, and a `delete` of a rule that
    was never added is a harmless no-op.
    """
    if not shutil.which("ufw"):
        return
    run(["ufw", "delete", "allow", "in", "on", iface], sudo=True, check=False)


def allow_tcp_port(port: int, comment: str) -> None:
    """Open one inbound TCP port on all interfaces — for the dashboard, which unlike Slurm/the mesh
    must be reachable from the LAN, not just `ccc0`. Port-scoped (not interface-scoped) precisely
    because the point is to admit the LAN. Only if ufw is already enforcing — same reason as
    allow_mesh_iface: setup never *enables* a firewall on a box that had none."""
    if not ufw_active():
        print(f"ufw inactive or absent — leaving the host firewall alone (nothing blocks :{port})")
        return
    run(["ufw", "allow", f"{port}/tcp", "comment", comment], sudo=True)


def deny_tcp_port(port: int) -> None:
    """Reverse of allow_tcp_port. Tolerant of partial state (teardown), like deny_mesh_iface."""
    if not shutil.which("ufw"):
        return
    run(["ufw", "delete", "allow", f"{port}/tcp"], sudo=True, check=False)
