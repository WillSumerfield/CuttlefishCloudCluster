"""`~/.ssh/ccc.config`: the **Tidepool** alias (`Host <name>`, written at `cuttle login`) plus one
`Host <name>-<id>` per live **Dive**, so anything that speaks ssh — VS Code's Remote-SSH, `scp`, a plain
`ssh <name>-58` — reaches it without anyone handling a port.

A Dive answers with the Tidepool's own host key and takes the User's own keys, so every alias pins
the same known_hosts entry and uses the same `~/.ssh/ccc_ed25519`; only HostName/Port differ (every
Dive is reached at its own port on the Head: a relay for an Arm Dive, the container itself on the Head).

**Generated wholly, every time, from the Dives this box knows of** (DIVE_DIR). A Dive the Head no
longer lists as live is not reproduced, so the file stays bounded, and cuttle needs exactly one line
in the User's own `~/.ssh/config`.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import config, ui

INCLUDE_LINE = "Include ccc.config"
_HEADER = ("# Written by cuttle. Regenerated in full at login and every Dive —\n"
           "# anything you add here is lost. Edit ~/.ssh/config instead.\n")


def alias(dive_id: int) -> str:
    """What to type: `<name>-58`."""
    return f"{config.cluster_name()}-{dive_id}"


def _dir() -> Path:
    config.ensure_ccc_home()
    config.DIVE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    return config.DIVE_DIR


def _meta_path(dive_id: int) -> Path:
    return _dir() / f"{dive_id}.json"


def record(dive_id: int, *, port: int, user: str, host: str) -> None:
    _meta_path(dive_id).write_text(json.dumps({"id": dive_id, "port": port, "user": user,
                                               "host": host}) + "\n")


def forget(dive_id: int) -> None:
    _meta_path(dive_id).unlink(missing_ok=True)


def _sessions() -> list[dict]:
    rows = []
    for meta in sorted(_dir().glob("*.json")):
        try:
            rows.append(json.loads(meta.read_text()))
        except (ValueError, OSError):
            meta.unlink(missing_ok=True)   # unreadable is indistinguishable from absent, so drop it
    return rows


def _pinned(name: str, host: str, port: int, user: str) -> str:
    """A Host entry for the Tidepool or one of its Dives: the User's CCC key, the Tidepool's host key
    pinned (no TOFU prompt, no StrictHostKeyChecking=no) via HostKeyAlias, so it holds whatever
    address this box reaches the Head at."""
    return "\n".join([
        f"Host {name}",
        f"  HostName {host}",
        f"  Port {port}",
        f"  User {user}",
        f"  IdentityFile {config.TIDEPOOL_KEY}",
        "  IdentitiesOnly yes",
        f"  HostKeyAlias {config.TIDEPOOL_HOSTKEY_ALIAS}",
        f"  UserKnownHostsFile {config.TIDEPOOL_KNOWN_HOSTS}",
        "  StrictHostKeyChecking yes",
        "  ForwardAgent yes",
        "  ServerAliveInterval 30",
    ]) + "\n"


def _block(row: dict) -> str:
    return _pinned(alias(row["id"]), row["host"], row["port"], row["user"])


def tidepool_block() -> str:
    """`Host <name>` → the User's Tidepool on the Head (`cuttle login` wrote its meta + host key)."""
    try:
        m = json.loads(config.TIDEPOOL_SSH_META.read_text())
    except (OSError, ValueError):
        return ""
    return _pinned(config.cluster_name(), m["host"], m["port"], m["user"])


def refresh(live: set[int] | None = None) -> None:
    """Prune dead sessions and rewrite `~/.ssh/ccc.config` from what is left.

    `live` is the set of Dive ids the Head still lists; `None` means we could not ask, in which case
    nothing is pruned — an unreachable Head is not evidence that a Dive ended.

    Best effort throughout: a `~/.ssh` that cannot be written is a missing convenience, never a reason
    to fail an attach that is otherwise working.
    """
    rows = _sessions()
    if live is not None:
        for row in rows:
            if row["id"] not in live:
                forget(row["id"])
        rows = [r for r in rows if r["id"] in live]
    try:
        config.SSH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        body = _HEADER + "\n" + "\n".join([b for b in [tidepool_block()] if b] +
                                            [_block(r) for r in rows])
        config.SSH_CCC_CONFIG.write_text(body)
        config.SSH_CCC_CONFIG.chmod(0o600)
        _ensure_include()
    except OSError:
        if ui.DEBUG:
            raise


def _ensure_include() -> None:
    """One `Include ccc.config` line at the TOP of `~/.ssh/config`, if it is not already somewhere in
    it. Top, not bottom, because ssh takes the FIRST value it sees for a keyword — a `Host *` block at
    the head of a hand-written config would otherwise win over everything `_block` sets, and the one
    that matters is `IdentityFile`.

    A relative path: ssh resolves it against `~/.ssh` (man ssh_config), so this line is the same on
    every box and survives the file being copied between them.
    """
    existing = config.SSH_CONFIG.read_text() if config.SSH_CONFIG.is_file() else ""
    # Compared with the comment stripped, exactly as ssh reads the file: the line cuttle writes
    # carries one, and a User who added the Include themselves will not have written ours.
    if any(line.split("#", 1)[0].strip() == INCLUDE_LINE for line in existing.splitlines()):
        return
    config.SSH_CONFIG.write_text(f"{INCLUDE_LINE}   # cuttle: your CCC Tidepool\n\n{existing}")
    config.SSH_CONFIG.chmod(0o600)


# --- WSL: `code` there is Windows VS Code, whose Remote-SSH runs Windows' ssh — blind to this alias,
# key, pinned host key and the Head's name, all of which live in WSL. So point its `remote.SSH.path`
# at a wrapper running WSL's ssh. Global to that VS Code: every Remote-SSH host then goes via WSL.
WSL_SSH_BAT = "ccc-wsl-ssh.bat"


def _wsl_code(exe: str) -> bool:
    return exe.startswith("/mnt/") and bool(os.environ.get("WSL_DISTRO_NAME"))


def _win_dir(var: str) -> Path | None:
    try:
        win = subprocess.run(["cmd.exe", "/c", f"echo %{var}%"], capture_output=True, text=True,
                             timeout=10, cwd="/mnt/c").stdout.strip()
        return Path(subprocess.run(["wslpath", "-u", win], capture_output=True, text=True,
                                   timeout=10).stdout.strip()) if win else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def wsl_route(appdata: Path | None = None, profile: Path | None = None) -> bool:
    """Wrapper written, VS Code's settings pointed at it. False (with what to do) if it could not be."""
    appdata, profile = appdata or _win_dir("APPDATA"), profile or _win_dir("USERPROFILE")
    if not appdata or not profile:
        ui.warn("could not find your Windows profile from WSL — VS Code there can't reach the Dive")
        return False
    bat = profile / ".ccc" / WSL_SSH_BAT
    bat.parent.mkdir(parents=True, exist_ok=True)
    bat.write_text(f"@wsl.exe -d {os.environ['WSL_DISTRO_NAME']} -e ssh %*\r\n")
    win_bat = subprocess.run(["wslpath", "-w", str(bat)], capture_output=True, text=True).stdout.strip()
    settings = appdata / "Code" / "User" / "settings.json"
    text = settings.read_text() if settings.exists() else ""
    try:
        cur = json.loads(text or "{}")
    except ValueError:   # JSONC (comments): one line inserted after the `{`, the rest kept byte for byte
        return _jsonc_insert(settings, text, win_bat)
    have = cur.get("remote.SSH.path")
    if have == win_bat:
        return True
    if have:
        ui.warn(f"VS Code's remote.SSH.path is already {have!r} — left alone; set it to "
                f"{win_bat!r} for VS Code to use WSL's ssh (and so reach Dives)")
        return False
    cur["remote.SSH.path"] = win_bat
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(cur, indent=4) + "\n")
    ui.ok("VS Code (Windows) now uses WSL's ssh for Remote-SSH, so it can reach Dives")
    return True


def _jsonc_insert(settings: Path, text: str, win_bat: str) -> bool:
    if json.dumps(win_bat) in text:
        return True
    i = text.find("{")
    if '"remote.SSH.path"' in text or i < 0:
        ui.warn(f"VS Code's settings.json already sets remote.SSH.path (or isn't an object) — left "
                f'alone; set it to {json.dumps(win_bat)} for VS Code to use WSL\'s ssh')
        return False
    # A trailing comma before `}` is fine in VS Code's JSONC.
    settings.write_text(text[:i + 1] + f'\n    "remote.SSH.path": {json.dumps(win_bat)},' + text[i + 1:])
    ui.ok("VS Code (Windows) now uses WSL's ssh for Remote-SSH, so it can reach Dives")
    return True


def open_vscode(name: str, proj: str | None) -> bool:
    """Launch VS Code straight into a Dive. A folder URI, not a path: WSL's `code` shim would turn a
    path into a Windows one. False (with a reason) if it could not be."""
    exe = shutil.which("code")
    if not exe:
        ui.warn("VS Code's `code` command is not on PATH — open it yourself and pick the host below "
                "(VS Code: Command Palette -> 'Connect to Host'). In VS Code, "
                "'Shell Command: Install code command in PATH' adds it.")
        return False
    if _wsl_code(exe) and not wsl_route():
        return False
    argv = [exe, "--folder-uri" if proj else "--remote",
            f"vscode-remote://ssh-remote+{name}{proj}" if proj else f"ssh-remote+{name}"]
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        ui.warn(f"could not launch VS Code: {e}")
        return False
    return True


def remove() -> None:
    """Undo everything this module ever wrote outside `~/.ccc` — for `cuttle teardown`, which removes
    the keys along with the rest of ~/.ccc and would otherwise leave aliases pointing at nothing.

    The `Include` goes too. A missing include file is not fatal to modern ssh, but "CCC leaves nothing
    behind" is the promise, and a line in a User's own config that names a file CCC deleted is
    something left behind. Only the line naming OUR file is dropped; every other line survives byte
    for byte.
    """
    config.SSH_CCC_CONFIG.unlink(missing_ok=True)
    config.TIDEPOOL_KEY.unlink(missing_ok=True)
    config.TIDEPOOL_KEY.with_suffix(".pub").unlink(missing_ok=True)
    if not config.SSH_CONFIG.is_file():
        return
    lines = config.SSH_CONFIG.read_text().splitlines(keepends=True)
    kept = [l for l in lines if l.split("#", 1)[0].strip() != INCLUDE_LINE]
    if len(kept) != len(lines):
        config.SSH_CONFIG.write_text("".join(kept).lstrip("\n"))
