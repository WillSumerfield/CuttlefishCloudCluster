"""Local admin for the Head: users, Karma Overrides, and Arm Retract/Return on someone else's behalf.

Runs *on the Head as the `ccc` service account* (`python -m ccc_head_api.admin ...`), writing head.db
directly — being the operator on the Head is the authorization, no HTTP/session. `cuttle user
add|list|remove|reset` are thin wrappers that shell into this via the API venv; `setup --head` uses `add
--admin` to seed the first admin.

Prints the one-time temp password to stdout — the operator hands it to the user, who must rotate it on
first login. Nothing here ever prints or stores a reusable password.
"""

import argparse
import re
import sys

from . import db, guises

# A username is also the User's Linux account inside their Tidepool container and part of its paths
# and unit names on the Head — so a plain lowercase Linux name, and none the base image already has.
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
RESERVED = {"root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail", "news", "uucp",
            "proxy", "www-data", "backup", "list", "irc", "gnats", "nobody", "messagebus", "sshd",
            "ubuntu", "ccc"}


def username_error(username: str) -> str | None:
    if not USERNAME_RE.match(username):
        return "usernames are lowercase letters, digits, '-' or '_', starting with a letter (max 32)"
    if username in RESERVED or username.startswith(("systemd-", "_")):
        return f"{username!r} is a system account name"
    return None


def _add(username: str, is_admin: bool) -> int:
    err = username_error(username)
    if err:
        print(err, file=sys.stderr)
        return 1
    with db.connect() as conn:
        db.init_schema(conn)
        if db.get_user(conn, username):
            print(f"user {username!r} already exists", file=sys.stderr)
            return 1
        temp_pw = db.add_user(conn, username, is_admin=is_admin)
        uid = db.guise_uid(conn, db.get_user(conn, username)["id"])
    role = "admin" if is_admin else "user"
    print(f"created {role} {username!r} (guise {guises.name(uid)})")
    print(f"temp password: {temp_pw}")
    print("hand this to the user; they must change it on first login")
    return 0


def _guises() -> int:
    """`<uid> <username>` per line — every allocated Guise with a User still behind it.

    The Unix half of `cuttle user add` reads this: allocating a uid is a head.db row needing no privilege
    (here, as `ccc`), while creating the account it names needs root (in the CLI, with sudo). Listing
    rather than returning just the one added lets the same call back-fill every existing User too.
    """
    with db.connect() as conn:
        db.init_schema(conn)
        for r in db.live_guises(conn):
            print(f"{r['uid']} {r['username']}")
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


def _reset(username: str) -> int:
    with db.connect() as conn:
        db.init_schema(conn)
        temp_pw = db.reset_password(conn, username)
    if temp_pw is None:
        print(f"no such user {username!r}", file=sys.stderr)
        return 1
    print(f"reset password for {username!r}")
    print(f"temp password: {temp_pw}")
    print("hand this to the user; they must change it on first login")
    return 0


def _override(username: str, amount: float, note: str | None) -> int:
    """Add or remove Karma from a User's Balance.

    Head-local: it needs no privilege beyond writing head.db, but minting Karma is the most valuable
    thing a stolen Session could do — the Head API exposes **no admin writes at all**, so the capability
    is simply not requestable over the network.
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


def _known_arm(name: str) -> bool:
    from . import slurm
    return any(a["name"] == name for a in slurm.arms())


def _retract(node: str) -> int:
    """`cuttle arm retract <name> --any` — an operator retracting on someone else's behalf, from the
    Head. Writes head.db as the Mantle (retraction.retract needs no further privilege — see its module
    docstring), skipping the owner check the network route enforces."""
    from . import retraction
    if not _known_arm(node):
        print(f"no such Arm {node!r}", file=sys.stderr)
        return 1
    retraction.retract(node)
    print(f"retracted {node!r}")
    return 0


def _return(node: str) -> int:
    from . import retraction
    if not _known_arm(node):
        print(f"no such Arm {node!r}", file=sys.stderr)
        return 1
    retraction.unretract(node)
    print(f"returned {node!r}")
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
    rs = sub.add_parser("reset", help="reset a user's password (random temp password, forced change, "
                        "kills their sessions).")
    rs.add_argument("username")
    e = sub.add_parser("exists", help="exit 0 if the user exists, 1 if not (no output).")
    e.add_argument("username")
    sub.add_parser("guises", help="list allocated Guise uids ('<uid> <username>' per line).")
    o = sub.add_parser("override", help="add/remove Karma from a User's Balance.")
    o.add_argument("username")
    o.add_argument("amount", type=float)
    o.add_argument("--note")
    rt = sub.add_parser("retract", help="retract an Arm on its owner's behalf (--any escape hatch).")
    rt.add_argument("node")
    rn = sub.add_parser("return", help="return an Arm on its owner's behalf (--any escape hatch).")
    rn.add_argument("node")

    args = p.parse_args(argv)
    if args.cmd == "add":
        return _add(args.username, args.admin)
    if args.cmd == "list":
        return _list()
    if args.cmd == "remove":
        return _remove(args.username)
    if args.cmd == "reset":
        return _reset(args.username)
    if args.cmd == "exists":
        return _exists(args.username)
    if args.cmd == "guises":
        return _guises()
    if args.cmd == "override":
        return _override(args.username, args.amount, args.note)
    if args.cmd == "retract":
        return _retract(args.node)
    if args.cmd == "return":
        return _return(args.node)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
