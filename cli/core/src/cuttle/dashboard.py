"""`cuttle dashboard` — open the web client, without anyone having to remember an address.

The Dashboard is served by the Head API itself (`ccc_head_api.main` mounts the SPA at `/`), so its
URL is exactly the one this box already logs in to. That is the whole point of this module: the
address is *already on disk* in `~/.ccc/config.yaml`, and asking a User to recall the Head's IP weeks
after `setup` is asking them to remember something the tool knows.

A **guest** is where the saved URL can be the wrong answer, because the browser is not on this box at
all — it is on the OS hosting the virtual machine (ChromeOS under Crostini), which holds neither the
**Cluster name** nor the cluster's CA (`guest.py`). Two cases come out of that, and `url` is the whole
of where they are decided:

  * a **guest whose host OS lacks the name** — it gets the *address*, which is in the Head
    certificate's SANs and so loads, rather than a name that fails exactly like a Head that is down.
    Always the case on ChromeOS, which has no hosts file anyone can write;
  * everything else — the saved URL, which is the name, as it is on every box CCC installs on.

The certificate warning is the guest's other tax: ChromeOS clicks through once per browser.
"""

import socket
import webbrowser
from urllib.parse import urlparse

from . import guest, settings, ui


def url() -> str:
    """Where the Dashboard is *from this box*."""
    head = settings.load().get("head")
    if not head:
        raise ui.abort("this box is not pointed at a Head",
                       next_step="cuttle setup --client")
    if not guest.outside_browser_has_name():
        return _by_address(head)
    return head.rstrip("/") + "/"


def _by_address(head: str) -> str:
    """The saved URL with the **Cluster name** swapped for the address it means on this box.

    The name is resolved here rather than stored anywhere: a client keeps its Head's address in its
    /etc/hosts line and nowhere else (`setup.point_at_head`), and that is still true of a guest — the
    host OS's browser is simply told the answer instead of being able to look it up. Falls back to the
    saved URL if the name does not resolve, since a box in that state has a larger problem than which
    spelling of the address its browser gets.
    """
    u = urlparse(head.rstrip("/") + "/")
    try:
        addr = socket.gethostbyname(u.hostname)
    except (OSError, TypeError):
        return head.rstrip("/") + "/"
    return u._replace(netloc=addr + (f":{u.port}" if u.port else "")).geturl()


def launch(target: str) -> bool:
    """Hand `target` to a browser. False when there was none to hand it to."""
    return webbrowser.open(target)


def open_dashboard(print_only: bool = False) -> None:
    # Before the URL is chosen, not after: on a guest this is what may put the **Cluster name** on the
    # host OS, and it is the only command a bundle-installed client is guaranteed to reach (install.sh
    # points and trusts the box itself, so `setup --client` may never run there). Silent unless the
    # host OS is actually missing something.
    guest.offer()
    target = url()
    # `data`, not `result`: printed to stdout before the launch and whether or not it works, because
    # over SSH or on a headless Head the URL *is* the answer — and because `--print` is worth piping.
    ui.data(target)
    if print_only:
        return
    if not launch(target):
        ui.hint("no browser here — open that address yourself")
        return
    ui.ok(f"opened the dashboard in your browser to: {target}")
