"""Shell + host-detection helpers."""

import os
import platform
import shutil
import socket
import subprocess
import sys

from . import config, ui

# --- sudo credential priming ------------------------------------------------
# `sudo` reads its password straight from the controlling tty, so its own prompt bypasses Rich
# entirely — it lands outside whatever box is open (or on the alternate screen, which is then torn
# down), leaving an unlabelled hang. We prime the credential ONCE, up front, via `ask_password`;
# every later `sudo` then finds a live timestamp and stays silent. One process, one prompt.
#
# **Prime as the box's first row, before any step rows grow it.** Every sudo flow calls `prime_sudo`
# as the first statement inside its `ui.section`, so the single password prompt is drawn while the
# frame still holds only its title — never injected mid-run after a dozen `ui.step` rows have grown
# it (which is what made a mis-tracking terminal stack duplicate borders). The prompt is a proper
# Rich prompt and `sudo -S` is fed the password over stdin, so no raw /dev/tty prompt bypasses Rich;
# it renders as an ordinary box row. `run`/`sh` still prime lazily as a backstop, but because the
# flow primed on entry `_SUDO_PRIMED` is already set and they never prompt mid-box.
_SUDO_PRIMED = False


def sudo_available() -> bool:
    """True if `sudo` will run right now without asking — a live timestamp, or a NOPASSWD rule.
    `-n` makes sudo fail rather than prompt, so this never blocks."""
    return subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0


def prime_sudo() -> None:
    """Cache the sudo credential once, ideally before a flow opens its box (see the note above). A
    no-op when there is no one to ask (scripted/CI — let sudo prompt or fail as it always did) or
    sudo needs no password."""
    global _SUDO_PRIMED
    if _SUDO_PRIMED:
        return
    if not ui.interactive() or sudo_available():
        _SUDO_PRIMED = True
        return
    password = ui.ask_password("Your sudo password")
    # -S reads the password from stdin; -p '' suppresses sudo's own prompt (we drew ours); -v just
    # refreshes the timestamp, running no command.
    r = subprocess.run(["sudo", "-S", "-p", "", "-v"],
                       input=password + "\n", capture_output=True, text=True)
    if r.returncode != 0:
        raise ui.abort("sudo authentication failed")
    _SUDO_PRIMED = True


def run(cmd: list[str], sudo: bool = False, check: bool = True, **kw) -> subprocess.CompletedProcess:
    """Run a command. Raises on non-zero unless check=False (e.g. teardown).

    **Quiet by default.** `setup` runs dozens of these, and echoing each one buries the single line
    that matters; the enclosing `ui.step` says what is happening instead. `--verbose` shows the
    command and lets its output through, and a *failure* always prints whatever was captured — the
    output of the command that broke is the whole reason you would have wanted verbose.

    Callers that already ask for `capture_output` (they parse stdout) keep it: their kwargs win.
    """
    if sudo:
        prime_sudo()
        cmd = ["sudo", *cmd]
    if ui.VERBOSE:
        ui.say(f"[muted]$ {' '.join(cmd)}[/muted]")
    caller_captures = "capture_output" in kw or "stdout" in kw
    if not caller_captures and not ui.VERBOSE:
        kw["capture_output"] = True
        kw.setdefault("text", True)
    try:
        return subprocess.run(cmd, check=check, **kw)
    except subprocess.CalledProcessError as e:
        _dump(e)
        raise


def _dump(e: subprocess.CalledProcessError) -> None:
    """Show what a failed command actually said. Without this, hiding output would turn a legible
    apt error into a bare exit code."""
    for stream in (e.stdout, e.stderr):
        if stream:
            text = stream if isinstance(stream, str) else stream.decode("utf8", "replace")
            if text.strip():
                ui.data(text.rstrip())


def sh(script: str, sudo: bool = False) -> subprocess.CompletedProcess:
    """Run a bash snippet (for pipes/redirects). Quiet by default; see `run`."""
    if sudo:
        prime_sudo()
    prefix = "sudo " if sudo else ""
    if ui.VERBOSE:
        ui.say(f"[muted]$ {prefix}bash -c {script!r}[/muted]")
    argv = (["sudo"] if sudo else []) + ["bash", "-euc", script]
    kw = {} if ui.VERBOSE else {"capture_output": True, "text": True}
    try:
        return subprocess.run(argv, check=True, **kw)
    except subprocess.CalledProcessError as e:
        _dump(e)
        raise


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


def nvidia_smi() -> str:
    """Path to the `nvidia-smi` binary. On PATH everywhere native; on WSL the binary sits in
    /usr/lib/wsl/lib, which is on PATH only in an interactive login shell — so a non-interactive caller
    (an srun step, a service) can't find it by name and the GPU checks would fail for a reason that has
    nothing to do with the GPU. Fall back to the known WSL location. If it's on neither, return the bare
    name so the caller fails loudly (a native box with no driver — a genuine failure, as before)."""
    return (shutil.which("nvidia-smi")
            or (config.WSL_NVIDIA_SMI if is_wsl() and os.path.exists(config.WSL_NVIDIA_SMI)
                else "nvidia-smi"))


def gpu_vram_mb() -> int:
    """This box's GPU memory, in MiB. Becomes the node's `vramN` Slurm feature — the thing a Spawn's
    `--vram` is matched against.

    From nvidia-smi on the host, NOT from the Pod-side Power benchmark, even though that measures VRAM
    too: a dynamic Arm's features must be baked into its `slurmd -Z --conf` at registration, and the
    benchmark cannot run until the node is already registered. Host and Pod agree on this number
    anyway (unlike throughput, which is exactly why Power is measured and VRAM is merely read).
    """
    out = subprocess.run(
        [nvidia_smi(), "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
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
