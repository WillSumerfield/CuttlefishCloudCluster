"""`cuttle dashboard` — open the web client, without anyone having to remember an address.

The Dashboard is served by the Head API itself (`ccc_head_api.main` mounts the SPA at `/`), so its
URL is exactly the one this box already logs in to. That is the whole point of this module: the
address is *already on disk* in `~/.ccc/config.yaml`, and asking a User to recall the Head's IP
weeks after `setup` — or to know that the port is 8082 — is asking them to remember something the
tool knows.

WSL is the one place the saved URL is the wrong answer: the CCC mesh lives inside WSL and the Windows
browser has no route to the Head's mesh IP, so `setup` installs a localhost forward
(`setup.install_wsl_dashboard_forward`) and the address to hand the browser is that instead.
"""

import subprocess
import webbrowser

from . import config, settings, system, ui


def url() -> str:
    """Where the Dashboard is *from this box*."""
    head = settings.load().get("head")
    if not head:
        raise ui.abort("this box is not pointed at a Head",
                       next_step="cuttle login --head https://<head-ip>:8082")
    # Only when the forward is actually installed: a WSL *client* (no `setup --arm`) has no proxy
    # listening on localhost, and its saved URL already reaches the Head over the LAN.
    if system.is_wsl() and config.DASH_FORWARD_SOCKET_UNIT.exists():
        return f"https://localhost:{config.HEAD_API_PORT}/"
    return head.rstrip("/") + "/"


def launch(target: str) -> bool:
    """Hand `target` to a browser. False when there was none to hand it to.

    Under WSL the browser is on the Windows side, where `webbrowser` cannot see it — it probes for
    Linux handlers and, finding none, opens nothing while reporting success. `explorer.exe` is the
    one launcher that is always present, and its exit status is famously not a result, so the call
    itself succeeding is the whole test.
    """
    if system.is_wsl():
        try:
            subprocess.run(["explorer.exe", target], check=False, capture_output=True, timeout=10)
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False
    return webbrowser.open(target)


def open_dashboard(print_only: bool = False) -> None:
    target = url()
    # `data`, not `result`: printed to stdout before the launch and whether or not it works, because
    # over SSH or on a headless Head the URL *is* the answer — and because `--print` is worth piping.
    ui.data(target)
    if print_only:
        return
    if not launch(target):
        ui.hint("no browser here — open that address yourself")
        return
    ui.say("  [muted]self-signed certificate — click through the warning once[/muted]")
