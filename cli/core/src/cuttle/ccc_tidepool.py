#!/usr/bin/python3 -I
"""ccc-tidepool — create, run and remove a User's **Tidepool** on the Head, as root.

Installed root-owned at /usr/local/sbin/ccc-tidepool on the Head by `cuttle setup --head`.
- `create <user> <uid>`: the operator, through `cuttle user add` / `setup --head` (reconcile, idempotent).
- `restart|reset <user>`: the Head API (as `ccc`) via one sudoers rule, for the logged-in User only;
  or the operator for anyone.
- `remove <user>`, `teardown [--all]`: the operator (`cuttle user remove`, `cuttle teardown`).

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

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SETTINGS = Path("/etc/ccc/tidepools.json")      # config.TIDEPOOL_SETTINGS
IMAGES_RECORD = Path("/opt/ccc/images.json")    # config.IMAGES_RECORD
AUTH_FILE = "/etc/ccc/registry-auth.json"       # config.REGISTRY_AUTH (the Head's push credential)
MNT_ROOT = Path("/var/lib/ccc/tidepools")       # config.TIDEPOOL_MNT_ROOT
KEYS_ROOT = Path("/var/lib/ccc/head/tidepool-keys")  # config.TIDEPOOL_KEYS_ROOT
FILES_DIR = Path("/usr/local/lib/ccc/tidepool")  # config.TIDEPOOL_FILES_DIR
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


def create_argv(user: str, uid: int, s: dict, base_ref: str) -> list[str]:
    b = id_base(uid)
    m = mnt(user)
    return [*podman(user), "create", "--pull=never", "--name", container(user), "--hostname", "tidepool",
            "--init", "--uidmap", f"0:{b}:{ID_COUNT}", "--gidmap", f"0:{b}:{ID_COUNT}",
            "--network", network(user), "-p", f"{ssh_port(uid)}:22",
            "--cpus", str(s["cpus"]), "--memory", f"{s['mem_gb']}g", "--memory-swap", f"{s['mem_gb']}g",
            "--cgroup-parent", SLICE, "--label", f"ccc.tidepool={user}",
            "-e", f"CCC_USER={user}",
            "-v", f"{m / 'home'}:/home/{user}",
            "-v", f"{m / 'ssh'}:/etc/ccc/hostkeys:ro",
            "-v", f"{KEYS_ROOT / user}:/etc/ccc/keys:ro",
            "-v", f"{FILES_DIR}:/etc/ccc/tidepool:ro",
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
    keys dir the Head API writes authorized_keys into (`ccc`, read-only in the container)."""
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
    keys.mkdir(parents=True, exist_ok=True, mode=0o755)
    shutil.chown(keys, API_USER, API_USER)
    shutil.copyfile(key.with_suffix(".pub"), keys / "host_ed25519.pub")
    Path(f"/run/ccc-tidepools/{user}").mkdir(parents=True, exist_ok=True, mode=0o700)


def ensure_network(user: str) -> None:
    if not ok("podman", "network", "exists", network(user)):
        sh("podman", "network", "create", "--opt", "isolate=true", network(user),
           capture_output=True)


def _container_image(user: str) -> str | None:
    r = subprocess.run([*podman(user), "container", "inspect", "--format", "{{.ImageName}}",
                        container(user)], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def ensure_container(user: str, uid: int, s: dict) -> None:
    base = json.loads(IMAGES_RECORD.read_text())["base"]
    have = _container_image(user)
    if have == base:
        # caps may have changed at a setup re-run; live, no restart
        sh(*podman(user), "update", "--cpus", str(s["cpus"]), "--memory", f"{s['mem_gb']}g",
           container(user), check=False, capture_output=True)
        return
    if have is not None:
        # a new release's base (setup --head wiped the old one): the old layer cannot outlive it —
        # recreated, home kept, like a reset
        sh("systemctl", "stop", service(user), check=False, capture_output=True)
        sh(*podman(user), "rm", "-f", "-t", "5", container(user), check=False, capture_output=True)
    # The digest ref is the pushed manifest's; a locally built image may not answer to it until pulled
    # once into the main store (layers already there — only the manifest moves). Never into the User's.
    if not ok("podman", "image", "exists", base):
        sh("podman", "pull", "-q", "--authfile", AUTH_FILE, base, capture_output=True)
    sh(*create_argv(user, uid, s, base), capture_output=True)


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
    """A fresh container from the base: the apt layer goes, home stays."""
    uid = record(check_user(user))["uid"]
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
    sh("systemctl", "daemon-reload", check=False)


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[0] == "create":
            create(argv[1], int(argv[2]))
        elif len(argv) == 2 and argv[0] in ("restart", "reset", "remove"):
            {"restart": restart, "reset": reset, "remove": remove}[argv[0]](argv[1])
        elif argv[:1] == ["teardown"] and argv[1:] in ([], ["--all"]):
            teardown(argv[1:] == ["--all"])
        else:
            raise Refused("usage: ccc-tidepool create <user> <uid> | restart|reset|remove <user> | "
                          "teardown [--all]")
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
