"""The OS around a guest install — Windows under WSL2, ChromeOS under Crostini.

`setup` teaches *this box* the **Cluster name** and the cluster's CA — a client and an Arm alike. On a
guest that is not where the browser is: the browser its owner actually uses runs on the OS hosting the
virtual machine, which has neither, and which for this purpose is a separate machine (CONTEXT.md,
**Cluster name**). Everything here is about that OS.

The two WSL roles are short of different things and take the same fix. A **client** cannot open the
Dashboard by name at all until Windows is given it. An **Arm** already reaches the Dashboard over the
localhost forward `setup --arm` installs, so what it gains is the certificate warning going away — and,
both roles equally, a name that stops disappearing from `/etc/hosts` every time Windows restarts, which
is WSL rebuilding that file from the Windows one.

**Windows can be given both, and only by its owner.** Writing `hosts` and the machine `Root` store
both need Administrator, which `sudo` in here is not — and the elevated shot cannot be fired from
setup either, since a UAC-elevated process famously cannot read back through `\\\\wsl$`. So we generate
a script into their Downloads, carrying this cluster's address and CA, and say what to do with it.
`-Remove` undoes exactly what it did, which is the only way a teardown in here can reach Windows at
all.

**ChromeOS can be given neither name nor trust automatically.** It has no user-writable hosts file at
any privilege, so a Chromebook's Dashboard is reached by *address* (`dashboard.url`), and the CA is
dropped where the Files app can see it for a manual import.

Nothing here is load-bearing. Every entry point is a no-op off a guest, none of them raise, and a
client whose owner ignores all of it still works: `cuttle` resolves the name inside the guest, and the
Dashboard opens at the Head's address behind a certificate warning — the same one every device CCC was
never installed on gets.
"""

import base64
import hashlib
import socket
import subprocess
from pathlib import Path

from . import config, system, ui


def outside_browser_has_name() -> bool:
    """Whether the browser this box's owner will actually open resolves the **Cluster name**.

    True off a guest, where that browser is on this box and setup already gave it the name. What
    `dashboard.url` asks before handing over a URL: a name the browser cannot resolve is worse than an
    address, because it fails identically to a Head that is down.
    """
    if system.is_wsl():
        return windows_holds_name()
    if system.is_crostini():
        return False
    return True


def _ca_pem() -> str | None:
    """The cluster's CA as this box holds it — the OS trust store copy, which is the one every role
    writes (`system.trust_ccc_ca`) and the one teardown removes."""
    try:
        return config.HEAD_CA_TRUST_PATH.read_text()
    except OSError:
        return None


def _address() -> str:
    """The address the **Cluster name** means here. A client's Head is on the LAN, so the same answer
    serves the host OS — which is what makes one hosts line enough for both sides."""
    try:
        return socket.gethostbyname(config.DOMAIN)
    except OSError:
        return ""


# --- Windows, seen from WSL2 -------------------------------------------------
# Interop only, and always tolerantly: a client whose Windows side cannot be reached is still a working
# client, so every failure here degrades to "the browser gets an address" rather than to an error.

def _win(argv: list[str]) -> subprocess.CompletedProcess | None:
    """Run a Windows command from inside WSL. None when it could not run at all.

    `cwd` is a Windows-visible path because `cmd.exe` warns and rehomes itself to C:\\Windows when its
    working directory is a WSL one — harmless, but it writes that warning to stderr on every call.
    """
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=30,
                              cwd="/mnt/c" if Path("/mnt/c").is_dir() else None)
    except (OSError, subprocess.SubprocessError):
        return None


def _windows_dir(var: str) -> Path | None:
    """A Windows environment variable (`%USERPROFILE%`) as a directory on this side of the mount.

    Asked of `cmd.exe` rather than assumed: the system drive is not guaranteed to be C:, and a
    redirected profile is ordinary on a managed machine. `cmd` echoes `%FOO%` back literally when FOO
    is unset, which is the one answer that must not be treated as a path.
    """
    r = _win(["cmd.exe", "/c", f"echo %{var}%"])
    win = r.stdout.strip() if r and r.returncode == 0 else ""
    if not win or win.startswith("%"):
        return None
    p = _win(["wslpath", "-u", win])
    if not p or p.returncode != 0:
        return None
    path = Path(p.stdout.strip())
    return path if path.is_dir() else None


def _windows_path(path: Path) -> str:
    """The same file spelled the way the instructions have to spell it: an elevated PowerShell is
    given a Windows path, never a /mnt one."""
    r = _win(["wslpath", "-w", str(path)])
    return r.stdout.strip() if r and r.returncode == 0 else str(path)


def windows_hosts() -> Path | None:
    root = _windows_dir("SystemRoot")
    p = root / "System32/drivers/etc/hosts" if root else None
    return p if p and p.exists() else None


def _windows_downloads() -> Path | None:
    """Where the artifact goes. Downloads is where people look for a file they were just told about;
    the profile root is the fallback, since Downloads can be redirected out from under it."""
    profile = _windows_dir("USERPROFILE")
    if not profile:
        return None
    return profile / "Downloads" if (profile / "Downloads").is_dir() else profile


def windows_holds_name() -> bool:
    """Does Windows resolve the **Cluster name**? Reading that file needs no privilege at all — only
    writing it does — so this is checked rather than assumed, the same way `dashboard.url` checks that
    the WSL forward's unit is really there before trusting it."""
    path = windows_hosts()
    if not path:
        return False
    try:
        return any(system.claims_name(ln) for ln in path.read_text(errors="replace").splitlines())
    except OSError:
        return False


def _thumbprint(pem: str) -> str:
    """SHA-1 over the certificate's DER — the id `certutil` matches a store entry on.

    Identity, not mere presence: an `--all` teardown and a redeploy mint a NEW CA, and the old one
    stays trusted on Windows forever. Asking "is *this* CA there" is the only question whose answer
    changes when that happens.
    """
    body = "".join(ln for ln in pem.splitlines() if "-----" not in ln)
    return hashlib.sha1(base64.b64decode(body)).hexdigest().upper()


def windows_holds_ca() -> bool:
    pem = _ca_pem()
    if not pem:
        return False
    # `certutil.exe`, spelled out: a client also has NSS's `certutil` on PATH (libnss3-tools, installed
    # by system.ensure_certutil), and it would answer confidently about an entirely different store.
    r = _win(["certutil.exe", "-store", "Root", _thumbprint(pem)])
    return bool(r) and r.returncode == 0


def _script(addr: str) -> str:
    """The script the owner runs from Windows, with this cluster's address baked in.

    It is what we would otherwise ask them to type, so it is left readable — and it is idempotent and
    reversible for the same reason `write_hosts_entry` is: setup is re-run routinely here, and a box
    that accumulated one stale line per run would resolve the name to whichever came first.

    The hosts rewrite matches on the NAME (see `system.claims_name`) so it also clears a line an
    earlier, differently-addressed run left behind. `-delstore` matches on the CN substring, which is
    every CCC Head CA on that machine — correct, since a Windows box that has left this cluster has no
    use for the authority of one it is not in.

    One script for both roles, and the address it carries is simply the one this box reaches the Head
    on: a client's LAN address, which Windows can route to as well, or an Arm's *mesh* address, which
    it cannot. That is not a mistake on an Arm — the line is there so WSL inherits it when it rebuilds
    /etc/hosts, and that machine's Dashboard opens over the localhost forward regardless.
    """
    return f"""\
<#
    CCC — teach Windows the cluster's name and certificate authority.

    Generated by `cuttle` inside WSL for this cluster, and pointed at {addr or "(no address)"}.
    WSL cannot do this itself: both writes need Administrator, and `sudo` in there is not it.

    Run from Windows, as Administrator:
        powershell -ExecutionPolicy Bypass -File "{config.WIN_SCRIPT_NAME}"
    Undo it the same way, with -Remove.
#>
param([switch]$Remove)

$Name  = '{config.DOMAIN}'
$Addr  = '{addr}'
$Nick  = '{config.HEAD_CA_NICKNAME}'
$Cert  = Join-Path $PSScriptRoot '{config.GUEST_CA_NAME}'
$Hosts = Join-Path $env:SystemRoot 'System32\\drivers\\etc\\hosts'

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    Write-Host "This has to run as Administrator - it writes $Hosts and the machine certificate store."
    Write-Host "Start Windows PowerShell with 'Run as administrator', then run this again."
    exit 1
}}
if (-not $Remove -and -not $Addr) {{
    Write-Host "No address was baked into this script. Re-run 'cuttle setup --client' in WSL."
    exit 1
}}
if (-not $Remove -and -not (Test-Path $Cert)) {{
    Write-Host "{config.GUEST_CA_NAME} is missing from this folder. Re-run 'cuttle setup --client' in WSL."
    exit 1
}}

# Drop every line resolving the name - ours, an older run's, anyone's - then add ours back.
$kept = @(Get-Content $Hosts | Where-Object {{
    $fields = @(($_ -split '#')[0] -split '\\s+' | Where-Object {{ $_ }})
    @($fields | Select-Object -Skip 1) -notcontains $Name
}})
if (-not $Remove) {{ $kept += "$Addr`t$Name {config.HOSTS_MARKER}" }}
Set-Content -Path $Hosts -Value $kept -Encoding ASCII

if ($Remove) {{
    certutil -delstore Root "$Nick" | Out-Null
    Write-Host "Removed $Name and the $Nick from Windows."
}} else {{
    certutil -addstore -f Root "$Cert" | Out-Null
    Write-Host "$Name -> $Addr, and the $Nick is trusted. Chrome, Edge and Firefox all read this store."
}}
"""


def _write_windows_artifacts(addr: str, with_ca: bool = True) -> Path | None:
    """Put the script (and the CA it installs) where its owner will find them. Unelevated: writing
    into the Windows *user profile* over /mnt needs nothing, which is the whole reason the elevation
    happens on the far side instead."""
    into = _windows_downloads()
    if not into:
        return None
    pem = _ca_pem()
    cert = into / config.GUEST_CA_NAME
    try:
        if with_ca and pem:
            cert.write_text(pem, newline="\r\n")
        elif not with_ca:
            cert.unlink(missing_ok=True)
        script = into / config.WIN_SCRIPT_NAME
        script.write_text(_script(addr), newline="\r\n")
    except OSError:
        return None
    return script


def _windows_command(script: Path, remove: bool = False) -> str:
    return (f'powershell -ExecutionPolicy Bypass -File "{_windows_path(script)}"'
            + (" -Remove" if remove else ""))


def _offer_windows(force: bool) -> None:
    if windows_holds_name() and windows_holds_ca() and not force:
        return
    addr = _address()
    script = _write_windows_artifacts(addr)
    if not script:
        # Deliberately not naming an address here: which one is right differs by role (an Arm's
        # Windows browser goes through the localhost forward), and `cuttle dashboard` is the thing
        # that knows. Nothing is broken — this is a nicety that could not be delivered.
        ui.warn("could not reach the Windows side of this box to prepare it")
        ui.hint("`cuttle dashboard` still opens the Dashboard, behind a certificate warning")
        return
    ui.say(f"cuttle: wrote {_windows_path(script)}")
    # What running it buys differs by role, and only the payoff differs — the script is the same one.
    # An Arm already reaches the Dashboard over its localhost forward (`dashboard.url`), so the name is
    # not what it is short of; a client cannot open the Dashboard by name at all until this is run.
    if config.DASH_FORWARD_SOCKET_UNIT.exists():
        ui.hint("Windows has neither the cluster's certificate nor its name, and WSL cannot give it "
                "them — run that from Windows as Administrator to be rid of the Dashboard's "
                "certificate warning, and to stop the name vanishing in here at the next restart")
    else:
        ui.hint("Windows has neither the cluster's name nor its certificate, and WSL cannot give it "
                "them — run that from Windows as Administrator and the Dashboard opens at "
                f"https://{config.DOMAIN} like anywhere else")
    ui.hint(_windows_command(script))
    ui.result("windows", _windows_path(script))


def _offer_chromeos(force: bool) -> None:
    """ChromeOS keeps its own trust store and has no hosts file anyone can write, so this is the
    whole of what a Chromebook can be given: the CA, somewhere the Files app can reach it. The name
    is not offered at all — `dashboard.url` hands ChromeOS an address instead."""
    pem = _ca_pem()
    if not pem:
        return
    dest = _chromeos_drop() / config.GUEST_CA_NAME
    if dest.exists() and not force:
        return
    try:
        dest.write_text(pem)
    except OSError:
        return
    ui.say(f"cuttle: wrote {dest}")
    ui.hint("ChromeOS trusts nothing this box trusts, so the Dashboard opens behind a certificate "
            "warning. To be rid of it: Settings → Privacy and security → Manage certificates → "
            "Authorities → Import, and pick that file")
    ui.result("chromeos", str(dest))


def _chromeos_drop() -> Path:
    """ChromeOS's Downloads when its owner has shared it with Linux, and $HOME otherwise — which the
    Files app always shows as "Linux files", so the file is never somewhere they cannot browse to."""
    return config.CHROMEOS_DOWNLOADS if config.CHROMEOS_DOWNLOADS.is_dir() else Path.home()


# --- what the rest of cuttle calls ------------------------------------------

def offer(force: bool = False) -> None:
    """Give the OS around this box what it needs, or hand its owner the means to give it. No-op off a
    guest.

    `force` is `setup` itself — `--client` or `--arm` — which writes the artifacts whether or not the
    host OS already looks right: this box has just been pointed or enrolled, and the address or the CA
    it carries may be new. Everywhere else (`update`, `dashboard`) nothing is written while the host OS
    already holds the current name and the current CA — the check is cheap and reading it needs no
    privilege.
    """
    if system.is_wsl():
        _offer_windows(force)
    elif system.is_crostini():
        _offer_chromeos(force)


def withdraw() -> None:
    """Teardown, as far as this side can reach. No-op off a guest.

    Windows keeps what it was given until its owner takes it back, so the script is rewritten rather
    than deleted: it is the only means of removing the line and the CA it installed, and deleting it
    would strand both. Called BEFORE the box's own hosts line goes, because the address baked into
    that script is read from it.
    """
    if system.is_wsl():
        if windows_holds_name() or windows_holds_ca():
            script = _write_windows_artifacts(_address(), with_ca=False)
            if script:
                ui.warn(f"Windows still resolves {config.DOMAIN} and trusts this cluster's CA")
                ui.hint("run this from Windows as Administrator to take both back")
                ui.hint(_windows_command(script, remove=True))
                ui.result("windows", _windows_command(script, remove=True))
                return
        into = _windows_downloads()
        if into:
            for name in (config.WIN_SCRIPT_NAME, config.GUEST_CA_NAME):
                (into / name).unlink(missing_ok=True)
    elif system.is_crostini():
        (_chromeos_drop() / config.GUEST_CA_NAME).unlink(missing_ok=True)
