"""`cuttle upload` / `cuttle download`: files between your own machine and your Tidepool or a Dive.

Your machine always runs the rsync, against the ssh alias (`<name>` / `<name>-<id>`). Two ways in:
- **From your machine**: container paths are from the Tidepool home (`proj` ≡ `~/proj`, absolute
  allowed); yours are from the cwd.
- **From inside** (a Tidepool or Dive shell): `cuttle tidepool` / `cuttle dive attach` no longer exec
  ssh — they listen on a local Unix socket, forward it in (`ssh -R`, path in $CCC_FWD), and serve
  copy requests on it. Your machine's side is confined to the **entry folder** (the cwd you entered
  from): `..`, absolute paths and symlinks out of it are refused. No socket (plain ssh, an editor's
  terminal) → enter via cuttle.

Copies are cp-like: the named item lands INSIDE dest (trailing slash or not), recursive, resumable
(partials kept in .cuttle-partial/), the folder's .cuttleignore honoured, never deletes. A file that
already exists at dest (and differs) refuses the copy and is listed; `--force` overwrites.
"""

import json
import os
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from . import config, ui

FWD_ENV = "CCC_FWD"
PARTIAL_DIR = ".cuttle-partial"
CLASH_RC = 3


# --- the copy (always on your own machine) ------------------------------------------

def rsync_argv(srcs: list[str], dest: str, dry: bool = False) -> list[str]:
    a = ["rsync", "-a", f"--partial-dir={PARTIAL_DIR}", "--mkpath", "--filter=:- .cuttleignore",
         "-e", "ssh -o BatchMode=yes"]
    a += ["--dry-run", "--existing", "--out-format=%n"] if dry else ["--info=progress2", "-h"]
    return a + [s.rstrip("/") or "/" for s in srcs] + [dest.rstrip("/") + "/"]


def clashes(srcs: list[str], dest: str) -> tuple[int, list[str], str]:
    """Files the copy would overwrite (exist at dest and differ): (rc, files, stderr)."""
    r = subprocess.run(rsync_argv(srcs, dest, dry=True), capture_output=True, text=True)
    files = [ln for ln in r.stdout.splitlines() if ln and not ln.endswith("/")]
    return r.returncode, files, r.stderr


def copy(srcs: list[str], dest: str, force: bool, write: Callable[[str], None]) -> int:
    if not force:
        rc, found, err = clashes(srcs, dest)
        if rc:
            write(err)
            return rc
        if found:
            shown = "".join(f"  {f}\n" for f in found[:20])
            more = f"  … and {len(found) - 20} more\n" if len(found) > 20 else ""
            write(f"cuttle: already there, copy refused (--force overwrites):\n{shown}{more}")
            return CLASH_RC
    p = subprocess.Popen(rsync_argv(srcs, dest), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for chunk in iter(lambda: p.stdout.read1(4096), b""):
        write(chunk.decode(errors="replace"))
    rc = p.wait()
    write("\n")
    return rc


def _done(rc: int) -> None:
    if rc == CLASH_RC:
        raise ui.abort("nothing copied")
    if rc:
        raise ui.abort(f"copy failed (rsync exit {rc}) — run it again to resume")
    ui.ok("copied")


def _out(s: str) -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


# --- from your own machine -----------------------------------------------------------

def container_path(p: str) -> str:
    """From your machine: relative = from the Tidepool home (rsync's remote cwd); absolute as given."""
    if p in ("~", "~/"):
        return "."
    return p[2:] if p.startswith("~/") else p


def _target(dive_id: int | None) -> tuple[str, str]:
    """(alias, default upload dest): the Tidepool home, or the Dive's main folder."""
    if dive_id is None:
        from . import tidepool
        tidepool.connect()
        return config.cluster_name(), "."
    from . import dive
    alias, info = dive.ensure_alias(dive_id)
    return alias, info["main"]


def download(path: str, dest: str | None, dive_id: int | None, force: bool) -> None:
    if inside():
        if dive_id is not None:
            raise ui.abort("in here, paths are this container's — leave out --dive")
        return _request("download", [os.path.abspath(path)], dest or ".", force)
    alias, _ = _target(dive_id)
    _done(copy([f"{alias}:{container_path(path)}"], dest or ".", force, _out))


def upload(path: str, dest: str | None, dive_id: int | None, force: bool) -> None:
    if inside():
        if dive_id is not None:
            raise ui.abort("in here, paths are this container's — leave out --dive")
        return _request("upload", [path], os.path.abspath(dest or "."), force)
    alias, default = _target(dive_id)
    if not Path(path).exists():
        raise ui.abort(f"{path} does not exist")
    _done(copy([path], f"{alias}:{container_path(dest or default)}", force, _out))


# --- from inside: the client end of the socket ----------------------------------------

def inside() -> bool:
    from . import dive
    return dive.in_container()


def _request(op: str, src: list[str], dest: str, force: bool) -> None:
    path = os.environ.get(FWD_ENV)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if not path:
            raise OSError
        s.connect(path)
    except OSError:
        raise ui.abort("this shell has no link to your machine",
                       next_step="enter via `cuttle tidepool` (or `cuttle dive attach`) from it") from None
    with s, s.makefile("rwb") as f:
        f.write(json.dumps({"op": op, "src": src, "dest": dest, "force": force}).encode() + b"\n")
        f.flush()
        rc = 1
        for line in f:
            msg = json.loads(line)
            if "out" in msg:
                _out(msg["out"])
            else:
                rc = msg["rc"]
                if msg.get("error"):
                    raise ui.abort(msg["error"])
    _done(rc)


# --- your machine's end: the forwarded socket -----------------------------------------

class Refused(Exception):
    pass


def confine(root: Path, rel: str) -> str:
    """A path on your machine, from inside: under the entry folder or refused."""
    if rel.startswith(("/", "~")):
        raise Refused(f"{rel}: paths on your machine are relative to {root}")
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise Refused(f"{rel} is outside {root}, the folder you entered from")
    return str(p)


def serve(req: dict, alias: str, root: Path, write: Callable[[str], None]) -> int:
    op, src, dest, force = req["op"], list(req["src"]), str(req["dest"]), bool(req.get("force"))
    if op == "download":
        if not all(p.startswith("/") for p in src):
            raise Refused("bad request")
        return copy([f"{alias}:{p}" for p in src], confine(root, dest), force, write)
    if op == "upload":
        if not dest.startswith("/"):
            raise Refused("bad request")
        local = [confine(root, p) for p in src]
        missing = [p for p in local if not os.path.lexists(p)]
        if missing:
            raise Refused(f"{missing[0]} does not exist")
        return copy(local, f"{alias}:{dest}", force, write)
    raise Refused("bad request")


class Forward:
    """What `cuttle tidepool` / `dive attach` hold open around ssh: a socket here, forwarded in."""

    def __init__(self, alias: str, root: Path | None = None):
        self.alias, self.root = alias, (root or Path.cwd()).resolve()
        self.remote = f"/tmp/ccc-fwd-{secrets.token_hex(8)}.sock"

    def __enter__(self) -> "Forward":
        run = config.CCC_HOME / "run"
        run.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.local = run / f"fwd-{os.getpid()}.sock"
        self.local.unlink(missing_ok=True)
        fwd = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                def write(s: str) -> None:
                    self.wfile.write(json.dumps({"out": s}).encode() + b"\n")
                    self.wfile.flush()
                try:
                    rc = serve(json.loads(self.rfile.readline()), fwd.alias, fwd.root, write)
                    end = {"rc": rc}
                except (Refused, ValueError, KeyError, TypeError) as e:
                    end = {"rc": 2, "error": str(e) or "bad request"}
                self.wfile.write(json.dumps(end).encode() + b"\n")

        self.server = socketserver.ThreadingUnixStreamServer(str(self.local), Handler)
        self.local.chmod(0o600)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def ssh_args(self) -> list[str]:
        return ["-R", f"{self.remote}:{self.local}", "-o", f"SetEnv={FWD_ENV}={self.remote}"]

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.local.unlink(missing_ok=True)
