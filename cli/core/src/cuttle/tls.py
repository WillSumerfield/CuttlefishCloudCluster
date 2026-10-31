"""Trust-on-first-use pinning of the cluster's certificate authority.

No public domain -> no CA-signed cert, so the Head is its OWN CA and signs its serving cert with it
(setup.install_tls). What a client stores is that CA, never the serving cert: the leaf is reissued
whenever it stops covering an address, and a client that had pinned the leaf would break every time.

Pinning at all, rather than leaning on the OS trust store the CA is also installed into: `cafile=<the
CA>` means cuttle verifies the Head against this one authority and nothing else, so a public CA that
mis-issues for `ccc.<name>` — or any other root the box has been talked into trusting — still cannot
impersonate the Head to the CLI. The browser gets the weaker, ordinary guarantee; the CLI does not
have to.

First contact is the weak point, exactly as with SSH's known_hosts: an attacker present at the very
first connection gets their CA pinned instead. Acceptable on a trusted LAN only — and it is the
narrowest of the three ways in, since an Arm receives the CA over the token-authenticated /enroll and
an installed client receives it in the bundle. Only a generic `setup --client` ever has to ask.
"""

import hashlib
import re
import ssl
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import typer

from . import config, system, ui


def _host_port(url: str) -> tuple[str, int]:
    u = urlparse(url)
    return u.hostname or "", u.port or (443 if u.scheme == "https" else 80)


def _pin_path(url: str) -> Path:
    """One PEM per Head, keyed by host: a pin only means anything against the Head that presented it,
    and a client may one day talk to more than one cluster."""
    host, port = _host_port(url)
    return config.PINS_DIR / f"{host}_{port}.pem"


def fingerprint(pem: str) -> str:
    """SHA-256 over the DER form — the same digest a browser shows, so the two can be compared."""
    der = ssl.PEM_cert_to_DER_cert(pem)
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def fetch_ca(url: str) -> str:
    """GET /ca.pem with verification off. Unavoidable and, on its own, worth nothing: this is the first
    look at a Head we have no way to check yet. `signs_leaf` below is what turns it into a fact."""
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(f"{url.rstrip('/')}/ca.pem", context=ctx, timeout=15) as r:
        return r.read().decode()


def fetch_cluster_name(url: str) -> str:
    """GET /cluster-name, unverified like `fetch_ca` (the ceremony that follows is what vouches)."""
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(f"{url.rstrip('/')}/cluster-name", context=ctx, timeout=15) as r:
        name = r.read().decode().strip()
    if not re.match(config.CLUSTER_NAME_RE, name):
        raise ui.abort(f"{url} answered with no usable cluster name ({name[:40]!r})")
    return name


def signs_leaf(url: str, ca_pem: str) -> bool:
    """Did this CA actually sign the certificate that host is serving?

    Done by dialling the Head again with `cafile=<the fetched CA>` rather than by comparing issuers by
    hand: stdlib then checks the signature AND that the name we dialled is in the leaf's SANs, and a
    hand-rolled version of either would fail open. It does not prove the CA is the *right* one — only
    the operator confirming the fingerprint can do that — but it does prove we were handed a coherent
    pair rather than a stray certificate, so the fingerprint on screen is the one actually in use.
    """
    host, port = _host_port(url)
    ctx = ssl.create_default_context(cadata=ca_pem)
    try:
        with ssl.create_connection((host, port), timeout=15) as sock:
            with ctx.wrap_socket(sock, server_hostname=host):
                return True
    except (ssl.SSLError, OSError):
        return False


def install_ca(url: str, pem: str) -> None:
    """Record this CA as the one cuttle verifies `url` against, and trust it box-wide so browsers stop
    warning. No confirmation: every caller of this has already established the CA's provenance some
    other way (the bundle baked it, /enroll handed it over, or `ensure_ca` asked)."""
    config.PINS_DIR.mkdir(parents=True, exist_ok=True)
    pin = _pin_path(url)
    pin.write_text(pem)
    pin.chmod(0o600)
    system.trust_ccc_ca(pem)
    system.trust_ccc_ca_browsers(pem)


def pinned(url: str) -> bool:
    """Whether this box already verifies `url` against a pinned CA. Lets a caller that could install one
    unattended (the Head, holding the CA it minted) skip the work rather than re-running the box-wide
    trust install on every setup."""
    return _pin_path(url).exists()


def ensure_ca(url: str) -> None:
    """Obtain and trust this cluster's CA if we haven't already. Interactive: the operator confirms the
    fingerprint once. No-op for a plain-HTTP Head (nothing to verify) or when a pin already exists."""
    if not url.startswith("https://"):
        return
    if _pin_path(url).exists():
        return
    host, _ = _host_port(url)
    pem = fetch_ca(url)
    if not signs_leaf(url, pem):
        raise ui.abort(f"{host} served a CA certificate that did not sign the certificate it is "
                       f"actually presenting — nothing was trusted",
                       next_step="check you are talking to the Head you think you are")
    ui.warn(f"{host} presented a certificate authority this machine has never seen.")
    ui.hint("The Head signs its own certificates, so nothing can vouch for it but you. Trusting it "
            "installs it into this box's certificate stores — your browser will stop warning about "
            "the Dashboard, and cuttle will refuse any Head that cannot prove it holds this CA.")
    ui.say(f"  SHA-256  {fingerprint(pem)}")
    if not ui.ask_confirm("Trust this cluster's certificate authority from now on?"):
        raise ui.abort("not trusted — nothing was sent")
    install_ca(url, pem)
    print(f"cuttle: trusted → {_pin_path(url)} (a Head that cannot prove it holds this CA is refused)")


def refresh_pin(url: str) -> None:
    """Re-check that the CA pinned for `url` still signs what that Head is presenting, and offer to
    re-pin when it does not. What `cuttle update` runs on a client.

    The case this exists for: the Head was torn down and set up again, minting a fresh CA. Nothing on
    the client changed, but every request to it now fails verification, and the error arrives at the
    next `cuttle queue` looking like a network fault. A box that is merely out of date should be able to
    say so and fix itself.

    **Still a confirmation, not a repair.** Re-pinning silently would turn trust-on-first-use into
    trust-on-every-use: whatever answers at that address gets pinned, which is precisely the check the
    first ceremony exists to make. So this is the one question `update` is allowed to ask — and with no
    human to ask, it refuses rather than guessing.
    """
    if not url.startswith("https://"):
        return
    pin = _pin_path(url)
    if not pin.exists():
        return ensure_ca(url)     # never trusted at all: the ordinary first-trust ceremony
    if signs_leaf(url, pin.read_text()):
        return
    host, _ = _host_port(url)
    pem = fetch_ca(url)
    if not signs_leaf(url, pem):
        raise ui.abort(f"{host} served a CA certificate that did not sign the certificate it is "
                       f"actually presenting — the pin was left alone",
                       next_step="check you are talking to the Head you think you are")
    ui.warn(f"{host} is presenting a certificate signed by an authority this box has not pinned.")
    ui.hint("Usually this means the Head was rebuilt and minted a new authority. It can also mean you "
            "are not talking to the Head you think you are — only the fingerprint tells them apart, "
            "so check it against the Head before accepting.")
    ui.say(f"  SHA-256  {fingerprint(pem)}")
    if not ui.interactive():
        raise ui.abort(f"{host}'s certificate authority changed and there is nobody here to confirm it",
                       next_step="re-run `cuttle setup --client` where you can check the fingerprint")
    if not ui.ask_confirm("Trust this new certificate authority?", False):
        raise ui.abort("left pinned to the old authority — this box cannot reach that Head until "
                       "one of them changes")
    install_ca(url, pem)
    print(f"cuttle: re-pinned → {_pin_path(url)}")


def context(url: str) -> ssl.SSLContext | None:
    """The SSL context for talking to this Head: verify against the pinned CA and nothing else.

    `cafile=<the pinned CA>` makes this cluster its own certificate authority, so stdlib ssl checks both
    the signature chain and that the hostname we dialed is in the serving cert's SANs — which is why
    setup puts the Cluster name, LAN IP, mesh IP, hostname and localhost in there (setup.required_sans).
    None for a plain-HTTP Head.
    """
    if not url.startswith("https://"):
        return None
    pin = _pin_path(url)
    if not pin.exists() and config.CONTAINER_CA.exists():
        pin = config.CONTAINER_CA   # inside a Tidepool/Dive: the Head's CA, mounted in
    if not pin.exists():
        raise typer.BadParameter(
            f"no trusted certificate authority for {url} — run `cuttle login --head {url}` first")
    return ssl.create_default_context(cafile=str(pin))
