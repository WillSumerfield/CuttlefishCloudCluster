#!/usr/bin/python3 -I
"""ccc-tidepool — create, run and remove a User's **Tidepool** on the Head, as root.

Installed root-owned at /usr/local/sbin/ccc-tidepool on the Head by `cuttle setup --head`.
- `create <user> <uid>`: the operator, through `cuttle user add` / `setup --head` (reconcile, idempotent).
- `restart|reset <user>`: the Head API (as `ccc`) via one sudoers rule, for the logged-in User only;
  or the operator for anyone.
- `remove <user>`, `teardown [--all]`: the operator (`cuttle user remove`, `cuttle teardown`).
- `dive-prep|dive-snap|dive-open <user> <token>`, `dive-close <token>`: the Head API, for a **Dive**
  (see the Dives section below). `dive-lock <token>`: rsyncd (pre-xfer exec). `started <user>`: the
  unit's ExecStartPost. `replay <user>`: systemd-run, after a Dive's end or a rebuild (apt replay).

A Tidepool is:
- **one fixed-size ext4 image** `<image_dir>/<user>.img` (fallocate — the space is taken up front),
  loop-mounted at MNT_ROOT/<user> by a systemd mount unit. Inside: `home/` (→ /home/<user>),
  `storage/` (the User's own Podman store: the container's writable layer, so apt installs count
  against the image and a full image is ENOSPC for that User alone), `ssh/` (host key).
- **one long-lived container** `tp-<user>`, created once and run by `ccc-tidepool@<user>.service`
  (`podman start --attach`). Not Quadlet: its `run --rm --replace` would drop the apt layer on every
  restart. The base image is read from the main store (additionalimagestore), never copied.
- container root = an unprivileged host id range (`id_base`, same formula as ccc-launch); the User is
  uid 1000 inside with passwordless sudo. No GPU, no host namespace, never `--privileged`.
- its own isolated bridge (`isolate=true` — no Tidepool reaches another's container IP), sshd
  published on SSH_PORT_BASE + (uid - UID_FLOOR).

Stdlib only, no CCC imports: it runs under the system python as root.
"""

import base64
import fcntl
import gzip
import hashlib
import json
import os
import pwd
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SETTINGS = Path("/etc/ccc/tidepools.json")      # config.TIDEPOOL_SETTINGS
IMAGES_RECORD = Path("/opt/ccc/images.json")    # config.IMAGES_RECORD
AUTH_FILE = "/etc/ccc/registry-auth.json"       # config.REGISTRY_AUTH (the Head's push credential)
MNT_ROOT = Path("/var/lib/ccc/tidepools")       # config.TIDEPOOL_MNT_ROOT
KEYS_ROOT = Path("/var/lib/ccc/head/tidepool-keys")  # config.TIDEPOOL_KEYS_ROOT
FILES_DIR = Path("/usr/local/lib/ccc/tidepool")  # config.TIDEPOOL_FILES_DIR
CUTTLE_ENV = Path("/usr/local/lib/ccc/cuttle")    # config.CONTAINER_CUTTLE
MESH_IFACE = "ccc0"                               # config.CCC_TS_TUN
RECORDS = Path("/etc/ccc/tidepools.d")          # config.TIDEPOOL_RECORDS
KEPT = Path("/etc/ccc-tidepools-kept.json")     # config.TIDEPOOL_KEPT
UNIT_DIR = Path("/etc/systemd/system")
MAIN_STORE = "/var/lib/containers/storage"
SLICE = "ccc-tidepools.slice"                   # config.TIDEPOOL_SLICE
SERVICE = "ccc-tidepool"                        # config.TIDEPOOL_SERVICE
API_USER = "ccc"                                # config.HEAD_API_USER

ID_BASE = 2**31
ID_COUNT = 65536
UID_FLOOR = 60000     # config.CCC_UID
UID_CEIL = 61182      # config.GUISE_UID_MAX
SSH_PORT_BASE = 32000  # config.TIDEPOOL_SSH_PORT_BASE
INNER_UID = 1000

USER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")  # ccc_head_api.admin.USERNAME_RE
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")

# Bumped when create_argv changes in a way an existing container must be recreated for (home kept).
SPEC_VERSION = "4"


class Refused(Exception):
    pass


def id_base(uid: int) -> int:
    if not UID_FLOOR < uid <= UID_CEIL:
        raise Refused(f"uid {uid} is not a CCC User")
    return ID_BASE + (uid - UID_FLOOR) * ID_COUNT


def ssh_port(uid: int) -> int:
    return SSH_PORT_BASE + uid - UID_FLOOR


def check_user(user: str) -> str:
    if not USER_RE.match(user):
        raise Refused(f"{user!r} is not a CCC username")
    return user


def mnt(user: str) -> Path:
    return MNT_ROOT / user


def mount_unit(user: str) -> str:
    """systemd-escape --path --suffix=mount, for a path whose only odd character can be '-'."""
    return str(mnt(user)).strip("/").replace("-", "\\x2d").replace("/", "-") + ".mount"


def service(user: str) -> str:
    return f"{SERVICE}@{user}.service"


def container(user: str) -> str:
    return f"tp-{user}"


def network(user: str) -> str:
    return f"ccc-tp-{user}"


STORE_MARK = ".store-v2"   # beside storage/: this store was made with podman()'s --tmpdir


def podman(user: str) -> list[str]:
    """Podman pointed at this User's store (inside their image), the base read from the main store."""
    # --tmpdir: its own reboot marker (`alive`). Shared with the main store's /run/libpod, the main
    # podman's post-boot refresh marks it done, this store never refreshes, and every start after a
    # reboot fails on run dirs the reboot wiped.
    return ["podman", "--root", str(mnt(user) / "storage"), "--runroot", f"/run/ccc-tidepools/{user}",
            "--tmpdir", f"/run/ccc-tidepools/{user}/libpod",
            "--storage-opt", f"additionalimagestore={MAIN_STORE}"]


def render_mount(user: str, image: Path) -> str:
    return (f"# Written by ccc-tidepool. Do not hand-edit.\n[Unit]\nDescription=CCC Tidepool image ({user})\n\n"
            f"[Mount]\nWhat={image}\nWhere={mnt(user)}\nType=ext4\nOptions=loop\n")


def mesh_ip() -> str:
    """The Head's mesh address: what `ccc.<name>` means inside a Tidepool (the API listens on all)."""
    out = subprocess.run(["/usr/sbin/ip", "-4", "-o", "addr", "show", "dev", MESH_IFACE],
                         capture_output=True, text=True).stdout
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", out)
    if not m:
        raise Refused(f"no mesh address on {MESH_IFACE}")
    return m[1]


def create_argv(user: str, uid: int, s: dict, base_ref: str, head_ip: str) -> list[str]:
    b = id_base(uid)
    m = mnt(user)
    return [*podman(user), "create", "--pull=never", "--name", container(user), "--hostname", "tidepool",
            "--label", f"ccc.tidepool.spec={SPEC_VERSION}",
            "--init", "--uidmap", f"0:{b}:{ID_COUNT}", "--gidmap", f"0:{b}:{ID_COUNT}",
            "--network", network(user), "-p", f"{ssh_port(uid)}:22",
            "--cpus", str(s["cpus"]), "--memory", f"{s['mem_gb']}g", "--memory-swap", f"{s['mem_gb']}g",
            "--cgroup-parent", SLICE, "--label", f"ccc.tidepool={user}",
            "-e", f"CCC_USER={user}",
            "-v", f"{m / 'home'}:/home/{user}",
            "-v", f"{m / 'ssh'}:/etc/ccc/hostkeys:ro",
            "-v", f"{KEYS_ROOT / user}:/etc/ccc/keys:ro",
            "-v", f"{FILES_DIR}:/etc/ccc/tidepool:ro",
            # `cuttle` inside, and what it needs to reach the API with the ambient session in keys/
            "-v", f"{CUTTLE_ENV}:{CUTTLE_ENV}:ro",
            "-v", f"{CLUSTER_NAME_FILE}:/etc/ccc/cluster-name:ro",
            "-v", f"{CA_CERT}:/etc/ccc/ca.crt:ro",
            "--add-host", f"ccc.{CLUSTER_NAME_FILE.read_text().strip()}:{head_ip}",
            base_ref, "/etc/ccc/tidepool/init"]


# --- side effects --------------------------------------------------------------

def sh(*argv, check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), check=check, **kw)


def ok(*argv) -> bool:
    return subprocess.run(list(argv), capture_output=True).returncode == 0


def settings() -> dict:
    return json.loads(SETTINGS.read_text())


def record(user: str) -> dict:
    p = RECORDS / f"{user}.json"
    if not p.exists():
        raise Refused(f"{user} has no Tidepool")
    return json.loads(p.read_text())


def image_path(s: dict, user: str) -> Path:
    return Path(s["image_dir"]) / f"{user}.img"


def free_bytes(path: Path) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def ensure_image(user: str, s: dict) -> Path:
    img = image_path(s, user)
    if img.exists():
        # Re-fill any holes at its own size: an image made before mkfs was told not to discard is
        # sparse, and only takes host disk as the User writes — the up-front reservation is the point.
        sh("fallocate", "-l", str(img.stat().st_size), str(img))
        return img
    img.parent.mkdir(parents=True, exist_ok=True)
    want = s["size_gb"] * 2**30
    if free_bytes(img.parent) < want:
        raise Refused(f"{img.parent} has {free_bytes(img.parent) // 2**30} GB free — not enough for "
                      f"another {s['size_gb']} GB Tidepool")
    sh("fallocate", "-l", str(want), str(img))
    img.chmod(0o600)
    # nodiscard: mke2fs otherwise punches out every block of a regular file, undoing the fallocate.
    sh("mkfs.ext4", "-q", "-F", "-m", "0", "-E", "nodiscard", "-L", f"tp-{user}"[:16], str(img))
    return img


def ensure_mount(user: str, img: Path) -> None:
    unit = UNIT_DIR / mount_unit(user)
    unit.write_text(render_mount(user, img))
    sh("systemctl", "daemon-reload")
    sh("systemctl", "start", mount_unit(user))


def ensure_dirs(user: str, uid: int) -> None:
    """home/ (the User, uid 1000 inside), storage/ (root), ssh/ (container root, host key), and the
    keys dir the Head API writes authorized_keys + the ambient session into (`ccc`, read-only in the
    container). Group = the User's mapped gid, setgid, 2751: the session (0640) is readable inside by
    the User and by no other host account; authorized_keys (0644) still by the container's sshd."""
    b, m = id_base(uid), mnt(user)
    # A store made before --tmpdir: podman refuses it under the new one (db config mismatch), so it is
    # dropped once — its only content is the container's own layer (apt installs); home is kept.
    if (m / "storage").exists() and not (m / STORE_MARK).exists():
        sh("systemctl", "stop", service(user), check=False, capture_output=True)
        shutil.rmtree(m / "storage")
    (m / STORE_MARK).touch()
    for d, owner, mode in ((m / "home", b + INNER_UID, 0o750), (m / "storage", 0, 0o700),
                           (m / "ssh", b, 0o700)):
        if not d.exists():
            d.mkdir(mode=mode)
            os.chown(d, owner, owner)
    key = m / "ssh" / "ssh_host_ed25519_key"
    if not key.exists():
        sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"tidepool-{user}", "-f", str(key))
        for p in (key, key.with_suffix(".pub")):
            os.chown(p, b, b)
    keys = KEYS_ROOT / user
    keys.mkdir(parents=True, exist_ok=True)
    os.chown(keys, pwd.getpwnam(API_USER).pw_uid, b + INNER_UID)
    keys.chmod(0o2751)
    shutil.copyfile(key.with_suffix(".pub"), keys / "host_ed25519.pub")
    Path(f"/run/ccc-tidepools/{user}").mkdir(parents=True, exist_ok=True, mode=0o700)


def ensure_network(user: str) -> None:
    if not ok("podman", "network", "exists", network(user)):
        sh("podman", "network", "create", "--opt", "isolate=true", network(user),
           capture_output=True)


def _container_image(user: str) -> str | None:
    """`<image ref> <spec label>` of the existing container, or None."""
    r = subprocess.run([*podman(user), "container", "inspect", "--format",
                        '{{.ImageName}} {{index .Config.Labels "ccc.tidepool.spec"}}',
                        container(user)], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def ensure_container(user: str, uid: int, s: dict) -> None:
    base = json.loads(IMAGES_RECORD.read_text())["base"]
    have = _container_image(user)
    if have == f"{base} {SPEC_VERSION}":
        # caps may have changed at a setup re-run; live, no restart
        sh(*podman(user), "update", "--cpus", str(s["cpus"]), "--memory", f"{s['mem_gb']}g",
           "--memory-swap", f"{s['mem_gb']}g", container(user), check=False, capture_output=True)
        return
    # `have` is None also for a container whose image a release wipe deleted (inspect fails on it), so
    # existence is asked separately — else `create` hits the name it still holds.
    if have is not None or ok(*podman(user), "container", "exists", container(user)):
        # a new release's base (setup --head wiped the old one), or a new create spec: the old layer
        # cannot outlive it — recreated, home kept, like a reset
        sh("systemctl", "stop", service(user), check=False, capture_output=True)
        queue_rebuild(user)
        sh(*podman(user), "rm", "-f", "-t", "5", container(user), check=False, capture_output=True)
    # The digest ref is the pushed manifest's; a locally built image may not answer to it until pulled
    # once into the main store (layers already there — only the manifest moves). Never into the User's.
    if not ok("podman", "image", "exists", base):
        sh("podman", "pull", "-q", "--authfile", AUTH_FILE, base, capture_output=True)
    sh(*create_argv(user, uid, s, base, mesh_ip()), capture_output=True)


def create(user: str, uid: int) -> None:
    check_user(user)
    id_base(uid)
    s = settings()
    RECORDS.mkdir(parents=True, exist_ok=True)
    (RECORDS / f"{user}.json").write_text(json.dumps({"uid": uid}) + "\n")
    img = ensure_image(user, s)
    ensure_mount(user, img)
    ensure_dirs(user, uid)
    ensure_network(user)
    ensure_container(user, uid, s)
    sh("systemctl", "enable", "--now", service(user), capture_output=True)
    print(f"tidepool {user}: {service(user)} up, ssh port {ssh_port(uid)}")


def restart(user: str) -> None:
    record(check_user(user))
    sh("systemctl", "restart", service(user))


def reset(user: str) -> None:
    """A fresh container from the base: the apt layer goes (its replay list too), home stays."""
    uid = record(check_user(user))["uid"]
    sh("systemctl", "stop", service(user))
    shutil.rmtree(APT_PENDING / user / "rebuild", ignore_errors=True)
    sh(*podman(user), "rm", "-f", "-t", "5", container(user), check=False, capture_output=True)
    ensure_container(user, uid, settings())   # nothing there now → created from the current base
    sh("systemctl", "start", service(user))


def _stop(user: str, drop_container: bool) -> None:
    sh("systemctl", "disable", "--now", service(user), check=False, capture_output=True)
    if drop_container:
        sh(*podman(user), "rm", "-f", "-t", "5", container(user), check=False, capture_output=True)
    sh("podman", "network", "rm", "-f", network(user), check=False, capture_output=True)
    sh("systemctl", "stop", mount_unit(user), check=False, capture_output=True)
    (UNIT_DIR / mount_unit(user)).unlink(missing_ok=True)
    shutil.rmtree(KEYS_ROOT / user, ignore_errors=True)
    shutil.rmtree(f"/run/ccc-tidepools/{user}", ignore_errors=True)
    shutil.rmtree(APT_PENDING / user, ignore_errors=True)
    (RECORDS / f"{user}.json").unlink(missing_ok=True)
    if mnt(user).is_dir() and not os.path.ismount(mnt(user)):
        mnt(user).rmdir()


def remove(user: str) -> None:
    """Everything, the image included — `cuttle user remove` asked for the username typed first."""
    check_user(user)
    s = settings()
    _stop(user, drop_container=True)
    if os.path.ismount(mnt(user)):
        raise Refused(f"{mnt(user)} is still mounted — image kept")
    image_path(s, user).unlink(missing_ok=True)
    sh("systemctl", "daemon-reload")
    print(f"tidepool {user}: removed")


def users() -> list[str]:
    return sorted(p.stem for p in RECORDS.glob("*.json")) if RECORDS.is_dir() else []


def teardown(all_: bool) -> None:
    """Every Tidepool down and unmounted. Images kept unless `all_`; kept ones are listed in KEPT
    (added to any earlier list), which a later `teardown --all` deletes even once this helper is gone."""
    s = settings() if SETTINGS.exists() else None
    kept = json.loads(KEPT.read_text()) if KEPT.exists() else []
    for user in users():
        _stop(user, drop_container=all_)   # a kept image keeps its container + apt layer too
        if s and not os.path.ismount(mnt(user)):
            img = str(image_path(s, user))
            if all_:
                Path(img).unlink(missing_ok=True)
            elif img not in kept:
                kept.append(img)
    if all_:
        for img in kept:
            Path(img).unlink(missing_ok=True)
        KEPT.unlink(missing_ok=True)
    elif kept:
        KEPT.write_text(json.dumps(kept))
        KEPT.chmod(0o644)
    for d in (DIVE_RECORDS, DIVE_RSYNCD, DIVE_STATE):
        shutil.rmtree(d, ignore_errors=True)
    sh("systemctl", "daemon-reload", check=False)

# --- Dives ---------------------------------------------------------------------
# A Dive is one GPU session. Everything here is keyed by a token the Head API mints per request (the
# Slurm job id does not exist yet when the snapshot is pushed and the rsync modules open).
#
# - `dive-prep <user> <tok>` (stdin {main, deps}): checks the paths, copies the dotfile allowlist into
#   DIVE_STATE/<tok>/home, and prints sizes (real staging totals, excludes applied) + cuttle.toml.
# - `dive-snap <user> <tok>`: commits the Tidepool container and pushes it — only the User's layer
#   travels; the base is already in the registry. Prints the digest ref. The commit records the
#   container's host ids (id_base+n), so the pushed layer is rewritten with them shifted back to n.
# - `dive-open <user> <tok>` (stdin {main, deps, toml, secret, image}): writes cuttle.toml back, the
#   record, and the per-Dive rsync modules (root-owned dir rsyncd &includes): main writable, deps and
#   the dotfile/host-key bundle read-only. Modules run as the User's mapped uid, so the files are uid
#   1000 inside on both ends (same id_base everywhere) and nothing else on the Head is reachable.
# - `dive-lock <tok>`: the main module's pre-xfer exec (rsyncd runs it as root) — the first stage-in
#   makes the main folder read-only inside the Tidepool container (a bind over itself; the host path
#   stays writable for the mirror). Soft: `sudo umount` inside undoes it, deliberately.
# - `dive-close <tok>`: the Head API's tick once the job has left the queue — unlock, modules
#   gone, the pushed snapshot's manifest deleted (the nightly registry-gc frees its blobs).

DIVE_RECORDS = Path("/etc/ccc/dives")             # config.DIVE_RECORDS — one <tok>.json per Dive
DIVE_RSYNCD = Path("/etc/ccc/rsyncd-dives.d")     # config.DIVE_RSYNCD — &included by the rsyncd conf
DIVE_STATE = Path("/var/lib/ccc/dives")           # config.DIVE_STATE — <tok>/{home,sys} bundle
CLUSTER_NAME_FILE = Path("/etc/ccc/cluster-name")  # config.CLUSTER_NAME_FILE
REGISTRY_PORT = 5000                              # config.REGISTRY_PORT
CA_CERT = "/opt/ccc/tls/ccc-ca.crt"               # config.HEAD_CA_CERT
HELPER = "/usr/local/sbin/ccc-tidepool"           # config.TIDEPOOL_HELPER_BIN


def registry() -> str:
    """config.registry(): `ccc.<cluster name>:5000`."""
    return f"ccc.{CLUSTER_NAME_FILE.read_text().strip()}:{REGISTRY_PORT}"


DOTFILE_CAP = 2**30
# The dotfile allowlist, as rsync rules (plan: Dives). Never private keys or credential files.
DOTFILE_RULES = [
    *(f"+ /{f}" for f in (".bashrc", ".bash_profile", ".profile", ".zshrc", ".zshenv", ".zprofile",
                          ".p10k.zsh", ".inputrc", ".gitconfig", ".tmux.conf", ".vimrc", ".condarc")),
    *(f"+ /{d}/***" for d in (".oh-my-zsh", ".vim")),
    "+ /.config/", *(f"+ /.config/{d}/***" for d in ("git", "tmux", "nvim", "htop")),
    "+ /.ssh/", "+ /.ssh/config", "+ /.ssh/known_hosts", "+ /.ssh/*.pub",
    "- *",
]
# Never staged, never counted, never touched by the mirror's --delete. Plus each folder's .cuttleignore.
DENYLIST = (".venv", "venv", "__pycache__", ".cache", "node_modules", ".mypy_cache", ".pytest_cache",
            ".ipynb_checkpoints")
FILTER_ARGS = [*(f"--exclude={d}" for d in DENYLIST), "--filter=:- .cuttleignore"]
REL_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._ @+-]*(/[A-Za-z0-9._][A-Za-z0-9._ @+-]*)*$")


def check_token(tok: str) -> str:
    if not TOKEN_RE.match(tok):
        raise Refused(f"{tok!r} is not a Dive token")
    return tok


def dive_record_path(tok: str) -> Path:
    return DIVE_RECORDS / f"{check_token(tok)}.json"


def dive_record(tok: str) -> dict:
    p = dive_record_path(tok)
    if not p.exists():
        raise Refused(f"no Dive {tok}")
    return json.loads(p.read_text())


def save_dive(tok: str, d: dict) -> None:
    DIVE_RECORDS.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = dive_record_path(tok).with_suffix(".tmp")
    tmp.write_text(json.dumps(d, sort_keys=True) + "\n")
    os.replace(tmp, dive_record_path(tok))


def dives(user: str | None = None) -> list[dict]:
    if not DIVE_RECORDS.is_dir():
        return []
    out = [json.loads(p.read_text()) for p in sorted(DIVE_RECORDS.glob("*.json"))]
    return [d for d in out if user is None or d["user"] == user]


def check_rel(rel: str) -> str:
    """A folder named relative to the Tidepool home: no absolute paths, no `..`, no odd characters."""
    rel = rel.rstrip("/")
    if not REL_RE.match(rel) or any(p in (".", "..") for p in rel.split("/")):
        raise Refused(f"{rel!r} is not a folder path inside your Tidepool (relative to ~, no '..')")
    return rel


def host_dir(user: str, rel: str) -> Path:
    """The folder's host path, refused if any component is a symlink or it is not a directory —
    it becomes an rsync module root and a bind target, both as root."""
    p = mnt(user) / "home"
    for part in check_rel(rel).split("/"):
        p = p / part
        if p.is_symlink():
            raise Refused(f"~/{rel} goes through a symlink ({part}) — name the real folder")
    if not p.is_dir():
        raise Refused(f"~/{rel} is not a folder in your Tidepool")
    return p


def check_folders(main: str, deps: list[str]) -> tuple[str, list[str]]:
    main, deps = check_rel(main), [check_rel(d) for d in deps]
    allf = [main, *deps]
    if len(set(allf)) != len(allf):
        raise Refused("a folder is named twice (main and dep folders must differ)")
    for a in allf:
        for b in allf:
            if a != b and (b + "/").startswith(a + "/"):
                raise Refused(f"~/{b} is inside ~/{a} — folders cannot nest")
    return main, deps


def as_user(uid: int) -> list[str]:
    """Run as the User's mapped host uid (uid 1000 inside): reads/writes of their tree never as root."""
    u = id_base(uid) + INNER_UID
    return ["setpriv", f"--reuid={u}", f"--regid={u}", "--clear-groups", "--"]


def staged_bytes(uid: int, src: Path) -> int:
    """What rsync would actually stage (excludes applied), via a dry run as the User."""
    r = sh(*as_user(uid), "rsync", "-a", "-n", "--stats", *FILTER_ARGS, f"{src}/", "/nonexistent-ccc/",
           capture_output=True, text=True)
    m = re.search(r"Total file size: ([\d,.]+) bytes", r.stdout)
    return int(re.sub(r"[,.]", "", m[1])) if m else 0


def _tree(root: Path) -> list[tuple[int, str]]:
    out = []
    for d, _, files in os.walk(root):
        for f in files:
            p = Path(d) / f
            out.append((p.lstat().st_size, str(p.relative_to(root))))
    return out


def layer_bytes(user: str) -> int:
    r = sh(*podman(user), "container", "inspect", "--size", "--format", "{{.SizeRw}}", container(user),
           capture_output=True, text=True)
    return int(r.stdout.strip() or 0)


def read_toml(uid: int, main_dir: Path) -> str | None:
    r = subprocess.run([*as_user(uid), "head", "-c", "65536", str(main_dir / "cuttle.toml")],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def dive_prep(user: str, tok: str, req: dict) -> dict:
    uid = record(check_user(user))["uid"]
    main, deps = check_folders(req["main"], req.get("deps") or [])
    folders = {f: staged_bytes(uid, host_dir(user, f)) for f in (main, *deps)}
    bundle = DIVE_STATE / check_token(tok)
    home = bundle / "home"
    shutil.rmtree(bundle, ignore_errors=True)
    bundle.mkdir(parents=True)
    bundle.chmod(0o711)                   # the User's rsync below writes into home/
    u = id_base(uid) + INNER_UID
    home.mkdir()
    os.chown(home, u, u)
    sh(*as_user(uid), "rsync", "-a", "-m", *(f"--filter={r}" for r in DOTFILE_RULES),
       f"{mnt(user) / 'home'}/", f"{home}/", capture_output=True)
    dots = _tree(home)
    return {"main": main, "deps": deps, "folders": folders,
            "dotfiles": sum(b for b, _ in dots), "dotfile_cap": DOTFILE_CAP,
            "dotfiles_biggest": [{"path": p, "bytes": b} for b, p in sorted(dots, reverse=True)[:5]],
            "layer": layer_bytes(user), "toml": read_toml(uid, host_dir(user, main))}


def _auth_header() -> str:
    auth = json.loads(Path(AUTH_FILE).read_text())["auths"][registry()]["auth"]
    return f"Basic {auth}"


def dive_snap(user: str, tok: str) -> dict:
    """Commit + push the User's system layer. The pushed manifest's digest is what the Arm pulls."""
    rec = record(check_user(user))
    check_token(tok)
    ref = f"{registry()}/ccc/u-{user}:{tok}"
    digest_file = DIVE_STATE / f"{tok}.digest"
    DIVE_STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The Dive's own apt entries start where the Tidepool's logs end now (taken before the commit: an
    # install racing it is replayed twice, never missed).
    (DIVE_STATE / tok / "apt-offsets.json").write_text(json.dumps(apt_offsets(user)))
    sh(*podman(user), "commit", "-q", container(user), ref, capture_output=True)
    try:
        sh(*podman(user), "push", "-q", "--authfile", AUTH_FILE, "--digestfile", str(digest_file), ref,
           capture_output=True)
    finally:
        sh(*podman(user), "rmi", "-f", ref, check=False, capture_output=True)
    digest = digest_file.read_text().strip()
    digest_file.unlink()
    return {"image": shift_snapshot(f"ccc/u-{user}", tok, digest, id_base(rec["uid"]))}


MANIFEST_TYPES = "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"


def _reg(method: str, path: str, data=None, headers: dict | None = None):
    """One registry API call. `path` is after /v2/, or an upload Location (absolute or /v2/…)."""
    url = path if path.startswith("https://") else f"https://{registry()}" + (
        path if path.startswith("/") else f"/v2/{path}")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": _auth_header(), **(headers or {})})
    ctx = ssl.create_default_context(cafile=CA_CERT if Path(CA_CERT).exists() else None)
    return urllib.request.urlopen(req, context=ctx, timeout=600)


class _Hashing:
    """A write-through file wrapper counting bytes and their sha256."""
    def __init__(self, f):
        self.f, self.h, self.n = f, hashlib.sha256(), 0

    def write(self, b) -> int:
        self.h.update(b)
        self.n += len(b)
        return self.f.write(b)

    def flush(self) -> None:
        self.f.flush()

    def digest(self) -> str:
        return "sha256:" + self.h.hexdigest()


def _shift(i: int, base: int) -> int:
    return i - base if base <= i < base + ID_COUNT else i


def shift_layer(src, dst, base: int) -> str:
    """Copy layer tar stream `src` to gzip `dst` with ids base..base+ID_COUNT moved down to 0… Returns
    the new uncompressed sha256 (the config's diff_id)."""
    gz = gzip.GzipFile(fileobj=dst, mode="wb", mtime=0)
    raw = _Hashing(gz)
    with tarfile.open(fileobj=src, mode="r|") as tin, \
            tarfile.open(fileobj=raw, mode="w|", format=tarfile.PAX_FORMAT) as tout:
        for ti in tin:
            ti.uid, ti.gid = _shift(ti.uid, base), _shift(ti.gid, base)
            ti.pax_headers.pop("uid", None)       # else the old big id survives in the pax header
            ti.pax_headers.pop("gid", None)
            tout.addfile(ti, tin.extractfile(ti) if ti.isreg() else None)
    gz.close()
    return raw.digest()


def _upload(name: str, f, size: int, digest: str) -> None:
    with _reg("POST", f"{name}/blobs/uploads/") as r:
        loc = r.headers["Location"]
    _reg("PUT", f"{loc}{'&' if '?' in loc else '?'}digest={digest}", data=f,
         headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"}).close()


def shift_snapshot(name: str, tag: str, digest: str, base: int) -> str:
    """Rewrite the pushed snapshot's top (User) layer with container ids, re-tag, drop the old manifest.
    Only that layer moves; the base layers are untouched. Returns the new digest ref."""
    with _reg("GET", f"{name}/manifests/{digest}", headers={"Accept": MANIFEST_TYPES}) as r:
        mtype = r.headers["Content-Type"]
        m = json.loads(r.read())
    with _reg("GET", f"{name}/blobs/{m['config']['digest']}") as r:
        cfg = json.loads(r.read())
    top = m["layers"][-1]
    if not top["mediaType"].endswith("gzip"):
        raise Refused(f"snapshot layer is {top['mediaType']}, not gzip")
    with tempfile.TemporaryFile(dir=DIVE_STATE) as tmp:
        out = _Hashing(tmp)
        with _reg("GET", f"{name}/blobs/{top['digest']}") as r:
            cfg["rootfs"]["diff_ids"][-1] = shift_layer(gzip.GzipFile(fileobj=r), out, base)
        tmp.seek(0)
        _upload(name, tmp, out.n, out.digest())
    top["digest"], top["size"] = out.digest(), out.n
    body = json.dumps(cfg).encode()
    cd = "sha256:" + hashlib.sha256(body).hexdigest()
    _upload(name, body, len(body), cd)
    m["config"]["digest"], m["config"]["size"] = cd, len(body)
    mbody = json.dumps(m).encode()
    _reg("PUT", f"{name}/manifests/{tag}", data=mbody, headers={"Content-Type": mtype}).close()
    new = "sha256:" + hashlib.sha256(mbody).hexdigest()
    delete_manifest(f"{registry()}/{name}@{digest}")
    return f"{registry()}/{name}@{new}"


def render_modules(tok: str, d: dict) -> str:
    """User-data modules run as the User's mapped uid with symlinks left as they are (munging would
    break every symlink in a project; an unprivileged uid reaches nothing a symlink could point at
    that the User cannot already read). Only `-sys` (host key + keys, files we wrote) runs as root,
    and keeps rsync's default symlink munging."""
    u = id_base(d["uid"]) + INNER_UID
    sec = DIVE_RSYNCD / f"{tok}.secrets"
    common = f"    auth users = d{tok}\n    secrets file = {sec}\n    strict modes = true\n    numeric ids = yes\n"
    user = f"    uid = {u}\n    gid = {u}\n    munge symlinks = no\n"
    out = (f"# Written by ccc-tidepool for one Dive. Removed at its end.\n"
           f"[d{tok}-main]\n    path = {host_dir(d['user'], d['main'])}\n    read only = false\n{user}{common}"
           f"    pre-xfer exec = {HELPER} dive-lock {tok}\n")
    for i, dep in enumerate(d["deps"]):
        out += f"[d{tok}-dep{i}]\n    path = {host_dir(d['user'], dep)}\n    read only = true\n{user}{common}"
    out += f"[d{tok}-dot]\n    path = {DIVE_STATE / tok / 'home'}\n    read only = true\n{user}{common}"
    # the Dive's apt log + sources, sent at its end (ccc-launch send_back), replayed into the Tidepool
    out += f"[d{tok}-back]\n    path = {DIVE_STATE / tok / 'back'}\n    read only = false\n{user}{common}"
    out += (f"[d{tok}-sys]\n    path = {DIVE_STATE / tok / 'sys'}\n    read only = true\n"
            f"    uid = root\n    gid = root\n{common}")
    return out


def write_toml(uid: int, main_dir: Path, text: str) -> None:
    """As the User (never root into their tree): tmp + rename, so a symlinked cuttle.toml is replaced,
    not followed."""
    sh(*as_user(uid), "sh", "-c", 'cat > "$1.ccc-tmp" && mv -f "$1.ccc-tmp" "$1"', "_",
       str(main_dir / "cuttle.toml"), input=text, text=True, capture_output=True)


def dive_open(user: str, tok: str, req: dict) -> None:
    uid = record(check_user(user))["uid"]
    main, deps = check_folders(req["main"], req.get("deps") or [])
    bundle = DIVE_STATE / check_token(tok)
    if not (bundle / "home").is_dir():
        raise Refused(f"Dive {tok} was never prepared")
    if req.get("toml") is not None:
        write_toml(uid, host_dir(user, main), req["toml"])
    # The Tidepool's own host key + the User's keys: the Dive's sshd answers as the Tidepool does, so
    # the pinned known_hosts entry and the User's own keys work unchanged.
    sysd = bundle / "sys"
    sysd.mkdir(mode=0o700, exist_ok=True)
    b = id_base(uid)
    (bundle / "back").mkdir(mode=0o700, exist_ok=True)
    os.chown(bundle / "back", b + INNER_UID, b + INNER_UID)
    for f in ("ssh_host_ed25519_key", "ssh_host_ed25519_key.pub"):
        shutil.copyfile(mnt(user) / "ssh" / f, sysd / f)
        os.chmod(sysd / f, 0o600 if not f.endswith(".pub") else 0o644)
        os.chown(sysd / f, b, b)
    keys = KEYS_ROOT / user / "authorized_keys"
    if keys.exists():
        shutil.copyfile(keys, sysd / "authorized_keys")
    else:
        (sysd / "authorized_keys").touch()
    os.chmod(sysd / "authorized_keys", 0o644)
    # The Dive's ambient session: `cuttle` inside reads it as the User (uid 1000 inside).
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", req.get("session") or ""):
        raise Refused("bad Dive session token")
    (sysd / "session").write_text(req["session"] + "\n")
    os.chmod(sysd / "session", 0o400)
    os.chown(sysd / "session", b + INNER_UID, b + INNER_UID)
    d = {"token": tok, "user": user, "uid": uid, "main": main, "deps": deps, "image": req["image"],
         "locked": False}
    save_dive(tok, d)
    DIVE_RSYNCD.mkdir(parents=True, exist_ok=True, mode=0o700)
    sec = DIVE_RSYNCD / f"{tok}.secrets"
    sec.write_text(f"d{tok}:{req['secret']}\n")
    sec.chmod(0o600)
    conf = DIVE_RSYNCD / f"{tok}.conf"
    tmp = conf.with_suffix(".tmp")
    tmp.write_text(render_modules(tok, d))
    os.replace(tmp, conf)


def _ctr_pid(user: str, wait_s: float = 0) -> int | None:
    deadline = time.monotonic() + wait_s
    while True:
        r = subprocess.run([*podman(user), "container", "inspect", "--format", "{{.State.Pid}}",
                            container(user)], capture_output=True, text=True)
        pid = int(r.stdout.strip() or 0) if r.returncode == 0 else 0
        if pid > 0 or time.monotonic() >= deadline:
            return pid or None
        time.sleep(0.5)


def _inner(d: dict) -> str:
    return f"/home/{d['user']}/{d['main']}"


def apply_lock(d: dict) -> None:
    pid = _ctr_pid(d["user"])
    if pid:
        p = _inner(d)
        # best effort: this runs as rsyncd's pre-xfer exec, and a failure there would abort staging.
        # -a: every ns incl. the user ns. Host root with -m alone gets EINVAL on the bind (the mount ns
        # belongs to the Tidepool's user ns); -p too is needed or /proc/self is missing for remount.
        sh("nsenter", "-t", str(pid), "-a", "--", "sh", "-c",
           'mount --bind "$1" "$1" && mount -o remount,bind,ro "$1"', "_", p, check=False,
           capture_output=True)


def release_lock(d: dict) -> None:
    pid = _ctr_pid(d["user"])
    if pid:
        sh("nsenter", "-t", str(pid), "-a", "--", "umount", _inner(d), check=False, capture_output=True)


def dive_lock(tok: str) -> None:
    """First stage-in only — a User who unmounts it inside is left alone until the next restart."""
    if not dive_record_path(tok).exists():
        return
    d = dive_record(tok)
    if d["locked"]:
        return
    d["locked"] = True
    save_dive(tok, d)
    apply_lock(d)


def started(user: str) -> None:
    """ExecStartPost: put the main-folder locks back. Its ambient session is rotated."""
    check_user(user)
    # A fresh start rotates the ambient session: the Head API's tick mints the next one.
    (KEYS_ROOT / user / "session").unlink(missing_ok=True)
    if not _ctr_pid(user, wait_s=60):
        return
    for d in dives(user):
        if d["locked"]:
            apply_lock(d)
    if (APT_PENDING / user).is_dir() and any((APT_PENDING / user).iterdir()):
        launch_replay(user, "start")


# --- fs: the Dashboard's file browser, as the User inside their own container -----------------
# `fs <user> <op> <path> [<path2>]` — paths from the Tidepool home. Runs `podman exec -u <user>`, so the
# User's own permissions and symlinks apply (it is their container); stdin/stdout pass straight
# through (put / get, zip, list). Argv, never a shell string: paths reach commands as arguments.

FS_OPS = {
    "list": lambda p: ["find", p, "-mindepth", "1", "-maxdepth", "1", "-printf", "%y\\0%s\\0%T@\\0%f\\0"],
    "stat": lambda p: ["find", p, "-maxdepth", "0", "-printf", "%y %Y"],
    "get": lambda p: ["cat", "--", p],
    "zip": lambda p: ["sh", "-c", 'cd "$(dirname "$1")" && exec zip -q -r -1 -y - "$(basename "$1")"',
                      "_", p],
    "put": lambda p: ["sh", "-c", 'mkdir -p "$(dirname "$1")" && exec cat > "$1"', "_", p],
    "mkdir": lambda p: ["mkdir", "-p", "--", p],
    "delete": lambda p: ["rm", "-rf", "--", p],
    "rename": lambda p, q: ["mv", "-T", "--", p, q],
}


def fs_path(user: str, rel: str) -> str:
    """`/home/<user>/<rel>`; "" or "." is the home itself. No `..`, no absolute paths."""
    if rel in ("", "."):
        return f"/home/{user}"
    if rel.startswith("/") or "\0" in rel or len(rel) > 4096 or \
            any(c in ("", ".", "..") for c in rel.split("/")):
        raise Refused(f"{rel!r} is not a path in your Tidepool home")
    return f"/home/{user}/{rel}"


def fs_argv(user: str, op: str, rels: list[str]) -> list[str]:
    if op not in FS_OPS or len(rels) != (2 if op == "rename" else 1):
        raise Refused(f"fs: bad op {op!r}")
    paths = [fs_path(user, r) for r in rels]
    if op in ("delete", "rename") and f"/home/{user}" in paths:
        raise Refused("not your home folder itself")
    return [*podman(user), "exec", *(["-i"] if op == "put" else []), "-u", user, "-w", f"/home/{user}",
            container(user), *FS_OPS[op](*paths)]


def fs(user: str, op: str, rels: list[str]) -> None:
    record(check_user(user))
    argv = fs_argv(user, op, rels)
    os.execvp(argv[0], argv)


def delete_manifest(image: str) -> None:
    """DELETE the snapshot's manifest from the registry (its blobs go at the next registry-gc)."""
    name, digest = image.removeprefix(f"{registry()}/").split("@")
    req = urllib.request.Request(f"https://{registry()}/v2/{name}/manifests/{digest}", method="DELETE",
                                 headers={"Authorization": _auth_header()})
    ctx = ssl.create_default_context(cafile=CA_CERT if Path(CA_CERT).exists() else None)
    try:
        urllib.request.urlopen(req, context=ctx, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise


def dive_close(tok: str) -> None:
    p = dive_record_path(tok)
    if p.exists():
        d = json.loads(p.read_text())
        if d["locked"]:
            release_lock(d)
        try:
            queue_dive_replay(d)
        except Exception as e:
            print(f"ccc-tidepool: Dive {tok}'s apt changes not queued ({e})", file=sys.stderr)
        try:
            delete_manifest(d["image"])
        except Exception as e:
            print(f"ccc-tidepool: snapshot of Dive {tok} not deleted ({e}) — registry-gc keeps it",
                  file=sys.stderr)
    (DIVE_RSYNCD / f"{tok}.conf").unlink(missing_ok=True)
    (DIVE_RSYNCD / f"{tok}.secrets").unlink(missing_ok=True)
    shutil.rmtree(DIVE_STATE / tok, ignore_errors=True)
    p.unlink(missing_ok=True)


# --- apt replay: a Dive's installs (and a rebuilt Tidepool's own) come back ------------------
# A Dive's container layer goes at its end; ccc-launch sends its apt log + sources back (`-back`
# module). `dive-close` queues them under APT_PENDING/<user>/<token>; `replay <user>` (systemd-run, one
# at a time per User) replays them into the Tidepool pinned (`x=ver`). A rebuild (new base/spec) queues
# the old container's own installs as APT_PENDING/<user>/rebuild, replayed by name after the start.
# The result: one line on the next shell (~/.ccc/installs/*.msg), the full log beside it.

APT_PENDING = Path("/var/lib/ccc/apt-pending")
APT_LOGS = ("/var/log/apt/history.log", "/var/log/dpkg.log")
SOURCE_DIRS = {"etc_apt_sources.list.d": "/etc/apt/sources.list.d", "etc_apt_keyrings": "/etc/apt/keyrings",
               "usr_share_keyrings": "/usr/share/keyrings"}
INSTALL_OPS = ("Install", "Upgrade", "Downgrade", "Reinstall")
REMOVE_OPS = ("Remove", "Purge")
_ENTRY_RE = re.compile(r"(?<![^\s,])([a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?) \(([A-Za-z0-9.+~:, -]*)\)")
# SUDO_UID: apt logs it as Requested-By, which is how a rebuild tells the User's installs from the base's.
APT = ["env", "DEBIAN_FRONTEND=noninteractive", "SUDO_UID=1000", "apt-get", "-y", "-q",
       "-o", "DPkg::Lock::Timeout=900", "--no-install-recommends", "--allow-downgrades"]


def apt_offsets(user: str) -> dict:
    r = subprocess.run([*podman(user), "exec", container(user), "sh", "-c",
                        'for f; do stat -c %s "$f" 2>/dev/null || echo 0; done', "_", *APT_LOGS],
                       capture_output=True, text=True)
    n = [int(x) for x in r.stdout.split()] if r.returncode == 0 else []
    return dict(zip(("history", "dpkg"), n)) if len(n) == 2 else {"history": 0, "dpkg": 0}


def parse_history(text: str, requested_only: bool = False) -> dict[str, dict]:
    """Net effect of apt's history, in log order: pkg -> {op: install|remove|purge, ver, auto}.
    auto: True/False for an Install, else what it was before (None = unknown, leave as is)."""
    out: dict[str, dict] = {}
    for block in text.split("\n\n"):
        fields = dict(ln.split(": ", 1) for ln in block.strip().splitlines() if ": " in ln)
        if requested_only and "Requested-By" not in fields:
            continue
        for op, val in fields.items():
            for name, vers in _ENTRY_RE.findall(val) if op in INSTALL_OPS + REMOVE_OPS else []:
                if op in REMOVE_OPS:
                    out[name] = {"op": op.lower()}
                    continue
                parts = [v.strip() for v in vers.split(",")]
                auto = "automatic" in parts if op == "Install" else out.get(name, {}).get("auto")
                out[name] = {"op": "install", "ver": [v for v in parts if v != "automatic"][-1], "auto": auto}
    return out


def dpkg_only(text: str, apt_names: set[str]) -> list[str]:
    """Packages dpkg installed that apt never logged — `dpkg -i`."""
    seen = []
    for ln in text.splitlines():
        f = ln.split()
        if len(f) >= 4 and f[2] in ("install", "upgrade"):
            n = f[3].split(":")[0]
            if n not in apt_names and n not in seen:
                seen.append(n)
    return seen


def _short(name: str) -> str:
    return name.split(":")[0]


def _tail(path: Path, offset: int) -> str:
    if not path.is_file() or path.is_symlink():
        return ""
    b = path.read_bytes()
    return (b[offset:] if offset <= len(b) else b).decode(errors="replace")


def _exec(user: str, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([*podman(user), "exec", "-u", "0", container(user), *argv],
                          capture_output=True, text=True)


def _apt(user: str, args: list[str], log: list[str]) -> bool:
    r = _exec(user, *APT, *args)
    log += [f"$ apt-get {' '.join(args)}", r.stdout + r.stderr]
    return r.returncode == 0


def _batch(user: str, verb: str, args: list[str], log: list[str]) -> tuple[list[str], list[str]]:
    """All at once; on failure one by one, to name the ones that fail."""
    if not args or _apt(user, [verb, *args], log):
        return args, []
    ok, bad = [], []
    for a in args:
        (ok if _apt(user, [verb, a], log) else bad).append(a)
    return ok, bad


def copy_sources(user: str, src: Path, log: list[str]) -> list[str]:
    """apt sources + keyrings the Dive added — never over a file the Tidepool has."""
    out = []
    for key, dest in SOURCE_DIRS.items():
        d = src / key
        if d.is_symlink() or not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            target = f"{dest}/{f.name}"
            if f.is_symlink() or not f.is_file():
                continue
            # exec + cat, not `podman cp` (into the running Tidepool it reported success, wrote nothing)
            r = subprocess.run([*podman(user), "exec", "-i", "-u", "0", container(user), "sh", "-c",
                                '[ ! -e "$1" ] || exit 3; cat > "$1" && chmod 644 "$1"', "_", target],
                               input=f.read_bytes(), capture_output=True)
            if r.returncode == 0:
                log.append(f"copied {target}")
                out.append(f.name)
            elif r.returncode != 3:
                log.append(f"not copied {target}: {r.stderr.decode(errors='replace').strip()}")
    return out


def replay_changes(user: str, changes: dict[str, dict], pinned: bool, log: list[str]) -> dict:
    res = {"back": [], "removed": [], "failed": [], "changed": []}
    if not changes:
        return res
    was_auto = set(_exec(user, "apt-mark", "showauto").stdout.split())
    _apt(user, ["update"], log)
    for verb in ("remove", "purge"):
        ok, bad = _batch(user, verb, [n for n, c in changes.items() if c["op"] == verb], log)
        res["removed"] += [_short(n) for n in ok]
        res["failed"] += [f"{verb} {_short(n)}" for n in bad]
    inst = {n: c for n, c in changes.items() if c["op"] == "install"}
    ok, bad = _batch(user, "install", [f"{n}={c['ver']}" if pinned else n for n, c in inst.items()], log)
    ok_names = [a.split("=")[0] for a in ok]
    res["failed"] += [_short(a) for a in bad]
    auto = [n for n in ok_names if inst[n]["auto"] or (inst[n]["auto"] is None and _short(n) in was_auto)]
    if auto:
        _exec(user, "apt-mark", "auto", *auto)
    manual = [n for n in ok_names if n not in auto]
    res["back"] = [_short(n) for n in manual]
    if not pinned and manual:
        r = _exec(user, "dpkg-query", "-W", "-f=${Package} ${Version}\n", *[_short(n) for n in manual])
        now = dict(ln.split(" ", 1) for ln in r.stdout.splitlines() if " " in ln)
        res["changed"] = [f"{_short(n)} {inst[n]['ver']} → {now[_short(n)]}" for n in manual
                          if now.get(_short(n), inst[n]["ver"]) != inst[n]["ver"]]
    return res


def summary(head: str, res: dict) -> str | None:
    parts = [f"{k}: {', '.join(res[key])}" for key, k in
             (("back", "brought back" if head.startswith("Dive") else "reinstalled"), ("removed", "removed"),
              ("sources", "apt sources"), ("changed", "new versions"), ("failed", "failed"),
              ("skipped", "not replayed (dpkg -i)")) if res.get(key)]
    return f"{head} {' · '.join(parts)}" if parts else None


def write_home(user: str, rel: str, text: str) -> None:
    """As the User (never root into their tree)."""
    uid = record(user)["uid"]
    sh(*as_user(uid), "sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "_",
       str(mnt(user) / "home" / rel), input=text, text=True, capture_output=True, check=False)


def replay_item(user: str, item: Path) -> None:
    meta = json.loads((item / "meta.json").read_text())
    log: list[str] = []
    if meta["kind"] == "rebuild":
        res = replay_changes(user, meta["changes"], pinned=False, log=log)
        head, name = "Tidepool rebuilt —", "rebuild"
    else:
        off = meta["offsets"]
        hist = _tail(item / "var_log_apt_history.log", off["history"])
        changes = parse_history(hist)
        sources = copy_sources(user, item, log)
        if sources and not changes:
            _apt(user, ["update"], log)
        res = replay_changes(user, changes, pinned=True, log=log)
        res["sources"] = sources
        res["skipped"] = dpkg_only(_tail(item / "var_log_dpkg.log", off["dpkg"]), {_short(n) for n in changes})
        head, name = f"Dive {meta['job']}", f"dive-{meta['job']}"
    line = summary(head, res)
    if line:
        write_home(user, f".ccc/{name}-installs.log", "\n".join(log) + "\n")
        write_home(user, f".ccc/installs/{name}.msg", f"{line} (log: ~/.ccc/{name}-installs.log)\n")


def replay(user: str) -> None:
    """Every queued item, oldest first (a rebuild before any Dive); stops while the Tidepool is down —
    `started` runs it again."""
    check_user(user)
    APT_PENDING.mkdir(mode=0o700, parents=True, exist_ok=True)
    q = APT_PENDING / user
    with open(APT_PENDING / f"{user}.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        while q.is_dir() and _ctr_pid(user):
            items = sorted(q.iterdir(), key=lambda p: (p.name != "rebuild", p.stat().st_mtime))
            if not items:
                return
            try:
                replay_item(user, items[0])
            finally:
                shutil.rmtree(items[0], ignore_errors=True)


def launch_replay(user: str, tag: str) -> None:
    sh("systemd-run", "--no-block", "--collect", "--quiet", f"--unit=ccc-apt-replay-{user}-{tag}",
       HELPER, "replay", user, check=False, capture_output=True)


def queue_dive_replay(d: dict) -> None:
    tok = d["token"]
    back = DIVE_STATE / tok / "back"
    job = (back / "job").read_text().strip() if (back / "job").is_file() else ""
    if not job.isdigit():
        return
    offp = DIVE_STATE / tok / "apt-offsets.json"
    off = json.loads(offp.read_text()) if offp.is_file() else {"history": 0, "dpkg": 0}
    q = APT_PENDING / d["user"] / tok
    q.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    APT_PENDING.chmod(0o700)
    shutil.rmtree(q, ignore_errors=True)
    shutil.move(str(back), str(q))
    (q / "meta.json").write_text(json.dumps({"kind": "dive", "job": int(job), "offsets": off}))
    launch_replay(d["user"], tok[:8])


def queue_rebuild(user: str) -> None:
    """Before the old container goes: its own (sudo) apt history, kept for the replay after the start."""
    with tempfile.TemporaryDirectory() as t:
        sh(*podman(user), "cp", f"{container(user)}:{APT_LOGS[0]}", f"{t}/h", check=False, capture_output=True)
        p = Path(t) / "h"
        changes = parse_history(p.read_text(errors="replace"), requested_only=True) if p.is_file() else {}
    q = APT_PENDING / user / "rebuild"
    old = json.loads((q / "meta.json").read_text())["changes"] if (q / "meta.json").is_file() else {}
    if not (changes or old):
        return
    q.mkdir(mode=0o700, parents=True, exist_ok=True)
    (q / "meta.json").write_text(json.dumps({"kind": "rebuild", "changes": {**old, **changes}}))


def registry_gc() -> None:
    """Nightly (ccc-registry-gc.timer): blobs no manifest references any more — every ended Dive's
    snapshot layer — go. The push instance is stopped for the sweep; the pull instance only reads."""
    svc = "ccc-registry-push"
    image = sh("podman", "container", "inspect", "--format", "{{.ImageName}}", svc,
               capture_output=True, text=True).stdout.strip()
    sh("systemctl", "stop", svc)
    try:
        sh("podman", "run", "--rm", "--network", "none", "-v", "/opt/ccc/registry:/var/lib/registry",
           "-v", "/etc/ccc/registry/push.yml:/etc/docker/registry/config.yml:ro", image,
           "garbage-collect", "/etc/docker/registry/config.yml", capture_output=True)
    finally:
        sh("systemctl", "start", svc)


def _stdin_json() -> dict:
    return json.loads(sys.stdin.read() or "{}")


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[0] == "create":
            create(argv[1], int(argv[2]))
        elif len(argv) == 2 and argv[0] in ("restart", "reset", "remove"):
            {"restart": restart, "reset": reset, "remove": remove}[argv[0]](argv[1])
        elif argv[:1] == ["teardown"] and argv[1:] in ([], ["--all"]):
            teardown(argv[1:] == ["--all"])
        elif len(argv) == 3 and argv[0] in ("dive-prep", "dive-snap", "dive-open") and not (
                check_user(argv[1]) and check_token(argv[2])):
            pass
        elif len(argv) == 3 and argv[0] == "dive-prep":
            print(json.dumps(dive_prep(argv[1], argv[2], _stdin_json())))
        elif len(argv) == 3 and argv[0] == "dive-snap":
            print(json.dumps(dive_snap(argv[1], argv[2])))
        elif len(argv) == 3 and argv[0] == "dive-open":
            dive_open(argv[1], argv[2], _stdin_json())
        elif len(argv) == 2 and argv[0] == "dive-close":
            dive_close(check_token(argv[1]))
        elif len(argv) == 2 and argv[0] == "dive-lock":
            dive_lock(argv[1])
        elif len(argv) == 2 and argv[0] == "started":
            started(argv[1])
        elif len(argv) == 2 and argv[0] == "replay":
            replay(argv[1])
        elif argv == ["registry-gc"]:
            registry_gc()
        elif len(argv) in (4, 5) and argv[0] == "fs":
            fs(argv[1], argv[2], argv[3:])
        else:
            raise Refused("usage: ccc-tidepool create <user> <uid> | restart|reset|remove <user> | "
                          "teardown [--all] | dive-prep|dive-snap|dive-open <user> <token> | "
                          "dive-close|dive-lock <token> | started|replay <user> | "
                          "registry-gc | fs <user> <op> <path> [<path>]")
    except (Refused, ValueError) as e:
        print(f"ccc-tidepool: {e}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as e:
        print(f"ccc-tidepool: {' '.join(map(str, e.cmd))} failed ({e.returncode})", file=sys.stderr)
        if e.stderr:
            sys.stderr.write(e.stderr.decode() if isinstance(e.stderr, bytes) else e.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("ccc-tidepool: run as root", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1:]))
