"""`~/.ssh/ccc.config`: one real ssh `Host` alias per live **Tide Pool**, so anything that speaks ssh
— VS Code's Remote-SSH, `scp`, a plain `ssh ccc-tp-58` — can reach a Pod without anyone ever handling
a key or a port.

Nothing here is a second way to connect. `cuttle run --tidepool` still execs `ssh` with the flags
spelled out, because that path must work on a box whose `~/.ssh` is unwritable or hand-tuned. This
file exists for the tools that cannot be handed flags: VS Code takes a *host alias* and nothing else.

**Generated wholly, every open, from the sessions this box can still reach.** No merge, no in-place
edit, no marker-delimited region to get out of step — the file is rewritten from `TIDEPOOL_DIR`, and a
Spawn the Head no longer lists as live is not reproduced (and its key is deleted). That is what keeps
it bounded on a box someone opens a Tide Pool from every day, and it is why cuttle needs exactly one
line in the User's own `~/.ssh/config` and never a second.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import config, ui

# A prefix per kind, because the two namespaces genuinely collide: a **Workspace** may legally be
# named `58` (workspaces.NAME_RE admits it) and so may a Spawn id. `ccc-` first so a User's own hosts
# sort away from these, and so `ssh ccc-<tab>` lists the live sessions and nothing else.
WS_PREFIX = "ccc-ws-"
TP_PREFIX = "ccc-tp-"
INCLUDE_LINE = "Include ccc.config"
_HEADER = ("# Written by cuttle. Regenerated in full on every Tide Pool or Workspace open —\n"
           "# anything you add here is lost. Edit ~/.ssh/config instead.\n")


def alias(spawn_id: int, workspace: str | None) -> str:
    """What to type. A visit is addressed by its Workspace's NAME rather than the visiting Spawn's id,
    for the same reason `cuttle workspace open <name>` is how it is rejoined: the id changes every
    visit, and an alias that changed with it would be one nobody could put in a bookmark."""
    return f"{WS_PREFIX}{workspace}" if workspace else f"{TP_PREFIX}{spawn_id}"


def _dir() -> Path:
    config.ensure_ccc_home()
    config.TIDEPOOL_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.TIDEPOOL_DIR.chmod(0o700)
    return config.TIDEPOOL_DIR


def key_path(spawn_id: int) -> Path:
    return _dir() / f"{spawn_id}.key"


def _meta_path(spawn_id: int) -> Path:
    return _dir() / f"{spawn_id}.json"


def record(spawn_id: int, *, private_key: str, port: int, user: str, host: str,
           workspace: str | None, proj: str | None) -> Path:
    """Put this session's key and connection details on disk, and return the key's path.

    The key is written 0600 before its bytes go in (`os.open` with the mode, not a chmod afterwards) —
    ssh refuses a group-readable IdentityFile outright, and a private key that is briefly world-
    readable on a shared box has already leaked by the time the chmod lands.
    """
    path = key_path(spawn_id)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(private_key if private_key.endswith("\n") else private_key + "\n")
    _meta_path(spawn_id).write_text(json.dumps({
        "id": spawn_id, "port": port, "user": user, "host": host,
        "workspace": workspace, "proj": proj,
    }) + "\n")
    return path


def forget(spawn_id: int) -> None:
    key_path(spawn_id).unlink(missing_ok=True)
    _meta_path(spawn_id).unlink(missing_ok=True)


def _sessions() -> list[dict]:
    rows = []
    for meta in sorted(_dir().glob("*.json")):
        try:
            rows.append(json.loads(meta.read_text()))
        except (ValueError, OSError):
            meta.unlink(missing_ok=True)   # unreadable is indistinguishable from absent, so drop it
    return rows


def _block(row: dict) -> str:
    """One Host entry. The options mirror `tidepool._wait_and_attach`'s `ssh` flags exactly, and for
    the same reasons: the Pod mints a fresh host key every session, so there is nothing to pin and a
    known_hosts entry would be a warning every time; `IdentitiesOnly` stops an agent with a dozen keys
    from exhausting `MaxAuthTries` before the one that works is offered."""
    return "\n".join([
        f"Host {alias(row['id'], row.get('workspace'))}",
        f"  HostName {row['host']}",
        f"  Port {row['port']}",
        f"  User {row['user']}",
        f"  IdentityFile {key_path(row['id'])}",
        "  IdentitiesOnly yes",
        "  StrictHostKeyChecking no",
        "  UserKnownHostsFile /dev/null",
        "  LogLevel ERROR",
        "  ServerAliveInterval 30",
    ]) + "\n"


def refresh(live: set[int] | None = None) -> None:
    """Prune dead sessions and rewrite `~/.ssh/ccc.config` from what is left.

    `live` is the set of Spawn ids the Head still has in its queue; `None` means we could not ask, in
    which case nothing is pruned. That asymmetry is deliberate — a Head that is down or unreachable is
    not evidence that a session ended, and deleting the key on the strength of a failed request would
    lock a User out of a Pod that is still running and still billing them.

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
        body = _HEADER + "\n" + "\n".join(_block(r) for r in rows)
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
    config.SSH_CONFIG.write_text(f"{INCLUDE_LINE}   # cuttle: live CCC tide pools\n\n{existing}")
    config.SSH_CONFIG.chmod(0o600)


def open_vscode(name: str, proj: str | None) -> bool:
    """Launch VS Code straight into the Pod. False (with a reason) if it could not be.

    `--remote ssh-remote+<alias> <folder>` is VS Code's own documented spelling, and it is the whole
    integration: Remote-SSH reads `~/.ssh/config`, finds the alias `refresh` just wrote, and installs
    its server under `VSCODE_AGENT_FOLDER=/ccc-tmp/vscode` (sshd's SetEnv — the 64MB home tmpfs cannot
    hold the ~150MB server).
    """
    exe = shutil.which("code")
    if not exe:
        ui.warn("VS Code's `code` command is not on PATH — open it yourself and pick the host below "
                "(VS Code: Command Palette -> 'Connect to Host'). In VS Code, "
                "'Shell Command: Install code command in PATH' adds it.")
        return False
    argv = [exe, "--remote", f"ssh-remote+{name}"] + ([proj] if proj else [])
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
    if not config.SSH_CONFIG.is_file():
        return
    lines = config.SSH_CONFIG.read_text().splitlines(keepends=True)
    kept = [l for l in lines if l.split("#", 1)[0].strip() != INCLUDE_LINE]
    if len(kept) != len(lines):
        config.SSH_CONFIG.write_text("".join(kept).lstrip("\n"))
