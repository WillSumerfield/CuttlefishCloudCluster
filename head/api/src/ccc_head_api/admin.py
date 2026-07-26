"""Local admin for the Head: users, Karma Overrides, and pinned Spawns (Phase 3a slice 1; widened
in Phase 6).

Runs *on the Head as the `ccc` service account* (`python -m ccc_head_api.admin ...`), writing head.db
directly — being the operator on the Head is the authorization, no HTTP/session (mirrors the way
`cuttle arm authorize` mutates the registry). `cuttle user add|list|remove` are thin wrappers that shell
into this via the API venv; `setup --head` uses `add --admin` to seed the first admin.

Prints the one-time temp password to stdout — the operator hands it to the user, who must rotate it on
first login. Nothing here ever prints or stores a reusable password.
"""

import argparse
import sys
from pathlib import Path

from . import db


def _add(username: str, is_admin: bool) -> int:
    with db.connect() as conn:
        db.init_schema(conn)
        if db.get_user(conn, username):
            print(f"user {username!r} already exists", file=sys.stderr)
            return 1
        temp_pw = db.add_user(conn, username, is_admin=is_admin)
    role = "admin" if is_admin else "user"
    print(f"created {role} {username!r}")
    print(f"temp password: {temp_pw}")
    print("hand this to the user; they must change it on first login")
    return 0


def _list() -> int:
    with db.connect() as conn:
        db.init_schema(conn)
        rows = db.list_users(conn)
    if not rows:
        print("(no users)")
        return 0
    for r in rows:
        flags = " ".join(f for f, on in
                         (("admin", r["is_admin"]), ("must-change-pw", r["must_change_password"])) if on)
        print(f"{r['username']:<24} {flags}")
    return 0


def _exists(username: str) -> int:
    """Exit status only, no output — `cuttle arm authorize --user` uses it to reject an owner typo
    before minting an enrollment token."""
    with db.connect() as conn:
        db.init_schema(conn)
        return 0 if db.get_user(conn, username) else 1


def _remove(username: str) -> int:
    with db.connect() as conn:
        db.init_schema(conn)
        if not db.remove_user(conn, username):
            print(f"no such user {username!r}", file=sys.stderr)
            return 1
    print(f"removed {username!r}")
    return 0


def _override(username: str, amount: float, note: str | None) -> int:
    """Add or remove Karma from a User's Balance.

    Head-local since Phase 6. It needs no privilege beyond writing head.db — which is exactly why it
    was an API route until the phase settled that the Head API exposes **no admin writes at all**.
    Minting Karma is the most valuable thing a stolen Session could do, and the only way to make that
    impossible is for the capability not to be requestable over the network.
    """
    from . import karma
    with db.connect() as conn:
        db.init_schema(conn)
        try:
            balance = karma.override(conn, username, amount, note)
        except KeyError:
            print(f"no such user {username!r}", file=sys.stderr)
            return 1
        except ValueError as e:
            print(str(e), file=sys.stderr)  # the target is an Admin — outside the economy
            return 1
    print(f"{username} balance {balance:+.1f}")
    return 0


def _submit(username: str, script: str, tar: str, minutes: int, vram: int, gpu: int,
            node: str | None, args: list[str]) -> int:
    """Submit a Spawn pinned to a named Arm, attributed to `username`.

    Head-local since Phase 6 (`cuttle run --node`). Authorization is being the operator on the Head;
    attribution is the account named here. Goes through `spawns.submit_checked`, so the Escrow and the
    Attribution row are written exactly as they are for a Spawn that arrived over HTTP.
    """
    from . import spawns
    with db.connect() as conn:
        db.init_schema(conn)
        user = db.get_user(conn, username)
        if user is None:
            print(f"no such user {username!r}", file=sys.stderr)
            return 1
    try:
        job_id = spawns.submit_checked(user["id"], bool(user["is_admin"]), script, gpu, args, {},
                                       Path(tar), minutes, vram, node)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(job_id)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ccc_head_api.admin", description="Head auth user admin.")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="create a user (random temp password, forced change).")
    a.add_argument("username")
    a.add_argument("--admin", action="store_true", help="grant admin.")
    sub.add_parser("list", help="list users.")
    r = sub.add_parser("remove", help="delete a user (and their sessions, via cascade).")
    r.add_argument("username")
    e = sub.add_parser("exists", help="exit 0 if the user exists, 1 if not (no output).")
    e.add_argument("username")
    o = sub.add_parser("override", help="add/remove Karma from a User's Balance.")
    o.add_argument("username")
    o.add_argument("amount", type=float)
    o.add_argument("--note")
    s = sub.add_parser("submit", help="submit a Spawn pinned to an Arm (prints the Spawn id).")
    s.add_argument("--user", required=True, help="the account to attribute the Spawn to.")
    s.add_argument("--script", required=True)
    s.add_argument("--tar", required=True, help="gzip-tar of the project tree.")
    s.add_argument("--minutes", type=int, required=True)
    s.add_argument("--vram", type=int, required=True)
    s.add_argument("--gpu", type=int, default=1)
    s.add_argument("--node")
    s.add_argument("args", nargs="*", help="extra arguments for the script.")

    args = p.parse_args(argv)
    if args.cmd == "add":
        return _add(args.username, args.admin)
    if args.cmd == "list":
        return _list()
    if args.cmd == "remove":
        return _remove(args.username)
    if args.cmd == "exists":
        return _exists(args.username)
    if args.cmd == "override":
        return _override(args.username, args.amount, args.note)
    if args.cmd == "submit":
        return _submit(args.user, args.script, args.tar, args.minutes, args.vram, args.gpu,
                       args.node, args.args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
