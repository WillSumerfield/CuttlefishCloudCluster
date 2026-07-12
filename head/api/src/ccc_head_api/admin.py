"""Local user-admin for the Head auth store (Phase 3a slice 1).

Runs *on the Head as the `ccc` service account* (`python -m ccc_head_api.admin ...`), writing head.db
directly — being the operator on the Head is the authorization, no HTTP/session (mirrors the way
`cuttle arm approve` mutates the registry). `cuttle user add|list|remove` are thin wrappers that shell
into this via the API venv; `setup --head` uses `add --admin` to seed the first admin.

Prints the one-time temp password to stdout — the operator hands it to the user, who must rotate it on
first login. Nothing here ever prints or stores a reusable password.
"""

import argparse
import sys

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


def _remove(username: str) -> int:
    with db.connect() as conn:
        db.init_schema(conn)
        if not db.remove_user(conn, username):
            print(f"no such user {username!r}", file=sys.stderr)
            return 1
    print(f"removed {username!r}")
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

    args = p.parse_args(argv)
    if args.cmd == "add":
        return _add(args.username, args.admin)
    if args.cmd == "list":
        return _list()
    if args.cmd == "remove":
        return _remove(args.username)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
