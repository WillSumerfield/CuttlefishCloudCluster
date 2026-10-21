"""The OS around a guest install — ChromeOS under Crostini.

`setup` teaches *this box* the **Cluster name** and the cluster's CA — a client and an Arm alike. On a
guest that is not where the browser is: the browser its owner actually uses runs on the OS hosting the
virtual machine, which has neither, and which for this purpose is a separate machine (CONTEXT.md,
**Cluster name**). Everything here is about that OS.

**ChromeOS can be given neither name nor trust automatically.** It has no user-writable hosts file at
any privilege, so a Chromebook's Dashboard is reached by *address* (`dashboard.url`), and the CA is
dropped where the Files app can see it for a manual import.

Nothing here is load-bearing. Every entry point is a no-op off a guest, none of them raise, and a
client whose owner ignores all of it still works: `cuttle` resolves the name inside the guest, and the
Dashboard opens at the Head's address behind a certificate warning — the same one every device CCC was
never installed on gets.
"""

from pathlib import Path

from . import config, system, ui


def outside_browser_has_name() -> bool:
    """Whether the browser this box's owner will actually open resolves the **Cluster name**.

    True off a guest, where that browser is on this box and setup already gave it the name. What
    `dashboard.url` asks before handing over a URL: a name the browser cannot resolve is worse than an
    address, because it fails identically to a Head that is down.
    """
    return not system.is_crostini()


def _ca_pem() -> str | None:
    """The cluster's CA as this box holds it — the OS trust store copy, which is the one every role
    writes (`system.trust_ccc_ca`) and the one teardown removes."""
    try:
        return config.HEAD_CA_TRUST_PATH.read_text()
    except OSError:
        return None


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

    `force` is `setup` itself — `--client` or `--arm` — which writes the artifact whether or not it is
    already there: this box has just been pointed or enrolled, and the CA it carries may be new.
    Everywhere else (`update`, `dashboard`) nothing is written while the file already exists.
    """
    if system.is_crostini():
        _offer_chromeos(force)


def withdraw() -> None:
    """Teardown, as far as this side can reach. No-op off a guest."""
    if system.is_crostini():
        (_chromeos_drop() / config.GUEST_CA_NAME).unlink(missing_ok=True)
