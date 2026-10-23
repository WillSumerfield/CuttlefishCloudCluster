"""The Dashboard's **file browser** over the caller's own Tidepool home.

This process (`ccc`) cannot read Tidepool files; every op is the root helper's `fs` verb, which runs
it as the User inside their own container (`podman exec -u`), so their permissions and symlinks
apply. Paths are from the Tidepool home. Main folders a Dive holds are **locked**: listed as such,
and anything that would write into one is refused (it is read-only in the container anyway).
"""

import subprocess

from fastapi import HTTPException

from . import db
from .tidepool import HELPER


def check(rel: str) -> str:
    rel = rel.strip("/")
    if rel in ("", "."):
        return "."
    if "\0" in rel or len(rel) > 4096 or any(c in ("", ".", "..") for c in rel.split("/")):
        raise HTTPException(422, f"{rel!r} is not a path in your Tidepool home")
    return rel


def argv(user: str, op: str, *rels: str) -> list[str]:
    return ["sudo", "-n", HELPER, "fs", user, op, *rels]


def _run(user: str, op: str, *rels: str, **kw) -> subprocess.CompletedProcess:
    r = subprocess.run(argv(user, op, *rels), capture_output=True, **kw)
    if r.returncode:
        err = r.stderr.decode(errors="replace") if isinstance(r.stderr, bytes) else r.stderr
        raise HTTPException(422, err.strip().removeprefix("ccc-tidepool: ") or f"{op} failed")
    return r


def stat(user: str, rel: str) -> tuple[str, str] | None:
    """(type, followed type) — find's %y/%Y letters (d, f, l…) — or None if nothing is there."""
    r = subprocess.run(argv(user, "stat", rel), capture_output=True, text=True)
    if r.returncode or not r.stdout.strip():
        return None
    y, _, big_y = r.stdout.strip().partition(" ")
    return y, big_y or y


def locks(user_id: int) -> list[str]:
    with db.connect() as conn:
        return [d["main"] for d in db.open_dives(conn, user_id)]


def locked_by(rel: str, mains: list[str]) -> str | None:
    return next((m for m in mains if rel == m or rel.startswith(m + "/")), None)


def refuse_locked(user: dict, *rels: str) -> None:
    mains = locks(user["id"])
    for rel in rels:
        if m := locked_by(rel, mains):
            raise HTTPException(423, f"~/{m} is held by a running Dive — read-only until it ends")


_KIND = {"d": "dir", "f": "file", "l": "link"}


def listing(user: dict, rel: str) -> list[dict]:
    raw = _run(user["username"], "list", rel).stdout.split(b"\0")
    mains = locks(user["id"])
    out = []
    for i in range(0, len(raw) - 3, 4):
        y, size, mtime, name = (x.decode(errors="replace") for x in raw[i:i + 4])
        path = name if rel == "." else f"{rel}/{name}"
        out.append({"name": name, "path": path, "type": _KIND.get(y, "other"),
                    "size": int(size) if y == "f" else None, "mtime": float(mtime),
                    "locked": path in mains})
    return sorted(out, key=lambda e: (e["type"] != "dir", e["name"].lower()))


def stream(user: str, op: str, rel: str):
    """Chunks of `get` / `zip` stdout; a client that disconnects kills the helper."""
    p = subprocess.Popen(argv(user, op, rel), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        yield from iter(lambda: p.stdout.read(1 << 16), b"")
    finally:
        p.kill()
        p.wait()


def mkdir(user: dict, rel: str) -> None:
    refuse_locked(user, rel)
    _run(user["username"], "mkdir", rel)


def delete(user: dict, rel: str) -> None:
    if rel == ".":
        raise HTTPException(422, "not your home folder itself")
    refuse_locked(user, rel)
    if any(m.startswith(rel + "/") for m in locks(user["id"])):
        raise HTTPException(423, f"~/{rel} holds a folder a running Dive is using")
    _run(user["username"], "delete", rel)


def rename(user: dict, rel: str, to: str) -> None:
    if "." in (rel, to):
        raise HTTPException(422, "not your home folder itself")
    refuse_locked(user, rel, to)
    if any(m.startswith(rel + "/") for m in locks(user["id"])):
        raise HTTPException(423, f"~/{rel} holds a folder a running Dive is using")
    if stat(user["username"], to):
        raise HTTPException(409, f"~/{to} already exists")
    _run(user["username"], "rename", rel, to)
