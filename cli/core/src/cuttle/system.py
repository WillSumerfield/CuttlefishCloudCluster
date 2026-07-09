"""Shell + host-detection helpers."""

import platform
import socket
import subprocess


def run(cmd: list[str], sudo: bool = False, check: bool = True, **kw) -> subprocess.CompletedProcess:
    """Run a command, echoing it. Raises on non-zero unless check=False (e.g. teardown)."""
    if sudo:
        cmd = ["sudo", *cmd]
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, **kw)


def sh(script: str, sudo: bool = False) -> subprocess.CompletedProcess:
    """Run a bash snippet (for pipes/redirects). Echoes it."""
    prefix = "sudo " if sudo else ""
    print(f"$ {prefix}bash -c {script!r}")
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
