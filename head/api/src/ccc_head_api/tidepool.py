"""Tidepool, Head API side: a User's status, their SSH keys, and restart/reset of their own.

The Tidepool itself (image, mount, container, unit) belongs to the root helper `ccc-tidepool`
(cuttle/ccc_tidepool.py). This process runs as `ccc` and touches it three ways only:
- **reads** need no privilege: unit state via `systemctl show`, usage via statvfs on the mountpoint;
- **keys**: head.db is the source; `authorized_keys` is regenerated from it into the User's keys dir
  (ccc-owned, mounted read-only into the container) on every change and every status read;
- **restart|reset**: `sudo -n ccc-tidepool <verb> <user>` — one sudoers rule, and the routes only
  ever pass the logged-in User's own name.

Constants duplicated from cuttle.config (the two packages do not import each other).
"""

import json
import os
import re
import subprocess
from pathlib import Path

from . import db

SETTINGS = Path("/etc/ccc/tidepools.json")
MNT_ROOT = Path("/var/lib/ccc/tidepools")
KEYS_ROOT = Path("/var/lib/ccc/head/tidepool-keys")
HELPER = "/usr/local/sbin/ccc-tidepool"
SSH_PORT_BASE = 32000
UID_FLOOR = 60000
CLUSTER_NAME_FILE = Path("/etc/ccc/cluster-name")   # config.CLUSTER_NAME_FILE


def cluster_name() -> str:
    """The **Cluster name** (setup --head): domain `ccc.<name>`, Tidepool alias `<name>`."""
    return CLUSTER_NAME_FILE.read_text().strip()

# One OpenSSH public key line: type, base64 blob, optional comment. No options prefix (from=, command=):
# the line goes into authorized_keys verbatim.
PUBKEY_RE = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|"
                       r"sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) "
                       r"[A-Za-z0-9+/]+={0,3}( [^\r\n]{0,200})?$")


def ssh_port(uid: int) -> int:
    return SSH_PORT_BASE + uid - UID_FLOOR


def size_gb() -> int | None:
    try:
        return json.loads(SETTINGS.read_text())["size_gb"]
    except (OSError, ValueError, KeyError):
        return None


def state(username: str) -> str:
    r = subprocess.run(["systemctl", "show", "-p", "ActiveState", "--value",
                        f"ccc-tidepool@{username}.service"], capture_output=True, text=True)
    return r.stdout.strip() or "unknown"


def used_gb(username: str) -> float | None:
    p = MNT_ROOT / username
    if not os.path.ismount(p):
        return None
    st = os.statvfs(p)
    return (st.f_blocks - st.f_bfree) * st.f_frsize / 2**30


def host_key(username: str) -> str | None:
    try:
        return " ".join((KEYS_ROOT / username / "host_ed25519.pub").read_text().split()[:2])
    except OSError:
        return None


def sync_keys(conn, user_id: int, username: str) -> None:
    """Rewrite the User's CCC authorized_keys from head.db (atomic: tmp + rename in the same dir).
    The dir is the helper's; before it exists there is no Tidepool to log into, so nothing to write."""
    d = KEYS_ROOT / username
    if not d.is_dir():
        return
    lines = "".join(k["pubkey"] + "\n" for k in db.ssh_keys(conn, user_id))
    tmp = d / ".authorized_keys.tmp"
    tmp.write_text(lines)
    tmp.chmod(0o644)
    os.replace(tmp, d / "authorized_keys")


SESSION_RENEW_DAYS = 7


def sync_session(conn, user_id: int, username: str) -> None:
    """The Tidepool's **ambient session**: a narrow token (`scope=tidepool`, main.current_user) in the
    keys dir, so `cuttle` inside works with no login. Re-minted when missing (the helper deletes it at
    every Tidepool start), unknown, or within SESSION_RENEW_DAYS of expiry; the old one is revoked.
    0640 in a setgid dir of the User's mapped group: readable inside by the User only."""
    d = KEYS_ROOT / username
    if not d.is_dir():
        return
    f = d / "session"
    try:
        if db.session_fresh(conn, f.read_text().strip(), SESSION_RENEW_DAYS):
            return
    except OSError:
        pass
    db.delete_tidepool_sessions(conn, user_id)
    tmp = d / ".session.tmp"
    tmp.write_text(db.create_session(conn, user_id, scope="tidepool") + "\n")
    tmp.chmod(0o640)
    os.replace(tmp, f)


def sync_sessions(conn) -> None:
    """Every tick: each User's Tidepool session exists and is fresh."""
    for uid, name in conn.execute("SELECT id, username FROM users").fetchall():
        sync_session(conn, uid, name)


def act(verb: str, username: str) -> None:
    subprocess.run(["sudo", "-n", HELPER, verb, username], check=True, capture_output=True, text=True)
