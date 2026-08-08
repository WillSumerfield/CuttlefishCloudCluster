"""Shell + host-detection helpers."""

import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

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
        raise ui.abort("sudo authentication failed",
                       next_step="check your password — or that you have sudo access — and try again")
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


def install_file(content: str | bytes, dest: Path, mode: str = "644",
                 owner: str | None = None, group: str | None = None) -> None:
    """Write `content` to a root-owned `dest` via a temp file + sudo install, and ALWAYS remove the
    temp — which is the entire reason this exists rather than hand-rolled copies of it.

    `install` COPIES, so the source survives every call and must be cleaned up explicitly. Two callers
    write secrets this way (setup.ensure_bench_secret's rsync credential, enroll.write_munge_key's
    munge key on an Arm) that land 0600 root at their destination but would otherwise sit
    operator-readable in /tmp forever, defeating the mode the destination was given.

    `finally`, not a trailing unlink: a failed `install` (bad path, full disk, sudo timeout) is exactly
    when the temp file is most likely to hold something worth not leaving lying around.
    """
    binary = isinstance(content, bytes)
    with tempfile.NamedTemporaryFile("wb" if binary else "w", suffix=".ccc", delete=False) as f:
        f.write(content)
        tmp = f.name
    argv = ["install", "-D", "-m", mode]
    if owner:
        argv += ["-o", owner]
    if group:
        argv += ["-g", group]
    try:
        run([*argv, tmp, str(dest)], sudo=True)
    finally:
        Path(tmp).unlink(missing_ok=True)


def hostname() -> str:
    return socket.gethostname()


def primary_ip() -> str:
    """The host's primary outbound IPv4 (the `src` of the default route). Default advertised
    address for the Headscale coordinator — override with `--address` for a public/DDNS addr."""
    out = subprocess.run(["ip", "-4", "route", "get", "1.1.1.1"],
                         check=True, capture_output=True, text=True).stdout
    return out.split("src", 1)[1].split()[0]


def local_ipv4s() -> list[str]:
    """Every globally-scoped IPv4 this host holds, in `ip addr` order.

    `primary_ip()` answers "where does traffic leave?", which is the wrong question for a coordinator
    address: a Head on both a LAN and a tailnet has two, and only the operator knows which one a
    donor's box can reach. This lets setup offer the real choice instead of guessing.
    """
    out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                         check=True, capture_output=True, text=True).stdout
    return [line.split()[3].split("/")[0] for line in out.splitlines()]


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


def gpu_devices() -> list[str]:
    """Every GPU **Device** node this box can hand to a Spawn, in Slurm's GRES index order.

    **WSL2 is permanently single, and that is not a deferral.** `/dev/dxg` is ONE device node for ALL
    of the Windows box's cards, and a device cgroup can only grant or deny that node whole — there is
    nothing in it to partition. So a WSL Arm registers `gpu:1` however many physical cards it holds,
    and no Slurm release can change that: the limit is the device node, not the scheduler.

    Sorted NUMERICALLY, not lexically. This order is the mapping from a Slurm GRES index to a device
    (see templates.render_gres_conf) and therefore from a Spawn's held index to the Power it is billed
    at — and `/dev/nvidia10` sorts before `/dev/nvidia2` as a string.
    """
    if Path("/dev/dxg").exists():
        return ["/dev/dxg"]
    # `nvidia[0-9]*` and not `nvidia*`: /dev/nvidiactl, /dev/nvidia-uvm and /dev/nvidia-caps are
    # control nodes, not cards, and a Spawn holds none of them.
    return sorted((str(p) for p in Path("/dev").glob("nvidia[0-9]*")),
                  key=lambda p: int(p.removeprefix("/dev/nvidia")))


def gpu_inventory() -> list[tuple[str, int]]:
    """(model, VRAM MiB) for every GPU nvidia-smi can see, in its index order.

    On WSL this lists every *physical* card even though `gpu_devices()` reports one — which is the
    point: `gpu_profile()` must still judge the real hardware, not the one device node fronting it.
    """
    out = subprocess.run(
        [nvidia_smi(), "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
        check=True, capture_output=True, text=True).stdout
    return [(name.strip(), int(vram)) for name, vram in
            (line.split(",") for line in out.strip().splitlines() if line.strip())]


def gpu_profile() -> tuple[str, int]:
    """The ONE (model, VRAM MiB) this box may advertise — or a refusal, if its GPUs disagree.

    A Slurm node **Feature** is per-node: a box advertises exactly one `vramN`
    (config.vram_feature), and that string is the only thing a Spawn's `--vram` can be matched
    against. A machine holding a 24 GB card and an 80 GB one cannot honestly advertise either — one
    choice starves the big jobs, the other sends them to a card that cannot hold them. So CCC refuses
    such a box outright rather than pick a side and misprice every Spawn that lands on the wrong card.

    Compared on model AND memory because they answer different questions: the model is what **Power**
    tracks, the memory is what **Eligibility** matches. A difference in either is disqualifying.
    """
    gpus = gpu_inventory()
    if not gpus:
        raise ui.abort("nvidia-smi reports no GPU on this box",
                       next_step="check the driver with `nvidia-smi`; a CCC node must have a GPU")
    if len(set(gpus)) > 1:
        found = "\n".join(f"    GPU {i}: {name} — {vram} MiB" for i, (name, vram) in enumerate(gpus))
        raise ui.abort(
            f"all GPUs in one Arm must be the same model — this box has more than one kind:\n{found}",
            next_step="a Slurm node advertises ONE VRAM size and carries ONE Weight, so a mixed box "
                      "cannot be described honestly. Leave one kind of card visible to this box "
                      "(e.g. CUDA_VISIBLE_DEVICES is not enough — remove or blacklist the others) "
                      "and re-run, or enroll the cards as separate machines")
    return gpus[0]


def gpu_vram_mb() -> int:
    """This box's GPU memory, in MiB. Becomes the node's `vramN` Slurm feature — the thing a Spawn's
    `--vram` is matched against.

    Reads EVERY card and takes the agreed value, rather than card 0's. They agree because
    `gpu_profile()` refuses the box otherwise — and reading them all is what *makes* that check, so
    this cannot silently describe a 4-card box by whichever card the driver happened to enumerate
    first.

    From nvidia-smi on the host, NOT from the Pod-side Power benchmark, even though that measures VRAM
    too: a dynamic Arm's features must be baked into its `slurmd -Z --conf` at registration, and the
    benchmark cannot run until the node is already registered. Host and Pod agree on this number
    anyway (unlike throughput, which is exactly why Power is measured and VRAM is merely read).
    """
    return gpu_profile()[1]


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


# --- the Cluster name -------------------------------------------------------
# CCC has no DNS — the mesh runs no resolver and there is no router to configure — so the **Cluster
# name** is carried by /etc/hosts on every box `setup` touches. One managed line, marked with
# config.HOSTS_MARKER, rewritten in place rather than appended to: setup is re-run routinely (a full
# teardown+setup is the normal redeploy here) and a box that accumulated one stale line per run would
# resolve the name to whichever address happened to be listed first.

def write_hosts_entry(addr: str, name: str = config.DOMAIN) -> None:
    """Point `name` at `addr` in /etc/hosts. Idempotent, and a no-op when the line is already right —
    setup writes this on every run, and rewriting an unchanged file for nothing is a root mutation
    with nothing to show for it."""
    want = f"{addr}\t{name} {config.HOSTS_MARKER}"
    current = Path("/etc/hosts").read_text().splitlines()
    if want in current:
        return
    kept = [ln for ln in current if not ln.rstrip().endswith(config.HOSTS_MARKER)]
    install_file("\n".join([*kept, want]) + "\n", Path("/etc/hosts"), "644")
    print(f"cuttle: {name} → {addr} (/etc/hosts)")


def remove_hosts_entry() -> None:
    """Drop our managed line. Every teardown, every role: the name is cuttle's own state, and a box
    that has left the cluster resolving it to a machine it no longer talks to is worse than one that
    does not resolve it at all."""
    path = Path("/etc/hosts")
    if not path.exists():
        return
    lines = [ln for ln in path.read_text().splitlines()
             if not ln.rstrip().endswith(config.HOSTS_MARKER)]
    install_file("\n".join(lines) + "\n", path, "644")


# --- trusting the CCC CA ----------------------------------------------------
# The Head is its own CA (see config.HEAD_CA_CERT). Trusting it is what makes the Dashboard load
# without "your connection is not private" — but on Linux there is no single store that does it:
#
#   * the OS store (`update-ca-certificates`) is read by curl, python, wget — and by nothing with a
#     window. This is the same store, and the same mechanism, mesh.trust_headscale_cert uses.
#   * Chromium-family browsers (Chrome, Chromium, Edge, Brave) ignore the OS store entirely and read
#     a per-user NSS database. One import covers all four; a second login account on the box does its
#     own.
#   * Firefox ignores both, but honours an enterprise policy telling it to read the OS store — so it
#     needs a file, not a package, and picks up anything installed there later for free.
#
# All three are host mutations that teardown undoes unconditionally (see teardown.untrust_ccc_ca):
# a box that has left the cluster must not keep trusting a CA whose private key is on a machine it
# no longer talks to.

def trust_ccc_ca(pem: str) -> None:
    """Install the CCC CA into this box's OS trust store. Idempotent in effect."""
    install_file(pem, config.HEAD_CA_TRUST_PATH, "644")
    run(["update-ca-certificates"], sudo=True)


def _nssdb() -> Path:
    return Path.home() / config.NSSDB_DIR


def trust_ccc_ca_browsers(pem: str) -> None:
    """Trust the CA in this user's Chromium-family NSS db and, box-wide, in Firefox.

    `-t C,,` — trusted to issue *server* certs, and nothing else. Delete-then-add rather than add
    alone: certutil happily stores a second entry under the same nickname, and after a CA reissue the
    browser would then be choosing between two.

    Firefox's `policies.json` is MERGED, not written: it is a shared file that may already carry an
    organisation's own settings, and clobbering those to add one key would be a poor trade. We record
    whether it was ours to begin with so teardown knows whether to delete the file or just our key.
    """
    db = _nssdb()
    db.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".crt", delete=False) as f:
        f.write(pem)
        tmp = f.name
    try:
        run(["certutil", "-d", f"sql:{db}", "-D", "-n", config.HEAD_CA_NICKNAME], check=False)
        run(["certutil", "-d", f"sql:{db}", "-A", "-t", "C,,",
             "-n", config.HEAD_CA_NICKNAME, "-i", tmp])
    finally:
        Path(tmp).unlink(missing_ok=True)
    _firefox_trust(True, pem)
    print(f"cuttle: trusted the CCC CA in this user's browsers ({db}) and in Firefox")


def _firefox_trust(on: bool, pem: str | None = None) -> None:
    """Install the CA into Firefox's own store via its enterprise policy, or take it back out.

    Two things have to be true at once, and each fails silently on its own:

      * the policy must be `Certificates.Install`, not `ImportEnterpriseRoots`. The latter is a
        Windows/macOS policy — on Linux it is accepted, applied, and trusts nothing.
      * the path it names must be readable by Firefox. Ubuntu ships it as a snap confined to one host
        tree, `/etc/firefox` (the `etc-firefox` system-files plug), so the copy under
        /usr/local/share/ca-certificates that every other tool reads is invisible to it. Hence
        config.FIREFOX_CA, which is inside that tree and beside the policy file itself.

    Firefox reads policies at STARTUP, so a running instance keeps warning until it fully exits — and
    "fully" means every process, not just the window.
    """
    path = config.FIREFOX_POLICIES
    doc = json.loads(path.read_text()) if path.exists() else {}
    certs = doc.setdefault("policies", {}).setdefault("Certificates", {})
    install = [p for p in certs.get("Install", []) if p != str(config.FIREFOX_CA)]
    if on:
        install_file(pem, config.FIREFOX_CA, "644")
        # Drop ImportEnterpriseRoots if it is OURS — earlier versions of this function wrote it, and a
        # box upgrading would otherwise keep a key we authored, no longer write, and know does nothing
        # here. Guarded on the marker so an admin who set it for their own CA keeps it.
        if certs.get("ccc"):
            certs.pop("ImportEnterpriseRoots", None)
        certs["Install"] = [*install, str(config.FIREFOX_CA)]
        certs["ccc"] = True
    else:
        # Only unwind what we put there. A policies.json that carried other settings keeps them, and
        # so does anyone else's entry in Install — an admin trusting their own CA through this file
        # should not lose it to a CCC teardown.
        if not certs.pop("ccc", False):
            return
        run(["rm", "-f", str(config.FIREFOX_CA)], sudo=True)
        certs.pop("ImportEnterpriseRoots", None)  # written by earlier versions of this function
        if install:
            certs["Install"] = install
        else:
            certs.pop("Install", None)
        if not certs:
            doc["policies"].pop("Certificates", None)
        if not doc.get("policies"):
            run(["rm", "-f", str(path)], sudo=True)
            return
    install_file(json.dumps(doc, indent=2) + "\n", path, "644")


def untrust_ccc_ca() -> None:
    """Reverse all three trust installs. Tolerant throughout: this runs on every teardown of every
    role, including boxes that never got as far as trusting anything."""
    if config.HEAD_CA_TRUST_PATH.exists():
        run(["rm", "-f", str(config.HEAD_CA_TRUST_PATH)], sudo=True)
        run(["update-ca-certificates"], sudo=True, check=False)
    if _nssdb().exists() and shutil.which("certutil"):
        run(["certutil", "-d", f"sql:{_nssdb()}", "-D", "-n", config.HEAD_CA_NICKNAME], check=False)
    _firefox_trust(False)


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
        raise ui.abort("couldn't read the firewall status (ufw) — can't tell whether it's enforcing",
                       next_step="check sudo works, then try `sudo ufw status` by hand and re-run")
    return r.stdout.strip().startswith("Status: active")


def allow_mesh_iface(iface: str) -> None:
    """Open inbound on the mesh interface — required on BOTH roles, not just the Head: slurmctld
    dials each slurmd on 6818, so a firewalled Arm registers and then goes DOWN minutes later.

    Interface-scoped, not port-scoped: srun allocates ephemeral ports for task I/O, and multi-node
    jobs need Arm<->Arm traffic. Grants the mesh nothing it didn't have — mesh membership already
    means full cluster trust.

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


def allow_tcp_port_range(base: int, count: int, comment: str) -> None:
    """Like `allow_tcp_port`, for a whole range at once — Tide Pool's relay ports, opened ONCE at
    `setup --head` rather than per-Spawn: a ufw rule added and removed on every submit/end would be
    firewall churn for no benefit, since the range itself is a fixed, harmless-when-idle constant."""
    if not ufw_active():
        print(f"ufw inactive or absent — leaving the host firewall alone (nothing blocks "
             f":{base}-{base + count - 1})")
        return
    run(["ufw", "allow", f"{base}:{base + count - 1}/tcp", "comment", comment], sudo=True)


def deny_tcp_port(port: int) -> None:
    """Reverse of allow_tcp_port. Tolerant of partial state (teardown), like deny_mesh_iface."""
    if not shutil.which("ufw"):
        return
    run(["ufw", "delete", "allow", f"{port}/tcp"], sudo=True, check=False)


def deny_tcp_port_range(base: int, count: int) -> None:
    """Reverse of allow_tcp_port_range. `ufw delete` matches on the rule's own text, so a teardown after
    the range constant has moved leaves the OLD range open forever — nothing removes a rule nobody names
    any more. Callers pass what the range was, not only what it is."""
    if not shutil.which("ufw"):
        return
    run(["ufw", "delete", "allow", f"{base}:{base + count - 1}/tcp"], sudo=True, check=False)
