"""Mesh layer (Phase 1 substrate).

Self-hosted **Headscale** coordinator on the Head + **Tailscale** client on every node. The Head
runs its own control server; all nodes (including the co-located Arm) join that tailnet. Arms
enroll with a preauth key minted by the Head under the CCC user.

Intended path only — no fallbacks (test box; failures decided live).
"""

import json
import shutil
import subprocess
import sys
import time
import urllib.request

from . import config
from .system import hostname, run, sh


# --- install ---------------------------------------------------------------

def _release(url: str) -> tuple[str, str]:
    """(version_tag, amd64_deb_url) for a Headscale GitHub release."""
    with urllib.request.urlopen(url) as r:
        rel = json.load(r)
    deb = next(a["browser_download_url"] for a in rel["assets"]
               if a["name"].endswith("_linux_amd64.deb"))
    return rel["tag_name"], deb


def _latest_headscale() -> tuple[str, str]:
    return _release(config.HEADSCALE_RELEASES)


def _install_deb(url: str, reinstall: bool = False) -> None:
    """--force-confmiss restores conffiles the admin deleted (dpkg won't, by default)."""
    opts = "--reinstall " if reinstall else ""
    sh(f"curl -fsSL -o /tmp/headscale.deb {url} && "
       f'apt-get install -y {opts}-o Dpkg::Options::=--force-confmiss /tmp/headscale.deb', sudo=True)


def installed_headscale() -> str | None:
    """Version tag of the installed Headscale, or None. (`headscale version` prints a paragraph;
    we want the `vX.Y.Z` off the first line, to match the GitHub release tag we record.)"""
    if not shutil.which("headscale"):
        return None
    r = run(["headscale", "version"], capture_output=True, text=True, check=False)
    first = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    return first.split()[-1] if first else None


def install_headscale() -> str:
    """Install the latest Headscale .deb (not carried in apt). Returns the version tag.

    Skipped if already installed: `_latest_headscale()` tracks upstream, so re-running setup would
    otherwise upgrade Headscale and migrate its database — the database holding every node's mesh IP
    — as a side effect of, say, changing the coordinator address. Upgrade deliberately, not
    incidentally.
    """
    if (have := installed_headscale()) is not None:
        # `teardown --keep-packages` removes /etc/headscale but leaves the package, so a re-setup
        # finds the binary and no config, then seds a file that isn't there. Re-fetch the deb for
        # the version that is *already installed* — never `_latest_headscale()` here, or a missing
        # config would silently upgrade Headscale and migrate the node database.
        # (`apt-get install --reinstall headscale` can't work: the deb came from a URL, not a repo.)
        if not config.HEADSCALE_CONF.exists():
            print(f"headscale {have} installed but {config.HEADSCALE_CONF} is missing — "
                  f"re-fetching {have} to restore the shipped config")
            _install_deb(config.HEADSCALE_DEB.format(tag=have, ver=have.lstrip("v")), reinstall=True)
        else:
            print(f"headscale already installed ({have}) — skipping (upgrade deliberately)")
        return have
    tag, url = _latest_headscale()
    print(f"headscale: installing {tag}")
    _install_deb(url)
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
        run(["systemctl", "status", config.CCC_TS_SERVICE, "--no-pager", "-l"],
            sudo=True, check=False)
        raise RuntimeError(f"{config.CCC_TS_SERVICE} died on startup — see status above")


# --- Headscale coordinator (Head only) -------------------------------------

_POLICY = """\
{
  // Approved CCC nodes reach each other on every port. `cuttle arm approve` is the trust gate;
  // this file must not pretend to be a second one. See docs/TODO.md — mesh membership already
  // means full cluster trust (an Arm can fetch the munge key), so a port list here would buy no
  // security while breaking srun's ephemeral task-I/O ports and Arm<->Arm traffic in multi-node
  // jobs. Scoped to the `ccc` user, so it grants nothing to nodes outside this mesh.
  // The trailing `@` marks these as usernames. A bare name is parsed as a *host* alias, and
  // headscale refuses to start: `host not defined in policy: "ccc"`.
  "acls": [
    { "action": "accept", "src": ["%(user)s@"], "dst": ["%(user)s@:*"] }
  ]
}
""" % {"user": config.HEADSCALE_USER}


def configure_headscale(server_url: str, write_root_file) -> None:
    """Patch the package-shipped config: server_url, bind-all, the CCC IP prefixes, and the policy.

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
    # magic_dns/override_local_dns OFF: a node accepting them claims resolved's `~.` default-route
    # domain and hijacks host-wide DNS. Nodes address each other by mesh IP; MagicDNS is unwanted.
    sh(f"sed -ri "
       f"-e 's#^server_url:.*#server_url: {server_url}#' "
       f"-e 's#^listen_addr:.*#listen_addr: 0.0.0.0:{config.HEADSCALE_PORT}#' "
       f"-e 's#^([[:space:]]*)v4:.*#\\1v4: {config.MESH_V4_PREFIX}#' "
       f"-e 's#^([[:space:]]*)v6:.*#\\1v6: {config.MESH_V6_PREFIX}#' "
       f"-e 's#^([[:space:]]*)magic_dns:.*#\\1magic_dns: false#' "
       f"-e 's#^([[:space:]]*)override_local_dns:.*#\\1override_local_dns: false#' "
       f"-e '/^policy:/,/^[a-z_]*:/ s#^([[:space:]]*)path:.*#\\1path: {config.HEADSCALE_POLICY}#' "
       f"{config.HEADSCALE_CONF}", sudo=True)


def start_headscale() -> None:
    run(["systemctl", "enable", "headscale"], sudo=True)
    run(["systemctl", "restart", "headscale"], sudo=True)


def ensure_user() -> None:
    """Create the Headscale user/namespace Arms enroll under (idempotent — ignore 'exists')."""
    run(["headscale", "users", "create", config.HEADSCALE_USER], sudo=True, check=False)


def _user_id() -> str:
    """Numeric id of the CCC Headscale user (recent headscale wants id, not name, for keys)."""
    r = run(["headscale", "users", "list", "--output", "json"],
            sudo=True, capture_output=True, text=True)
    for u in json.loads(r.stdout):
        if u["name"] == config.HEADSCALE_USER:
            return str(u["id"])
    raise RuntimeError(f"headscale user {config.HEADSCALE_USER!r} not found")


def mint_preauthkey(reusable: bool = False, hours: int = 1) -> str:
    """Mint a preauth key under the CCC user (goes into the Arm join token)."""
    args = ["headscale", "preauthkeys", "create", "--user", _user_id(),
            "--expiration", f"{hours}h", "--output", "json"]
    if reusable:
        args.append("--reusable")
    r = run(args, sudo=True, capture_output=True, text=True)
    return json.loads(r.stdout)["key"]


# --- Tailscale client (every node) -----------------------------------------

def join_mesh(login_server: str, hostname: str, authkey: str | None = None) -> None:
    """`tailscale up` on CCC's isolated instance (targeted via --socket). `--reset` only affects
    CCC's own instance; the personal daemon is untouched.

    With `authkey`: unattended join. Only the Head does this — it registers against its own
    Headscale, and there is no operator to ask.

    Without: **this call blocks**, printing an auth URL containing an auth-id, until an operator runs
    `cuttle arm approve`. That wait is the cluster's front door. No secret is involved: the auth-id
    names a pending request from this specific machine, and only the Head operator can grant it.

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
    """
    cmd = ["sudo", "tailscale", f"--socket={config.CCC_TS_SOCKET}", "up", "--reset",
           "--login-server", login_server, "--hostname", hostname,
           "--accept-dns=false", "--accept-routes=false", "--netfilter-mode=off"]
    shown = " ".join(cmd)
    if authkey:  # never echo the key
        cmd += ["--authkey", authkey]
        shown += " --authkey <redacted>"
    print(f"$ {shown}", file=sys.stderr)
    subprocess.run(cmd, check=True)  # blocks here awaiting approval when authkey is None


def tailscale_ip() -> str:
    """CCC mesh IP (from CCC's isolated instance, not the personal daemon)."""
    r = run(["tailscale", f"--socket={config.CCC_TS_SOCKET}", "ip", "-4"],
            capture_output=True, text=True)
    return r.stdout.strip().splitlines()[0]


# --- node removal (Head only) ----------------------------------------------

def delete_node(name: str) -> None:
    """Evict a node from the mesh by its Headscale name (== its Slurm node name; see join_mesh).

    Half of `cuttle arm remove`. Skipping this half leaves a departed donor holding a live mesh
    address — and the mesh is what authorizes `GET /enroll`, i.e. the munge key.
    """
    r = run(["headscale", "nodes", "list", "--output", "json"],
            sudo=True, capture_output=True, text=True)
    for n in json.loads(r.stdout):
        if n.get("given_name") == name or n.get("name") == name:
            run(["headscale", "nodes", "delete", "--identifier", str(n["id"]), "--force"], sudo=True)
            return
    print(f"mesh: no node named {name!r} — nothing to evict")


# --- orchestration ---------------------------------------------------------

def up_head(server_url: str, write_unit) -> tuple[str, str]:
    """Bring up the Head's coordinator and join it via CCC's isolated tailscaled instance.
    `write_unit` writes a root-owned file (setup._write_root_file). Returns (headscale_version, mesh_ip)."""
    hs_ver = install_headscale()
    configure_headscale(server_url, write_unit)
    start_headscale()
    ensure_user()
    install_tailscale()
    start_ccc_tailscaled(write_unit)
    # The Head registers against its own Headscale: nobody to approve it, so it uses a preauth key.
    join_mesh(server_url, hostname(), authkey=mint_preauthkey())
    return hs_ver, tailscale_ip()


def up_arm(coordinator: str, node_name: str, write_unit) -> str:
    """Join an Arm to an existing mesh. Returns its mesh IP.

    BLOCKS inside join_mesh until the Head operator approves this machine. No key is presented —
    admission is a decision someone makes, not a secret someone holds.
    """
    install_tailscale()
    start_ccc_tailscaled(write_unit)
    print("\ncuttle: requesting mesh admission — send the auth-id below to the Head operator,\n"
          f"        who runs:  cuttle arm approve {node_name} --auth-id <id>\n")
    join_mesh(coordinator, node_name)
    return tailscale_ip()


def register_node(auth_id: str) -> None:
    """Grant a pending mesh-admission request. The cluster's front door, opened by hand.

    `--user` takes the user *name* here, unlike `preauthkeys create`, which takes the numeric id
    (see mint_preauthkey). Headscale 0.29 is inconsistent across subcommands; an id here fails with
    `looking up user: user not found`. Don't "fix" the asymmetry — it's load-bearing.
    """
    run(["headscale", "auth", "register", "--auth-id", auth_id,
         "--user", config.HEADSCALE_USER], sudo=True)


def forget_state() -> None:
    """Wipe this node's CCC mesh identity so it can register against a *different* coordinator.

    Required, not hygiene: on start, tailscaled auto-reconnects to the control URL in its state
    file. That successful noise dial makes the dialer force port 443 for any subsequently-supplied
    login server, so `tailscale up --login-server <new>` hangs forever retrying HTTPS against a
    plain-HTTP Headscale. A daemon with no prior control URL registers over HTTP:8080 happily.

    Deletes the Headscale node too, so the address it held is released and we reclaim it on re-join
    (verified: Headscale frees the IP on node delete).
    """
    run(["systemctl", "stop", config.CCC_TS_SERVICE], sudo=True, check=False)
    delete_node(hostname())  # before the state wipe: the node is named after this host
    run(["rm", "-f", str(config.CCC_TS_STATE)], sudo=True, check=False)
