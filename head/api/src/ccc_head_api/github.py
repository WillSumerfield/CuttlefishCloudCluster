"""Talking to GitHub with a minted Credential — the one thing the Head does with a key rather than to it.

`credentials.py` mints the keypair and seals it; this module is what proves the pair is any use, by
authenticating to GitHub with it exactly as a Spawn will. That is the whole point of `verify`: the
public half is only half a Credential until the User has pasted it into github.com/settings/keys, and
nothing on the Head can observe that they have. Without this route the first evidence would be a
`Permission denied (publickey)` buried in a Spawn's `err`, minutes or days after the click that caused
it, blamed on the job.

**The key never touches a filesystem, here or in a Spawn.** An ssh-agent is started on a private
socket, the key is fed to `ssh-add -` on stdin, and the agent dies with this function — the same
mechanism `spawns.py` uses inside the Pod, so a passing verify is evidence about the real path and not
just about this one.

The ssh invocation is deliberately hermetic: `-F /dev/null` and a scratch `HOME` so the Head operator's
own `~/.ssh/config`, keys and `known_hosts` cannot take part. That is not tidiness. This box very
plausibly has its own GitHub key in its own agent (the box CCC is developed on certainly does), and
without the isolation the route would cheerfully authenticate with it and report the operator's GitHub
username as the User's — a green tick for a Credential that is not registered anywhere.
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

HOST = "github.com"

# Pinned rather than accepted on first use: a Spawn nobody is watching cannot answer a host-key
# prompt, and TOFU inside one is not trust, it is a shrug. ed25519 alone — it is the one GitHub host
# key that has never been rotated, unlike the RSA key they replaced in March 2023 after it leaked.
HOST_KEY = ("github.com ssh-ed25519 "
            "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl")

# GitHub has no shell, so a *successful* `ssh -T` exits **1** and says hello on the way out. Both
# failure modes exit 255, which is why the two patterns below and not the exit code decide the answer.
_GREETING = re.compile(r"Hi ([^!]+)! You've successfully authenticated")
_REFUSED = "Permission denied"

_CONNECT_TIMEOUT = 10   # seconds, ssh's own; a Head with no route out should not hold the request open
_TIMEOUT = 25           # seconds, wall clock for the whole exchange


def verify(private_key: str) -> dict:
    """Authenticate to GitHub with `private_key`. Answers one of three states, never raises:

        {"status": "ok",           "username": "..."}   the key is registered, and to whom
        {"status": "unregistered", "detail": "..."}     GitHub refused it — the User has not pasted it in yet
        {"status": "error",        "detail": "..."}     nobody could tell: no ssh, no route, timed out

    The third state exists because both failures exit 255 identically. Folding it into the second
    would leave `cuttle setup` polling forever on a Head that simply has no way to reach github.com,
    and the operator watching a spinner that means "waiting for you" when it means "broken".
    """
    for tool in ("ssh", "ssh-agent", "ssh-add"):
        if shutil.which(tool) is None:
            return {"status": "error", "detail": f"this Head has no `{tool}` — install openssh-client"}

    with tempfile.TemporaryDirectory(prefix="ccc-gh-") as tmp:
        known_hosts = Path(tmp) / "known_hosts"
        known_hosts.write_text(HOST_KEY + "\n")
        sock = str(Path(tmp) / "agent.sock")
        # A scratch HOME, so `-F /dev/null` covering ssh_config is matched for everything else ssh
        # reaches for by convention. PATH is kept: this needs to find ssh.
        env = {"HOME": tmp, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "SSH_AUTH_SOCK": sock}

        # -D keeps the agent in the foreground on a socket we chose, which beats parsing `-s` output
        # for a PID we would then have to be trusted to kill. It dies with the `with` block either way.
        agent = subprocess.Popen(["ssh-agent", "-D", "-a", sock],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):                      # the socket appears in ~1ms; 500ms is generous
                if os.path.exists(sock):
                    break
                time.sleep(0.01)
            else:
                return {"status": "error", "detail": "the Head's ssh-agent did not start"}

            add = subprocess.run(["ssh-add", "-"], input=private_key, env=env, text=True,
                                 capture_output=True, timeout=_TIMEOUT)
            if add.returncode != 0:
                # Never `add.stderr`: it is the only place in this function that has seen the key, and
                # a mangled one could well be quoted back in the message.
                return {"status": "error", "detail": "the stored key could not be loaded"}

            try:
                out = subprocess.run(
                    ["ssh", "-T", "-F", "/dev/null",
                     "-o", "BatchMode=yes",
                     "-o", f"IdentityAgent={sock}",
                     "-o", f"UserKnownHostsFile={known_hosts}",
                     "-o", "StrictHostKeyChecking=yes",
                     "-o", "HostKeyAlgorithms=ssh-ed25519",
                     "-o", f"ConnectTimeout={_CONNECT_TIMEOUT}",
                     f"git@{HOST}"],
                    env=env, text=True, capture_output=True, timeout=_TIMEOUT)
            except subprocess.TimeoutExpired:
                return {"status": "error", "detail": f"{HOST} did not answer in {_TIMEOUT}s"}
        finally:
            agent.terminate()

    # GitHub's greeting arrives as the remote command's stderr, but reading both streams costs nothing
    # and saves this from depending on that.
    return interpret(f"{out.stderr}\n{out.stdout}")


def interpret(said: str) -> dict:
    """What ssh's output means, split out from running it because *this* is the part with the bug in
    it: the three states are told apart by two string matches against another project's wording."""
    if (hello := _GREETING.search(said)) is not None:
        return {"status": "ok", "username": hello.group(1)}
    if _REFUSED in said:
        return {"status": "unregistered",
                "detail": "GitHub refused the key — it is not on your account yet"}
    detail = next((ln.strip() for ln in said.splitlines() if ln.strip()), "ssh failed silently")
    log.warning("github verify failed: %s", detail)
    return {"status": "error", "detail": detail}
