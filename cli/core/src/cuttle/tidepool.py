"""Tidepool — each User's fixed-size filesystem + always-on CPU container on the Head, reached by SSH.

Two halves:
- **Head (operator, sudo)**: `setup --head` asks the sizes, installs the root helper
  (ccc_tidepool.py → /usr/local/sbin/ccc-tidepool), its unit/slice/files, and reconciles a Tidepool for
  every User; `user add`/`user remove`/`teardown` call the helper. The Head API reaches the helper only
  through one sudoers rule (restart|reset <user>), and only for the caller's own.
- **Any box (User)**: `cuttle login` makes `~/.ssh/ccc_ed25519`, uploads its public half, and writes
  the `Host <name>` alias with the Tidepool's host key pinned (sshconfig.py). `cuttle tidepool` ssh's
  in; plain ssh/scp/rsync/VS Code use the same alias.
"""

import json
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from . import api, auth, config, sshconfig, system, templates, transfer, ui
from .system import run

_HERE = Path(__file__).resolve().parent
_P10K = Path(__file__).resolve().parents[4] / "head" / "api" / "src" / "ccc_head_api" / "zsh" / "p10k.zsh"


# --- Head: settings --------------------------------------------------------------

def load_settings() -> dict | None:
    try:
        return json.loads(config.TIDEPOOL_SETTINGS.read_text())
    except (OSError, ValueError):
        return None


def default_settings() -> dict:
    cpus, mem_gb = system.cpu_count(), system.real_memory_mb() // 1024
    f = config.TIDEPOOL_DEFAULT_TOTAL_FRACTION
    total_cpus, total_mem = max(1, cpus // f), max(1, mem_gb // f)
    # One Tidepool's cap never above all of them together (the slice would cap it anyway, silently).
    return {"image_dir": config.TIDEPOOL_DEFAULT_IMAGE_DIR, "size_gb": config.TIDEPOOL_DEFAULT_SIZE_GB,
            "cpus": min(config.TIDEPOOL_DEFAULT_CPUS, total_cpus),
            "mem_gb": min(config.TIDEPOOL_DEFAULT_MEM_GB, total_mem),
            "total_cpus": total_cpus, "total_mem_gb": total_mem}


def prompt_settings(existing: dict | None) -> dict:
    """Asked at `setup --head` (not on update). One size for every User; caps are ceilings."""
    d = existing or default_settings()
    s = {"image_dir": ui.ask_text("Folder for Tidepool images (one fixed-size file per User)?",
                                  d["image_dir"]).strip(),
         "size_gb": ui.ask_int("Tidepool size per User (GB, reserved up front)?", str(d["size_gb"])),
         "cpus": ui.ask_int("CPU cap per Tidepool?", str(d["cpus"])),
         "mem_gb": ui.ask_int("Memory cap per Tidepool (GB)?", str(d["mem_gb"])),
         "total_cpus": ui.ask_int("CPUs for all Tidepools together (taken off this box's Slurm node)?",
                                  str(d["total_cpus"])),
         "total_mem_gb": ui.ask_int("Memory for all Tidepools together (GB, taken off Slurm)?",
                                    str(d["total_mem_gb"]))}
    if not s["image_dir"].startswith("/") or Path(s["image_dir"]).resolve().is_relative_to(config.CCC_LIB):
        raise ui.abort(f"Tidepool folder must be absolute and outside {config.CCC_LIB} "
                       f"(teardown removes that)")
    return s


def slurm_reserve() -> tuple[int, int]:
    """(cpus, mem_mb) the Head's Slurm node gives up to the Tidepools — (0, 0) before setup asked."""
    s = load_settings()
    return (s["total_cpus"], s["total_mem_gb"] * 1024) if s else (0, 0)


def save_settings(write_root_file, s: dict) -> None:
    write_root_file(config.TIDEPOOL_SETTINGS, json.dumps(s, indent=2, sort_keys=True) + "\n")


# --- Head: install / reconcile / remove -------------------------------------------

def install(write_root_file, s: dict) -> None:
    """Helper, sudoers rule, container-side files, unit template, slice, keys root, SSH port range."""
    write_root_file(config.TIDEPOOL_HELPER_BIN, (_HERE / "ccc_tidepool.py").read_text(), "755")
    write_root_file(config.SUDOERS_CCC_TIDEPOOL, templates.render_tidepool_sudoers(), "440")
    f = config.TIDEPOOL_FILES_DIR
    write_root_file(f / "init", templates.render_tidepool_init(), "755")
    write_root_file(f / "sshd_config", templates.TIDEPOOL_SSHD_CONFIG)
    write_root_file(f / "zshrc", templates.render_tidepool_zshrc())
    write_root_file(f / "p10k.zsh", _P10K.read_text())
    write_root_file(config.TIDEPOOL_UNIT, templates.render_tidepool_unit())
    write_root_file(config.TIDEPOOL_SLICE_UNIT,
                    templates.render_tidepool_slice(s["total_cpus"], s["total_mem_gb"]))
    run(["install", "-d", "-m", "0755", "-o", config.HEAD_API_USER, "-g", config.HEAD_API_USER,
         str(config.TIDEPOOL_KEYS_ROOT)], sudo=True)
    run(["install", "-d", "-m", "0755", str(config.TIDEPOOL_MNT_ROOT)], sudo=True)
    run(["install", "-d", "-m", "0700", str(s["image_dir"])], sudo=True)
    run(["install", "-d", "-m", "0700", str(config.DIVE_RECORDS), str(config.DIVE_RSYNCD)], sudo=True)
    run(["install", "-d", "-m", "0711", str(config.DIVE_STATE)], sudo=True)
    write_root_file(config.REGISTRY_GC_UNIT, templates.render_registry_gc_service())
    write_root_file(config.REGISTRY_GC_TIMER, templates.REGISTRY_GC_TIMER_TMPL)
    run(["systemctl", "daemon-reload"], sudo=True)
    run(["systemctl", "start", config.TIDEPOOL_SLICE], sudo=True)
    run(["systemctl", "enable", "--now", f"{config.REGISTRY_GC_SERVICE}.timer"], sudo=True)
    system.allow_tcp_port_range(config.TIDEPOOL_SSH_PORT_BASE + 1, _port_count(),
                                "ccc tidepool ssh (LAN+mesh)")
    system.allow_dns_from(config.TIDEPOOL_BRIDGE_SUBNETS, "ccc tidepool dns")


def _port_count() -> int:
    return config.GUISE_UID_MAX - config.CCC_UID


def _helper(*args: str, check: bool = True):
    r = run([str(config.TIDEPOOL_HELPER_BIN), *args], sudo=True, check=check, capture_output=True,
            text=True)
    if (r.stdout or "").strip():
        ui.say(f"  [muted]{r.stdout.strip()}[/muted]")
    return r


def ensure_all() -> None:
    """A Tidepool for every User (idempotent: creates what is missing, re-applies caps)."""
    for uid, name in auth.guise_accounts():
        _helper("create", name, str(uid))


def remove_user(name: str) -> None:
    _helper("remove", name, check=False)


def drop_kept_images() -> None:
    """The images an earlier teardown kept (config.TIDEPOOL_KEPT lists exact paths), then the list."""
    imgs = json.loads(config.TIDEPOOL_KEPT.read_text())
    if imgs:
        run(["rm", "-f", *imgs], sudo=True, check=False)
    run(["rm", "-f", str(config.TIDEPOOL_KEPT)], sudo=True, check=False)


def teardown(all_: bool) -> None:
    """Every Tidepool stopped and unmounted (images kept unless `all_`), then CCC's shared pieces."""
    if config.TIDEPOOL_HELPER_BIN.exists():
        _helper("teardown", *(["--all"] if all_ else []), check=False)
    elif all_ and config.TIDEPOOL_KEPT.exists():    # an earlier teardown kept them, helper and all gone
        drop_kept_images()
    system.deny_tcp_port_range(config.TIDEPOOL_SSH_PORT_BASE + 1, _port_count())
    system.deny_dns_from(config.TIDEPOOL_BRIDGE_SUBNETS)
    run(["systemctl", "stop", config.TIDEPOOL_SLICE], sudo=True, check=False)
    run(["systemctl", "disable", "--now", f"{config.REGISTRY_GC_SERVICE}.timer"], sudo=True, check=False)
    run(["rm", "-rf", str(config.REGISTRY_GC_UNIT), str(config.REGISTRY_GC_TIMER),
         str(config.DIVE_RECORDS), str(config.DIVE_RSYNCD), str(config.DIVE_STATE)], sudo=True,
        check=False)
    run(["rm", "-rf", str(config.TIDEPOOL_HELPER_BIN), str(config.SUDOERS_CCC_TIDEPOOL),
         str(config.TIDEPOOL_FILES_DIR), str(config.TIDEPOOL_UNIT), str(config.TIDEPOOL_SLICE_UNIT),
         str(config.TIDEPOOL_RECORDS), str(config.TIDEPOOL_SETTINGS)], sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


# --- any box: the User's own Tidepool -----------------------------------------------

def _ensure_key() -> str:
    """`~/.ssh/ccc_ed25519`, made once; returns the public line."""
    key = config.TIDEPOOL_KEY
    if not key.exists():
        config.SSH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                        f"ccc@{system.hostname()}", "-f", str(key)], check=True)
    return key.with_suffix(".pub").read_text().strip()


def _head_host() -> str:
    return urlsplit(api._head_url()).hostname


def connect() -> dict:
    """Make sure this box can `ssh <name>`: key uploaded, alias written, host key pinned."""
    pub = _ensure_key()
    have = {k["pubkey"] for k in auth._api("GET", "/ssh-keys")}
    if pub not in have:
        auth._api("POST", "/ssh-keys", body={"pubkey": pub, "name": f"cuttle@{system.hostname()}"})
    tp = auth._api("GET", "/tidepool")
    meta = {"host": _head_host(), "port": tp["port"], "user": tp["user"]}
    config.ensure_ccc_home()
    config.TIDEPOOL_SSH_META.write_text(json.dumps(meta) + "\n")
    if tp.get("host_key"):
        config.TIDEPOOL_KNOWN_HOSTS.write_text(f"{config.TIDEPOOL_HOSTKEY_ALIAS} {tp['host_key']}\n")
    sshconfig.refresh()
    return tp


def open_dive_lines(rows: list[dict]) -> list[str]:
    """One line per open Dive, for the Tidepool's entry: where it is and what it holds."""
    out = []
    for r in rows:
        if r["state"].lower() == "running":
            out.append(f"Dive {r['id']} on {r.get('node') or '?'} · ~/{r['main']} read-only here until it "
                       f"ends · `cuttle dive attach {r['id']}`")
        else:
            out.append(f"Dive {r['id']} {r['state'].lower()} · ~/{r['main']}")
    return out


def enter() -> None:
    """Refresh the alias (port/host key may have changed at a reset or re-setup), then ssh in, with
    this folder forwarded for `cuttle upload/download` inside (transfer.Forward)."""
    tp = connect()
    if tp["state"] != "active":
        raise ui.abort(f"your Tidepool isn't running ({tp['state']})",
                       next_step="`cuttle tidepool restart`, or ask an admin")
    name = config.cluster_name()
    with transfer.Forward(name) as fwd:
        ui.ok(f"Tidepool {tp['user']}@{name} — {tp['used_gb']:.1f} / {tp['size_gb']} GB used")
        ui.hint("no GPU in the Tidepool: GPU work runs in a Dive (`cuttle dive`)")
        for line in open_dive_lines(auth._api("GET", "/dives") or []):
            ui.caution(line)
        ui.hint(f"`cuttle download <path>` sends to {fwd.root}")
        ui.hint(f"`cuttle upload <path>` fetches from {fwd.root}")
        ui.hint("type `exit` to leave — the Tidepool keeps running either way")
        with ui.handover():
            rc = subprocess.run(["ssh", *fwd.ssh_args(), name]).returncode
        if fwd.handoff:   # `cuttle dive` in there closed the session to send you on (dive._attach_inside)
            from . import dive
            return dive.attach(fwd.handoff)
        if rc == 255:   # ssh's own failure; any other code is the shell's last command
            raise ui.abort("couldn't connect to your Tidepool (ssh exit 255)",
                           next_step="`cuttle tidepool restart`, or ask an admin")


def _act(verb: str, user: str | None) -> None:
    """Restart/reset: your own via the Head API; `--user` (operator, on the Head) via the helper."""
    if user:
        _helper(verb, user)
    else:
        auth._api("POST", f"/tidepool/{verb}")
    ui.ok(f"Tidepool {verb} done" if verb == "reset" else "Tidepool restarted")


def restart(user: str | None = None) -> None:
    _act("restart", user)


def reset(user: str | None = None) -> None:
    _act("reset", user)


def status(as_json: bool = False) -> None:
    tp = auth._api("GET", "/tidepool")
    ui.emit(tp, as_json, lambda: ui.kv([
        ("state", tp["state"]), ("used", f"{tp['used_gb']:.1f} / {tp['size_gb']} GB"),
        ("enter", "cuttle tidepool"), ("editors (ssh host)", config.cluster_name()),
        ("port", tp["port"])]))


def keys(as_json: bool = False) -> None:
    rows = auth._api("GET", "/ssh-keys")
    ui.emit(rows, as_json, lambda: ui.table(
        ["id", "name", "key"], [[k["id"], k["name"] or "", k["pubkey"][:48] + "…"] for k in rows]))


def key_add(path: Path, name: str | None) -> None:
    pub = path.expanduser().read_text().strip()
    auth._api("POST", "/ssh-keys", body={"pubkey": pub, "name": name or path.name})
    ui.ok("key added")


def key_remove(key_id: int) -> None:
    auth._api("DELETE", f"/ssh-keys/{key_id}")
    ui.ok("key removed")
