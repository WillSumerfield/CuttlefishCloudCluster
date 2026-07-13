"""Trust-on-first-use pinning of the Head's TLS certificate.

Why this exists. The Head API carries passwords and session tokens, so its LAN path must be encrypted
— but no Certificate Authority will sign a cert for a box with no public domain name (that is the
deferred item #8). So the Head signs its own. A self-signed cert gives *encryption* but proves nothing
about *identity*: anyone on the LAN can self-sign a cert claiming to be the Head, and an encrypted
connection to an impostor is exactly as bad as an unencrypted one — you hand them your password.

Identity comes back the way SSH does it. The first time we talk to a Head we have nothing to compare
against, so we show the operator the certificate's fingerprint and ask. Then we remember that exact
certificate, and every later connection must present it again. A swapped certificate is refused. This
is `known_hosts`, and its one weak point is the same as SSH's: an attacker present at the *very first*
connection would be pinned instead of the Head. Acceptable on a trusted LAN (see the roadmap's
donors-never-strangers / LAN-only scoping); it is not acceptable on the open internet, which is why a
real cert is still on the roadmap.

We pin the PEM and hand it to `ssl` as the CA, rather than disabling verification and comparing
fingerprints by hand. Same trust model, but stdlib does the signature *and* hostname checks — and a bug
in a hand-rolled check fails **open** (no verification at all), which is the worst possible failure.
"""

import hashlib
import ssl
from pathlib import Path
from urllib.parse import urlparse

import typer

from . import config


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


def ensure_pinned(url: str) -> None:
    """Pin the Head's cert if we haven't already. Interactive: the operator confirms the fingerprint
    once. No-op for a plain-HTTP Head (nothing to pin) or when a pin already exists."""
    if not url.startswith("https://"):
        return
    pin = _pin_path(url)
    if pin.exists():
        return
    host, port = _host_port(url)
    # No verification possible here by definition — this IS the first look at the cert.
    pem = ssl.get_server_certificate((host, port))
    print(f"\ncuttle: {host} presented a certificate this machine has never seen.\n"
          f"        The Head signs its own certificate, so nothing can vouch for it but you.\n\n"
          f"        SHA-256  {fingerprint(pem)}\n")
    if not typer.confirm("Trust this Head from now on?"):
        raise typer.Abort()
    config.PINS_DIR.mkdir(parents=True, exist_ok=True)
    pin.write_text(pem)
    pin.chmod(0o600)
    print(f"cuttle: pinned → {pin} (a different certificate will now be refused)")


def context(url: str) -> ssl.SSLContext | None:
    """The SSL context for talking to this Head: verify against the pinned cert and nothing else.

    `cafile=<the pinned cert>` makes the Head its own certificate authority, so stdlib ssl checks both
    the signature and that the hostname we dialed is in the cert's SANs — which is why setup puts the
    LAN IP, mesh IP, hostname and localhost in there. None for a plain-HTTP Head.
    """
    if not url.startswith("https://"):
        return None
    pin = _pin_path(url)
    if not pin.exists():
        raise typer.BadParameter(
            f"no pinned certificate for {url} — run `cuttle login --head {url}` first")
    return ssl.create_default_context(cafile=str(pin))
