#!/usr/bin/python3 -I
"""ccc-tidepool — create, run and remove a User's **Tidepool** on the Head, as root.

Installed root-owned at /usr/local/sbin/ccc-tidepool on the Head by `cuttle setup --head`.
- `create <user> <uid>`: the operator, through `cuttle user add` / `setup --head` (reconcile, idempotent).
- `restart|reset <user>`: the Head API (as `ccc`) via one sudoers rule, for the logged-in User only;
  or the operator for anyone.
- `remove <user>`, `teardown [--all]`: the operator (`cuttle user remove`, `cuttle teardown`).
- `dive-prep|dive-snap|dive-open <user> <token>`, `dive-close <token>`: the Head API, for a **Dive**
  (see the Dives section below). `dive-lock <token>`: rsyncd (pre-xfer exec). `dive-gpu`: ccc-launch on
  the Head. `started <user>`: the unit's ExecStartPost.

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
import json
import os
import pwd
import re
import shutil
import signal
import ssl
import subprocess
import sys
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
SPEC_VERSION = "3"
CDI_SPEC = Path("/var/run/cdi/nvidia.yaml")      # config.CDI_SPEC


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


def podman(user: str) -> list[str]:
    """Podman pointed at this User's store (inside their image), the base read from the main store."""
    return ["podman", "--root", str(mnt(user) / "storage"), "--runroot", f"/run/ccc-tidepools/{user}",
            "--storage-opt", f"additionalimagestore={MAIN_STORE}"]


def render_mount(user: str, image: Path) -> str:
    return (f"# Written by ccc-tidepool. Do not hand-edit.\n[Unit]\nDescription=CCC Tidepool image ({user})\n\n"
            f"[Mount]\nWhat={image}\nWhere={mnt(user)}\nType=ext4\nOptions=loop\n")


def head_gpus() -> bool:
    """The Head is also an Arm with GPUs: every Tidepool gets them via CDI, denied until a Dive."""
    return CDI_SPEC.exists() and any(Path("/dev").glob("nvidia[0-9]*"))


def mesh_ip() -> str:
    """The Head's mesh address: what `ccc.<name>` means inside a Tidepool (the API listens on all)."""
    out = subprocess.run(["/usr/sbin/ip", "-4", "-o", "addr", "show", "dev", MESH_IFACE],
                         capture_output=True, text=True).stdout
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", out)
    if not m:
        raise Refused(f"no mesh address on {MESH_IFACE}")
    return m[1]


def create_argv(user: str, uid: int, s: dict, base_ref: str, head_ip: str,
                gpus: bool = False) -> list[str]:
    b = id_base(uid)
    m = mnt(user)
    return [*podman(user), "create", "--pull=never", "--name", container(user), "--hostname", "tidepool",
            *(["--device", "nvidia.com/gpu=all"] if gpus else []),
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
        return img
    img.parent.mkdir(parents=True, exist_ok=True)
    want = s["size_gb"] * 2**30
    if free_bytes(img.parent) < want:
        raise Refused(f"{img.parent} has {free_bytes(img.parent) // 2**30} GB free — not enough for "
                      f"another {s['size_gb']} GB Tidepool")
    sh("fallocate", "-l", str(want), str(img))
    img.chmod(0o600)
    sh("mkfs.ext4", "-q", "-F", "-m", "0", "-L", f"tp-{user}"[:16], str(img))
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
    if have is not None:
        # a new release's base (setup --head wiped the old one), or a new create spec: the old layer
        # cannot outlive it — recreated, home kept, like a reset
        sh("systemctl", "stop", service(user), check=False, capture_output=True)
        sh(*podman(user), "rm", "-f", "-t", "5", container(user), check=False, capture_output=True)
    # The digest ref is the pushed manifest's; a locally built image may not answer to it until pulled
    # once into the main store (layers already there — only the manifest moves). Never into the User's.
    if not ok("podman", "image", "exists", base):
        sh("podman", "pull", "-q", "--authfile", AUTH_FILE, base, capture_output=True)
    sh(*create_argv(user, uid, s, base, mesh_ip(), gpus=head_gpus()), capture_output=True)


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


def _no_head_dive(user: str) -> None:
    if any(d.get("head") for d in dives(user)):
        raise Refused(f"a Dive is using {user}'s Tidepool for its GPU — end it first (`cuttle dive end`)")


def restart(user: str) -> None:
    record(check_user(user))
    _no_head_dive(user)
    sh("systemctl", "restart", service(user))


def reset(user: str) -> None:
    """A fresh container from the base: the apt layer goes, home stays."""
    uid = record(check_user(user))["uid"]
    _no_head_dive(user)
    sh("systemctl", "stop", service(user))
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
    """Every Tidepool down and unmounted. Images kept unless `all_` — head.db goes with the teardown,
    so a kept image is an orphan only an operator can recover by hand."""
    s = settings() if SETTINGS.exists() else None
    for user in users():
        _stop(user, drop_container=all_)   # a kept image keeps its container + apt layer too
        if all_ and s and not os.path.ismount(mnt(user)):
            image_path(s, user).unlink(missing_ok=True)
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
#   travels; the base is already in the registry. Prints the digest ref.
# - `dive-open <user> <tok>` (stdin {main, deps, toml, secret, image}): writes cuttle.toml back, the
#   record, and the per-Dive rsync modules (root-owned dir rsyncd &includes): main writable, deps and
#   the dotfile/host-key bundle read-only. Modules run as the User's mapped uid, so the files are uid
#   1000 inside on both ends (same id_base everywhere) and nothing else on the Head is reachable.
# - `dive-lock <tok>`: the main module's pre-xfer exec (rsyncd runs it as root) — the first stage-in
#   makes the main folder read-only inside the Tidepool container (a bind over itself; the host path
#   stays writable for the mirror). Soft: `sudo umount` inside undoes it, deliberately.
# - `dive-gpu grant|revoke <tok> …`: ccc-launch when the Dive landed on the Head — the Tidepool
#   container itself gets the GPU (live device allow on its scope) and the job's CPU/RAM.
# - `dive-close <tok>`: the Head API's tick once the job has left the queue — unlock, revoke, modules
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
    record(check_user(user))
    check_token(tok)
    ref = f"{registry()}/ccc/u-{user}:{tok}"
    digest_file = DIVE_STATE / f"{tok}.digest"
    DIVE_STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    sh(*podman(user), "commit", "-q", container(user), ref, capture_output=True)
    try:
        sh(*podman(user), "push", "-q", "--authfile", AUTH_FILE, "--digestfile", str(digest_file), ref,
           capture_output=True)
    finally:
        sh(*podman(user), "rmi", "-f", ref, check=False, capture_output=True)
    digest = digest_file.read_text().strip()
    digest_file.unlink()
    return {"image": f"{registry()}/ccc/u-{user}@{digest}"}


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
         "locked": False, "head": None}
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
        # best effort: this runs as rsyncd's pre-xfer exec, and a failure there would abort staging
        sh("nsenter", "-t", str(pid), "-m", "--", "sh", "-c",
           'mount --bind "$1" "$1" && mount -o remount,bind,ro "$1"', "_", p, check=False,
           capture_output=True)


def release_lock(d: dict) -> None:
    pid = _ctr_pid(d["user"])
    if pid:
        sh("nsenter", "-t", str(pid), "-m", "--", "umount", _inner(d), check=False, capture_output=True)


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


# --- the Head's own GPU, handed to a Tidepool -------------------------------------

def scope(user: str) -> str:
    cid = sh(*podman(user), "container", "inspect", "--format", "{{.Id}}", container(user),
             capture_output=True, text=True).stdout.strip()
    return f"libpod-{cid}.scope"


# What a Tidepool may open while no Dive grants it the GPU: the pseudo devices `closed` already
# allows, plus terminals. The container was created with every GPU (CDI), so granting is one more
# DeviceAllow, live.
BASE_DEVICES = ("char-pts rw", "/dev/ptmx rw", "/dev/tty rw", "/dev/console rw", "/dev/fuse rw")
GPU_SHARED = ("/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia-uvm-tools")


def device_policy(user: str, minors: list[int]) -> None:
    allow = [*BASE_DEVICES, *(f"{n} rw" for n in GPU_SHARED if minors),
             *(f"/dev/nvidia{m} rw" for m in minors)]
    sh("systemctl", "set-property", "--runtime", scope(user), "DevicePolicy=closed", "DeviceAllow=",
       *(f"DeviceAllow={a}" for a in allow), capture_output=True)


def _cgroup_pids(user: str) -> list[int]:
    cg = sh("systemctl", "show", "-p", "ControlGroup", "--value", scope(user), capture_output=True,
            text=True).stdout.strip()
    pids = []
    for procs in Path(f"/sys/fs/cgroup{cg}").rglob("cgroup.procs"):
        pids += [int(x) for x in procs.read_text().split()]
    return pids


def kill_gpu_holders(user: str) -> int:
    n = 0
    for pid in _cgroup_pids(user):
        try:
            if any(os.readlink(f).startswith("/dev/nvidia") for f in Path(f"/proc/{pid}/fd").iterdir()):
                os.kill(pid, signal.SIGKILL)
                n += 1
        except OSError:
            pass
    return n


def _resize(user: str) -> None:
    """Container caps = the Tidepool cap, or the live Head Dives' allocation if larger; the slice
    grows by every live Head Dive's allocation (Slurm gave those CPUs/RAM; the slice must not hold
    them back)."""
    s = settings()
    held = [d["head"] for d in dives() if d.get("head")]
    mine = [h for d in dives(user) if (h := d.get("head"))]
    cpus = max([s["cpus"], *(h["cpus"] for h in mine)])
    mem_mb = max([s["mem_gb"] * 1024, *(h["mem_mb"] for h in mine)])
    sh(*podman(user), "update", "--cpus", str(cpus), "--memory", f"{mem_mb}m", "--memory-swap",
       f"{mem_mb}m", container(user), check=False, capture_output=True)
    sh("systemctl", "set-property", "--runtime", SLICE,
       f"CPUQuota={(s['total_cpus'] + sum(h['cpus'] for h in held)) * 100}%",
       f"MemoryMax={s['total_mem_gb'] * 1024 + sum(h['mem_mb'] for h in held)}M",
       check=False, capture_output=True)


def _minors_held(user: str) -> list[int]:
    return sorted({m for d in dives(user) if d.get("head") for m in d["head"]["minors"]})


def dive_gpu_grant(tok: str, jid: str, minors: list[int], cpus: int, mem_mb: int) -> None:
    d = dive_record(tok)
    d["head"] = {"jid": jid, "minors": minors, "cpus": cpus, "mem_mb": mem_mb}
    save_dive(tok, d)
    if d["locked"]:                       # a lock is for a copy; the Head Dive works on the live folder
        release_lock(d)
        d["locked"] = False
        save_dive(tok, d)
    _resize(d["user"])
    device_policy(d["user"], _minors_held(d["user"]))


def dive_gpu_revoke(tok: str) -> None:
    d = dive_record(tok)
    if not d.get("head"):
        return
    d["head"] = None
    save_dive(tok, d)
    device_policy(d["user"], _minors_held(d["user"]))   # new opens fail from here…
    if not _minors_held(d["user"]):
        kill_gpu_holders(d["user"])                      # …and what still holds one is killed
    _resize(d["user"])


def dive_gpu_revoke_job(jid: str) -> None:
    """The epilog's backstop, by job id: ccc-launch may have been killed before it revoked."""
    for d in dives():
        if (d.get("head") or {}).get("jid") == jid:
            dive_gpu_revoke(d["token"])


def started(user: str) -> None:
    """ExecStartPost: a (re)started container has a fresh scope — deny the GPU again (or grant what
    live Head Dives hold) and put the main-folder locks back. Its ambient session is rotated."""
    check_user(user)
    # A fresh start rotates the ambient session: the Head API's tick mints the next one.
    (KEYS_ROOT / user / "session").unlink(missing_ok=True)
    if not _ctr_pid(user, wait_s=60):
        return
    if head_gpus():
        device_policy(user, _minors_held(user))
    for d in dives(user):
        if d["locked"]:
            apply_lock(d)


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
        if d.get("head"):
            dive_gpu_revoke(tok)
        if d["locked"]:
            release_lock(d)
        try:
            delete_manifest(d["image"])
        except Exception as e:
            print(f"ccc-tidepool: snapshot of Dive {tok} not deleted ({e}) — registry-gc keeps it",
                  file=sys.stderr)
    (DIVE_RSYNCD / f"{tok}.conf").unlink(missing_ok=True)
    (DIVE_RSYNCD / f"{tok}.secrets").unlink(missing_ok=True)
    shutil.rmtree(DIVE_STATE / tok, ignore_errors=True)
    p.unlink(missing_ok=True)


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
        elif len(argv) == 7 and argv[:2] == ["dive-gpu", "grant"]:
            dive_gpu_grant(argv[2], str(int(argv[3])), [int(m) for m in argv[4].split(",") if m],
                           int(argv[5]), int(argv[6]))
        elif len(argv) == 3 and argv[:2] == ["dive-gpu", "revoke"]:
            dive_gpu_revoke(argv[2])
        elif len(argv) == 3 and argv[:2] == ["dive-gpu", "revoke-job"]:
            dive_gpu_revoke_job(str(int(argv[2])))
        elif len(argv) == 2 and argv[0] == "started":
            started(argv[1])
        elif argv == ["registry-gc"]:
            registry_gc()
        else:
            raise Refused("usage: ccc-tidepool create <user> <uid> | restart|reset|remove <user> | "
                          "teardown [--all] | dive-prep|dive-snap|dive-open <user> <token> | "
                          "dive-close|dive-lock <token> | dive-gpu grant|revoke … | started <user> | "
                          "registry-gc")
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
