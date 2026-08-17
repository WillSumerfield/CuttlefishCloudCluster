"""Mesh layer.

Self-hosted **Headscale** coordinator on the Head + **Tailscale** client on every node. The Head
runs its own control server; all nodes (including the co-located Arm) join that tailnet. Arms
enroll with a preauth key minted by the Head under the CCC user.

Intended path only — no fallbacks (test box; failures decided live).
"""

import base64
import ipaddress
import json
import shutil
import ssl
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from . import config, tls, ui
# `_dump` is reached across the module boundary on purpose: join_mesh cannot go through `run`
# (see its docstring) but must still report a failure the way every other command does.
from .system import _dump, hostname, prime_sudo, run, sh


# --- install ---------------------------------------------------------------

def _headscale_deb(tag: str) -> str:
    """The amd64 .deb URL for a Headscale release tag. Composed, not discovered: the asset name is a
    documented convention, so asking the GitHub API for it only added an unauthenticated request
    (60/hr per IP) to the setup path and a way for the answer to change between two Heads."""
    return config.HEADSCALE_DEB.format(tag=tag, ver=tag.lstrip("v"))


def _install_deb(url: str, reinstall: bool = False) -> None:
    """--force-confmiss restores conffiles the admin deleted (dpkg won't, by default)."""
    opts = "--reinstall " if reinstall else ""
    # Unlinked afterwards: dpkg has copied what it needs, and nothing sweeps /tmp/headscale.deb —
    # `_sweep_job_tmp` only globs /tmp/ccc-*, so it would sit on every node CCC ever touched.
    sh(f"curl -fsSL -o /tmp/headscale.deb {url} && "
       f'apt-get install -y {opts}-o Dpkg::Options::=--force-confmiss /tmp/headscale.deb; '
       f"rc=$?; rm -f /tmp/headscale.deb; exit $rc", sudo=True)


def installed_headscale() -> str | None:
    """Version tag of the installed Headscale, or None. (`headscale version` prints a paragraph;
    we want the `vX.Y.Z` off the first line, to match the GitHub release tag we record.)"""
    if not shutil.which("headscale"):
        return None
    r = run(["headscale", "version"], capture_output=True, text=True, check=False)
    first = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    return first.split()[-1] if first else None


def install_headscale() -> str:
    """Install the pinned Headscale .deb (not carried in apt). Returns the version tag.

    Skipped if already installed, even though the version is pinned: bumping HEADSCALE_VERSION would
    otherwise upgrade Headscale and migrate its database — the database holding every node's mesh IP —
    as a side effect of, say, changing the coordinator address. Upgrade deliberately, not incidentally.
    """
    if (have := installed_headscale()) is not None:
        # `teardown --keep-packages` removes /etc/headscale but leaves the package, so a re-setup
        # finds the binary and no config, then seds a file that isn't there. Re-fetch the deb for
        # the version that is *already installed* — never the pinned one here, or a missing config
        # would silently move an existing Head onto a new Headscale and migrate its node database.
        # (`apt-get install --reinstall headscale` can't work: the deb came from a URL, not a repo.)
        if not config.HEADSCALE_CONF.exists():
            print(f"headscale {have} installed but {config.HEADSCALE_CONF} is missing — "
                  f"re-fetching {have} to restore the shipped config")
            _install_deb(_headscale_deb(have), reinstall=True)
        else:
            print(f"headscale already installed ({have}) — skipping (upgrade deliberately)")
        return have
    tag = config.HEADSCALE_VERSION
    print(f"headscale: installing {tag}")
    _install_deb(_headscale_deb(tag))
    return tag


def install_tailscale() -> None:
    """Install the Tailscale client (provides the `tailscaled` binary + CLI). Skipped if already
    present — never touch an existing install (it may be carrying the admin's personal tailnet)."""
    if shutil.which("tailscale"):
        print("tailscale already installed — skipping (won't disturb the personal daemon)")
        return
    sh(f"curl -fsSL {config.TAILSCALE_REPO_KEY} -o {config.TAILSCALE_KEYRING} && "
       f"curl -fsSL {config.TAILSCALE_REPO_LIST} -o {config.TAILSCALE_REPO_FILE}", sudo=True)
    run(["apt-get", "update"], sudo=True)
    run(["apt-get", "install", "-y", "tailscale"], sudo=True)


# --- CCC's own isolated tailscaled instance --------------------------------
# A second tailscaled with its own socket/state/tun/port. The default (personal) tailscaled is
# never referenced, so the admin's connection to the box cannot be affected.

_CCC_TAILSCALED_UNIT = f"""\
[Unit]
Description=CCC Tailscale agent (isolated instance — does not touch the default tailscaled)
After=network-pre.target
Wants=network-pre.target

[Service]
ExecStart=/usr/sbin/tailscaled --state={config.CCC_TS_STATE} --socket={config.CCC_TS_SOCKET} \
--tun={config.CCC_TS_TUN} --port={config.CCC_TS_PORT}
RuntimeDirectory={config.CCC_TS_RUNDIR.name}
StateDirectory={config.CCC_TS_STATEDIR.name}
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""


def start_ccc_tailscaled(write_unit) -> None:
    """Install + start CCC's dedicated tailscaled instance. `write_unit` is a callable that writes
    a root-owned file (setup._write_root_file) — passed in to avoid a setup↔mesh import cycle."""
    write_unit(config.CCC_TS_UNIT, _CCC_TAILSCALED_UNIT, "644")
    run(["systemctl", "daemon-reload"], sudo=True)
    run(["systemctl", "enable", config.CCC_TS_SERVICE], sudo=True)
    run(["systemctl", "restart", config.CCC_TS_SERVICE], sudo=True)

    # `systemctl restart` exits 0 for a unit that starts and then dies, so its status says nothing
    # about liveness. Without this, a dead daemon surfaces three steps later as a baffling
    # "no such file: tailscaled.sock" from `tailscale up`.
    time.sleep(2)
    if run(["systemctl", "is-active", config.CCC_TS_SERVICE],
           sudo=True, check=False, capture_output=True, text=True).stdout.strip() != "active":
        # Captured and printed, not left to `run`'s default: `run` captures whenever the caller asks
        # for neither stream, and `check=False` means its own failure dump never fires — so this
        # status was being collected and discarded, under an error telling the operator to read it.
        status = run(["systemctl", "status", config.CCC_TS_SERVICE, "--no-pager", "-l"],
                     sudo=True, check=False, capture_output=True, text=True)
        ui.data((status.stdout + status.stderr).rstrip())
        raise ui.abort(f"{config.CCC_TS_SERVICE} died on startup — see the status above",
                       next_step=f"re-run setup, or inspect with `journalctl -u {config.CCC_TS_SERVICE}`")


# --- Headscale coordinator (Head only) -------------------------------------

# The coordinator's address is the ONE address that must work before the mesh exists — an Arm dials
# it with no tunnel to fall back on. These blocks can only be reached from the Head's own network, so
# an Arm anywhere else gets packets that look routable, leave, and die. Lives here, beside the
# coordinator, because both the code that WRITES the address (setup.mesh_up_head) and the code that
# hands it out in a token (enroll) have to agree on what "unreachable from outside" means.
#
# NOT 100.64.0.0/10 — that is the CGNAT range Tailscale allocates from, and a coordinator advertised
# there is precisely how a remote donor bootstraps. Written out rather than using
# `ipaddress.is_private`, whose membership has moved between Python point releases (it counts
# 100.64.0.0/10 as private in some, which would flag the one address that works).
_LAN_ONLY_BLOCKS = [ipaddress.ip_network(b) for b in
                    ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16")]


def lan_only(host: str) -> bool:
    """Is `host` an address only this LAN can reach? False for a DNS name — a name can resolve to
    anything, and guessing wrong in either direction is worse than staying quiet."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in block for block in _LAN_ONLY_BLOCKS)


_POLICY = """\
{
  // Enrolled CCC nodes reach each other on every port. `cuttle arm authorize` is the trust gate;
  // this file must not pretend to be a second one — mesh membership already means full cluster
  // trust (an Arm can fetch the munge key), so a port list here would buy no security while
  // breaking srun's ephemeral task-I/O ports and Arm<->Arm traffic in multi-node jobs. Scoped to
  // the `ccc` user, so it grants nothing to nodes outside this mesh.
  // The trailing `@` marks these as usernames. A bare name is parsed as a *host* alias, and
  // headscale refuses to start: `host not defined in policy: "ccc"`.
  "acls": [
    { "action": "accept", "src": ["%(user)s@"], "dst": ["%(user)s@:*"] }
  ]
}
""" % {"user": config.HEADSCALE_USER}


def _san_entry(addr: str) -> str:
    """`IP:<addr>` or `DNS:<addr>` — whichever the coordinator's advertised host actually is.
    `--address` may be a literal IP (the ordinary case) or a public/DDNS name, and a cert's SAN has
    to say which kind each entry is or the check fails."""
    try:
        ipaddress.ip_address(addr)
        return f"IP:{addr}"
    except ValueError:
        return f"DNS:{addr}"


def install_headscale_tls(addr: str) -> tuple[Path, Path]:
    """Generate the coordinator's own self-signed cert — separate from the Head API's
    (setup.install_tls): different service, different reader, different trust story. That one is
    pinned by cuttle's own HTTP client (cuttle/tls.py); this one has to be trusted by `tailscaled` —
    a separate binary with no pinning option of its own — on every box that ever dials this
    coordinator (see trust_headscale_cert).

    Lives INSIDE HEADSCALE_LIB (0750 headscale:headscale, set by the package) rather than
    CCC_TLS_DIR, so the `headscale` unix account can read it with no chown of its own — cuttle never
    loosens that directory. Generated only if absent, for the same reason install_tls is: once any
    box has trusted these exact bytes, regenerating them silently un-trusts every one of them at once.

    SANs: the coordinator's advertised host (what every Arm actually dials, `--address`) plus this
    box's hostname and localhost — NOT the mesh IP, unlike the Head API's cert. The coordinator is
    dialed BEFORE the mesh exists; a mesh address here would be the one SAN that can never be used.
    """
    if config.HEADSCALE_CERT.exists():
        print(f"cuttle: Headscale TLS cert exists ({config.HEADSCALE_CERT}) — keeping it "
              f"(re-generating would un-trust it on every Arm that has already trusted it)")
        return config.HEADSCALE_CERT, config.HEADSCALE_KEY
    host = hostname()
    san = f"DNS:{host},DNS:localhost,IP:127.0.0.1,{_san_entry(addr)}"
    run(["openssl", "req", "-x509", "-newkey", "rsa:4096", "-nodes",
         "-days", str(config.CERT_DAYS),
         "-subj", f"/CN={host}", "-addext", f"subjectAltName={san}",
         "-keyout", str(config.HEADSCALE_KEY), "-out", str(config.HEADSCALE_CERT)], sudo=True)
    run(["chown", f"{config.HEADSCALE_SERVICE_USER}:{config.HEADSCALE_SERVICE_USER}",
         str(config.HEADSCALE_KEY)], sudo=True)
    run(["chmod", "0600", str(config.HEADSCALE_KEY)], sudo=True)   # the private half: headscale-only
    run(["chown", f"{config.HEADSCALE_SERVICE_USER}:{config.HEADSCALE_SERVICE_USER}",
         str(config.HEADSCALE_CERT)], sudo=True)
    run(["chmod", "0644", str(config.HEADSCALE_CERT)], sudo=True)  # the public half: readable, once in
    print(f"cuttle: Headscale TLS cert for {san}")
    return config.HEADSCALE_CERT, config.HEADSCALE_KEY


def configure_headscale(server_url: str, addr: str, write_root_file) -> None:
    """Patch the package-shipped config: server_url, bind-all, TLS, the CCC IP prefixes, and the
    policy.

    We edit the shipped /etc/headscale/config.yaml rather than render our own — robust to
    Headscale's config-schema drift across versions (only well-known keys touched). The prefixes
    carve a sub-range out of Tailscale's own space; see config.MESH_V4_PREFIX for why they cannot
    sit outside it.

    The policy is not optional. `policy.mode: file` ships with an empty `path`, and an empty policy
    is **deny-all**: nodes register, get addresses, and establish DERP, but every peer arrives in the
    netmap with no allowed IPs, so nothing can route to anything. Invisible on a one-node Head (a
    node always reaches itself) and total the moment a second node joins.
    """
    run(["install", "-d", "-o", "root", "-g", "root", str(config.HEADSCALE_LIB)], sudo=True)
    write_root_file(config.HEADSCALE_POLICY, _POLICY, "644")
    cert, key = install_headscale_tls(addr)
    # magic_dns/override_local_dns OFF: a node accepting them claims resolved's `~.` default-route
    # domain and hijacks host-wide DNS. Nodes address each other by mesh IP; MagicDNS is unwanted.
    # tls_cert_path/tls_key_path is what actually turns TLS on — headscale reads their presence, not
    # a separate on/off key; `server_url`'s https:// and `listen_addr` (still bare host:port, schemes
    # don't apply to it) agreeing with that is on the caller (setup.mesh_up_head builds both).
    sh(f"sed -ri "
       f"-e 's#^server_url:.*#server_url: {server_url}#' "
       f"-e 's#^listen_addr:.*#listen_addr: 0.0.0.0:{config.HEADSCALE_PORT}#' "
       f"-e 's#^([[:space:]]*)v4:.*#\\1v4: {config.MESH_V4_PREFIX}#' "
       f"-e 's#^([[:space:]]*)v6:.*#\\1v6: {config.MESH_V6_PREFIX}#' "
       f"-e 's#^([[:space:]]*)magic_dns:.*#\\1magic_dns: false#' "
       f"-e 's#^([[:space:]]*)override_local_dns:.*#\\1override_local_dns: false#' "
       f"-e 's#^tls_cert_path:.*#tls_cert_path: {cert}#' "
       f"-e 's#^tls_key_path:.*#tls_key_path: {key}#' "
       f"-e '/^policy:/,/^[a-z_]*:/ s#^([[:space:]]*)path:.*#\\1path: {config.HEADSCALE_POLICY}#' "
       f"{config.HEADSCALE_CONF}", sudo=True)


def start_headscale() -> None:
    run(["systemctl", "enable", "headscale"], sudo=True)
    run(["systemctl", "restart", "headscale"], sudo=True)


# --- trusting the coordinator's cert -----------------------------------------
# `tailscaled` makes the actual TLS connection when it dials `--login-server https://...` — a
# separate Go binary, not cuttle's own Python HTTP client, and it has no pinning flag of its own
# (`tailscale up --help` carries no --insecure-skip-verify, no custom-CA option, nothing). The ONLY
# lever it reads is the OS's own CA trust store, so that is what these install into — a heavier,
# coarser mechanism than cuttle/tls.py's fingerprint-pin (which only ever governed cuttle's own
# urllib client, e.g. the Head API dashboard): trusting this CA trusts anything it ever signs, with
# no per-connection "this exact cert, and only this one" check the way a pin gives.

def trust_headscale_cert(pem: str) -> None:
    """Install a Headscale coordinator's cert into THIS box's OS trust store. Idempotent in effect
    (update-ca-certificates re-runs happily), but callers still gate on `HEADSCALE_CA_PATH.exists()`
    so a normal run doesn't re-announce a decision already made."""
    with tempfile.NamedTemporaryFile("w", suffix=".crt", delete=False) as f:
        f.write(pem)
        tmp = f.name
    run(["install", "-D", "-m", "0644", tmp, str(config.HEADSCALE_CA_PATH)], sudo=True)
    run(["update-ca-certificates"], sudo=True)


def trust_own_headscale_cert() -> None:
    """The Head trusting the cert it just minted for its OWN coordinator, before its own tailscaled
    can join it. Not a TOFU moment — setup wrote these exact bytes to disk a few lines ago — so there
    is nothing to confirm, only to announce: installing anything into the OS CA store is a real host
    mutation regardless of who authored the cert, and system.allow_mesh_iface/ufw rules already set
    the precedent of a one-line print rather than silence for this class of change.

    Reads HEADSCALE_CERT via `sudo cat`, not a bare Python open(): the file's own mode is 0644, but
    its parent (HEADSCALE_LIB, 0750 headscale:headscale) blocks the operator's own login user from
    even traversing into the directory to reach it — same reason mesh._state_prefs reads CCC_TS_STATE
    the same way.
    """
    if config.HEADSCALE_CA_PATH.exists():
        return
    pem = run(["cat", str(config.HEADSCALE_CERT)], sudo=True, capture_output=True, text=True).stdout
    print(f"cuttle: trusting this Head's own coordinator certificate ({config.HEADSCALE_CERT}) so "
          f"its own tailscaled can join it — installing into the OS CA store ({config.HEADSCALE_CA_PATH})")
    trust_headscale_cert(pem)


def ensure_headscale_trusted(coordinator: str) -> None:
    """TOFU for an Arm's FIRST contact with a coordinator, mirroring cuttle/tls.py's ensure_ca —
    except what gets trusted differs (see the module note above): this installs into the OS CA store,
    the one thing tailscaled actually reads, not cuttle/tls.py's PINS_DIR.

    No-op for a plain-HTTP coordinator (nothing to trust — a transitional/already-http deployment) or
    once this box has already trusted A Headscale cert (one coordinator per Arm; see HEADSCALE_CA_PATH).
    """
    if not coordinator.startswith("https://") or config.HEADSCALE_CA_PATH.exists():
        return
    host = urlparse(coordinator).hostname
    port = urlparse(coordinator).port or 443
    # No verification possible here by definition — this IS the first look at the cert, same as
    # tls.fetch_ca's identical position for the Head API.
    pem = ssl.get_server_certificate((host, port))
    ui.warn(f"{host} presented a certificate this machine has never seen — it's the CCC mesh "
           f"coordinator, self-signed (no public CA will sign a cert for a box with no public "
           f"domain).")
    ui.hint("Trusting it installs it into this box's OS certificate store, not just a cuttle-side "
            "pin — tailscaled has no lighter-weight option. A DIFFERENT certificate presented later "
            "at this address is still refused normally; what is not protected is a swap at this "
            "exact moment, the same weak point SSH's known_hosts has on a first connection.")
    ui.say(f"  SHA-256  {tls.fingerprint(pem)}")
    if not ui.ask_confirm("Trust this coordinator's certificate?"):
        raise ui.abort("not trusted — the mesh join did not proceed")
    trust_headscale_cert(pem)
    ui.say(f"  [muted]installed → {config.HEADSCALE_CA_PATH}[/muted]")


def ensure_user() -> None:
    """Create the Headscale user/namespace Arms enroll under (idempotent — ignore 'exists')."""
    run(["headscale", "users", "create", config.HEADSCALE_USER], sudo=True, check=False)


def _user_id() -> str:
    """Numeric id of the CCC Headscale user (recent headscale wants id, not name, for keys)."""
    r = run(["headscale", "users", "list", "--output", "json"],
            sudo=True, capture_output=True, text=True)
    for u in json.loads(r.stdout) or []:  # `null`, not `[]`, when empty — see list_nodes
        if u["name"] == config.HEADSCALE_USER:
            return str(u["id"])
    raise ui.abort(f"the CCC Headscale user {config.HEADSCALE_USER!r} doesn't exist — "
                   f"the Head's mesh coordinator isn't fully set up",
                   next_step="re-run `cuttle setup --head`")


def mint_preauthkey(reusable: bool = False, hours: int = 1) -> str:
    """Mint a preauth key under the CCC user (goes into the Arm join token)."""
    args = ["headscale", "preauthkeys", "create", "--user", _user_id(),
            "--expiration", f"{hours}h", "--output", "json"]
    if reusable:
        args.append("--reusable")
    r = run(args, sudo=True, capture_output=True, text=True)
    return json.loads(r.stdout)["key"]


# --- Tailscale client (every node) -----------------------------------------

def join_mesh(login_server: str, hostname: str, authkey: str) -> None:
    """`tailscale up` on CCC's isolated instance (targeted via --socket). `--reset` only affects
    CCC's own instance; the personal daemon is untouched.

    Always unattended: the caller presents a preauth key and the node joins without anyone approving
    anything. The Head mints its own key (it registers against its own Headscale, with no operator to
    ask); an Arm's key arrives inside its **enrollment token**, minted by `cuttle arm authorize`. The
    admission decision has already been made by then — it *is* the act of minting the key.

    `hostname` names the node in Headscale. We pass the Slurm node name so one string identifies the
    machine in both systems — `cuttle arm remove <name>` can then reap it from each.

    Three flags keep CCC off host-global resources a second tailscaled would otherwise fight over:
    - `--accept-dns=false`: accepting DNS makes CCC claim systemd-resolved's `~.` default-route
      domain and point all lookups at its own MagicDNS, killing name resolution host-wide.
    - `--netfilter-mode=off`: tailscaled installs `ts-input`/`ts-forward`/`ts-postrouting` chains
      under fixed names; two daemons rewrite each other's, breaking unrelated traffic. CCC runs no
      subnet router or exit node, so it needs no firewall rules.
    - `--accept-routes=false`: no subnet routers here; keeps CCC out of the host route table.
    CCC addresses Arms by mesh IP, so it loses nothing.

    **Hand-rolled rather than `run`, and quiet like it.** The authkey is an argument, and `run` echoes
    the command it is about to spawn under `--verbose` — through `ui.say`, which also writes the run
    log. So this is the one command whose echo has to be redacted by hand, which is why it does its
    own `subprocess.run` and its own `_dump`.
    Capture is the other half, and it is what keeps `setup`'s box intact: `section` redirects Python's
    `sys.stdout`, but a *child process* inherits the real fd 1/2 and writes straight onto the alternate
    screen underneath. `tailscale up` was the only command on the setup path still doing that, so the
    frame tore on every join and was repaired a fraction of a second later by the repaint tick.
    Nothing is lost by holding it: this join is always unattended (the caller presents a preauth key,
    so there is no auth URL to watch), and a failure still prints everything the command said.
    """
    cmd = ["sudo", "tailscale", f"--socket={config.CCC_TS_SOCKET}", "up", "--reset",
           "--login-server", login_server, "--hostname", hostname,
           "--accept-dns=false", "--accept-routes=false", "--netfilter-mode=off"]
    # `cmd` carries its own `sudo`, so `run`'s priming never happens here. A no-op once the enclosing
    # flow has primed — but if it ever did prompt, capture would swallow the prompt and the join
    # would look like a hang with nothing on screen to explain it.
    prime_sudo()
    if ui.VERBOSE:
        ui.say(f"[muted]$ {' '.join(cmd)} --authkey <redacted>[/muted]")   # never echo the key
    kw = {} if ui.VERBOSE else {"capture_output": True, "text": True}
    try:
        subprocess.run(cmd + ["--authkey", authkey], check=True, **kw)
    except subprocess.CalledProcessError as e:
        _dump(e)
        raise


def tailscale_ip() -> str:
    """CCC mesh IP (from CCC's isolated instance, not the personal daemon)."""
    r = run(["tailscale", f"--socket={config.CCC_TS_SOCKET}", "ip", "-4"],
            capture_output=True, text=True)
    return r.stdout.strip().splitlines()[0]


def _ts_json(*args: str) -> dict | None:
    """Query CCC's tailscaled. None if the daemon can't answer — which we read as 'not joined'."""
    r = run(["tailscale", f"--socket={config.CCC_TS_SOCKET}", *args],
            capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)


def _state_prefs() -> dict | None:
    """ControlURL + LoggedOut, read straight off tailscaled-ccc's own on-disk state — not through
    `tailscale debug prefs` (an explicitly unstable CLI surface Tailscale may change or drop; see
    joined_to's docstring). The state file is tailscaled's own persistence, keyed by profile:
    `_current-profile` names the active one (base64), and that key's value (base64 again) is the
    profile's Prefs JSON — the same struct `debug prefs` prints. tailscaled has to keep reading its
    own past state across upgrades, so this shape is a much safer bet than a debug-only subcommand.

    Root-owned (0600) — needs sudo, primed by the caller's setup flow before this runs. Never returns
    the full decoded Prefs blob: it also carries PrivateNodeKey/NetworkLockKey, real secret material
    that has no business leaving this function.
    """
    r = run(["cat", str(config.CCC_TS_STATE)], sudo=True, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return None
    try:
        store = json.loads(r.stdout)
        current = base64.b64decode(store["_current-profile"]).decode()
        prefs = json.loads(base64.b64decode(store[current]))
    except (KeyError, ValueError, UnicodeDecodeError):
        return None
    return {"ControlURL": prefs.get("ControlURL"), "LoggedOut": prefs.get("LoggedOut", False)}


def current_coordinator() -> str | None:
    """Which coordinator this box is registered against, or None if it is not on the mesh.

    The one fact an **enrollment token** carries that is not also written to settings, and it is on
    disk anyway — tailscaled has to remember who it talks to. That is what lets a box that is already
    enrolled be set up again without one (see enroll.enrollment_from_state)."""
    prefs = _state_prefs()
    return prefs.get("ControlURL") if prefs else None


def joined_to(coordinator: str) -> bool:
    """Is this node already on the mesh, registered against *this* coordinator?"""
    prefs = _state_prefs()
    status = _ts_json("status", "--json")
    if prefs is None or status is None:
        return False
    return (prefs.get("ControlURL") == coordinator
            and not prefs.get("LoggedOut", False)
            and status.get("BackendState") == "Running")


def _wipe_local_state() -> None:
    """Stop CCC's tailscaled and delete its identity. The Arm half of forget_state()."""
    run(["systemctl", "stop", config.CCC_TS_SERVICE], sudo=True, check=False)
    run(["rm", "-f", str(config.CCC_TS_STATE)], sudo=True, check=False)


# --- node removal (Head only) ----------------------------------------------

def list_nodes() -> list[dict]:
    """Every node Headscale knows. Empty on a fresh coordinator — headscale prints the JSON literal
    `null` rather than `[]` when it has none, so `or []` is load-bearing: without it a fresh Head
    crashes in `forget_state()` with 'NoneType is not iterable' before it can join its own mesh."""
    r = run(["headscale", "nodes", "list", "--output", "json"],
            sudo=True, capture_output=True, text=True)
    return json.loads(r.stdout) or []


def node_ids_named(name: str) -> list[str]:
    """Ids of every node Headscale knows by this name. Normally 0 or 1; 2 while an Arm re-enrolls."""
    return [str(n["id"]) for n in list_nodes()
            if n.get("given_name") == name or n.get("name") == name]


def delete_node_id(node_id: str) -> None:
    run(["headscale", "nodes", "delete", "--identifier", node_id, "--force"], sudo=True)


def delete_node(name: str) -> None:
    """Evict a node from the mesh by its Headscale name (== its Slurm node name; see join_mesh).

    Half of `cuttle arm remove`. Skipping this half leaves a departed donor holding a live mesh
    address — and the mesh is what authorizes `GET /enroll`, i.e. the munge key.
    """
    ids = node_ids_named(name)
    if not ids:
        print(f"mesh: no node named {name!r} — nothing to evict")
    for node_id in ids:
        delete_node_id(node_id)


# --- orchestration ---------------------------------------------------------

def up_head(server_url: str, addr: str, write_unit) -> tuple[str, str]:
    """Bring up the Head's coordinator and join it via CCC's isolated tailscaled instance. `addr` is
    the coordinator's bare advertised host (no scheme/port — goes into the cert's SANs; `server_url`
    is built from it by the caller). `write_unit` writes a root-owned file (setup._write_root_file).
    Returns (headscale_version, mesh_ip).

    Re-runnable, and it has to be: `setup --head` is the only way to re-provision a live Head, so it
    meets a daemon that already holds mesh state far more often than a fresh one. Three cases, and the
    middle one is a trap:

    - **Already joined to this coordinator** — nothing to do. Skip the join entirely rather than
      `up --reset`-ing a working daemon, and leave the (single-use) preauth key unminted.
    - **Holding stale state** (a rebuilt Head, a re-pointed coordinator, or a join that half-failed) —
      `forget_state()` first, unconditionally. Without it `tailscale up` inherits a dialer that has
      latched onto the control URL it last spoke to, so even a same-address re-run can fail against a
      coordinator that is demonstrably up — gating the wipe on the address alone misses that case.
    - **Fresh box** — no state; `forget_state()` is a no-op beyond an "already gone" print. It must run
      here and not in the caller, because it shells out to `headscale`, which only exists after
      `install_headscale()` above.
    """
    hs_ver = install_headscale()
    configure_headscale(server_url, addr, write_unit)
    start_headscale()
    ensure_user()
    # Before ANY tailscale contact with this coordinator, including the Head's own self-join two
    # lines down — tailscaled verifies the cert on every `tailscale up`, itself included.
    trust_own_headscale_cert()
    install_tailscale()
    start_ccc_tailscaled(write_unit)

    if joined_to(server_url):
        ip = tailscale_ip()
        print(f"cuttle: already on the mesh at {ip} via {server_url} — skipping join")
        return hs_ver, ip

    forget_state()
    start_ccc_tailscaled(write_unit)
    # The Head registers against its own Headscale: nobody to approve it, so it uses a preauth key.
    join_mesh(server_url, hostname(), authkey=mint_preauthkey())
    return hs_ver, tailscale_ip()


def up_arm(coordinator: str, node_name: str, authkey: str, write_unit,
           re_enroll: bool = False) -> str:
    """Join an Arm to an existing mesh with the preauth key from its enrollment token. Returns its
    mesh IP. Nobody approves anything here — the Admin already did, by minting the key.

    A state file naming a node key the Head no longer knows (it ran `cuttle arm remove`, or rebuilt
    its database) makes `tailscale up` hang forever: no auth URL, no error, nothing to diagnose. So
    we wipe unless the daemon is demonstrably already joined to *this* coordinator — in which case
    there is nothing to do, and re-running setup leaves the (single-use) key unspent.

    `re_enroll` forces the wipe, for the cases the check can't see: a corrupt state file, or a Head
    that was rebuilt but still answers at the same URL.

    **A token is good for exactly one attempt.** The wipe has to precede the join (see above), and the
    preauth key inside the token is single-use — so a `setup --arm` that dies *after* the join has both
    spent the key and reset this box's identity, and re-running it with the same token cannot work. The
    bare `backend error: authkey already used` gives no hint of that, so we say it — otherwise a join
    that succeeded followed by a later failure against an unreachable Head reads as a mesh bug when
    the mesh was fine.
    """
    # Before ANYTHING else: this is the donor's first-ever contact with this coordinator, and
    # tailscaled will refuse the join outright if it doesn't trust the cert — no point
    # installing/starting a daemon first only to fail at the one step that needed this.
    ensure_headscale_trusted(coordinator)
    install_tailscale()
    start_ccc_tailscaled(write_unit)

    if not re_enroll and joined_to(coordinator):
        ip = tailscale_ip()
        ui.say(f"  [muted]already on the mesh at {ip} via {coordinator} — skipping join[/muted]")
        return ip

    # Unconditional: `rm -f` of an absent state file is a no-op, and testing for it would depend on
    # the root-owned state dir staying world-readable.
    why = "--re-enroll" if re_enroll else "not joined to this coordinator"
    ui.say(f"  [muted]starting from a clean mesh identity — {why}[/muted]")
    _wipe_local_state()
    start_ccc_tailscaled(write_unit)

    try:
        join_mesh(coordinator, node_name, authkey)
    except subprocess.CalledProcessError:
        raise ui.abort(
            f"could not join the mesh at {coordinator}.\n"
            f"  If this says 'authkey already used', the token is SPENT — its preauth key is\n"
            f"  single-use, so a box that redeemed it once can never redeem it again. Retrying\n"
            f"  will not help; get a fresh one. On the Head:\n"
            f"      cuttle arm remove {node_name}\n"
            f"      cuttle arm authorize {node_name} --user <owner>\n"
            f"  then here:\n"
            f"      cuttle setup --arm --re-enroll --token <the new blob>")
    return tailscale_ip()


def forget_state() -> None:
    """Wipe this node's CCC mesh identity so it can register against a *different* coordinator.

    Required, not hygiene: on start, tailscaled auto-reconnects to the control URL in its state
    file. That successful noise dial makes the dialer force port 443 for any subsequently-supplied
    login server, so `tailscale up --login-server <new>` hangs forever retrying HTTPS against a
    plain-HTTP Headscale. A daemon with no prior control URL registers over HTTP:8080 happily.

    Deletes the Headscale node too, so the address it held is released and we reclaim it on re-join
    (verified: Headscale frees the IP on node delete). Head-only: an Arm has no `headscale` binary,
    and uses _wipe_local_state() alone — the Head reaps its node in `cuttle arm remove`.
    """
    run(["systemctl", "stop", config.CCC_TS_SERVICE], sudo=True, check=False)
    delete_node(hostname())  # before the state wipe: the node is named after this host
    run(["rm", "-f", str(config.CCC_TS_STATE)], sudo=True, check=False)
