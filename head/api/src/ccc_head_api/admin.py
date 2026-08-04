"""Local admin for the Head: users, Karma Overrides, pinned Spawns, and Arm Retract/Return on someone
else's behalf (Phase 3a slice 1; widened in Phase 6 and Phase 2's retract/return `--any`).

Runs *on the Head as the `ccc` service account* (`python -m ccc_head_api.admin ...`), writing head.db
directly — being the operator on the Head is the authorization, no HTTP/session (mirrors the way
`cuttle arm authorize` mutates the registry). `cuttle user add|list|remove|reset` are thin wrappers that
shell into this via the API venv; `setup --head` uses `add --admin` to seed the first admin.

Prints the one-time temp password to stdout — the operator hands it to the user, who must rotate it on
first login. Nothing here ever prints or stores a reusable password.
"""

import argparse
import json
import sys
from pathlib import Path

from . import db, guises


def _add(username: str, is_admin: bool) -> int:
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

    The Unix half of `cuttle user add` reads this: allocating a uid is a row in head.db and needs no
    privilege (so it happens here, as `ccc`), while creating the account it names needs root (so it
    happens in the CLI, with the operator's sudo — cuttle.auth.ensure_guise_accounts). Listing rather
    than returning the one just added is deliberate: the same call provisions a brand-new User and
    back-fills every User who predates Guises, so an upgrade is just a `setup --head` re-run.
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


def _submit(username: str, script: str, tar: str, minutes: int, vram: int, gpu: int,
            node: str | None, wandb_env: dict[str, str], args: list[str], outputs: list[str],
            features: list[str] | None = None, tidepool: bool = False,
            manifest: str | None = None, activation: bool = False) -> int:
    """Submit a Spawn pinned to a named Arm, attributed to `username`.

    Head-local since Phase 6 (`cuttle run --node`). Authorization is being the operator on the Head;
    attribution is the account named here. Goes through `spawns.submit_checked`, so the Escrow, the
    Attribution row and the W&B environment are all produced exactly as they are for a Spawn that
    arrived over HTTP.

    `wandb_env` is the caller's project/entity hint and only that — never a key, which comes from
    whichever User this is attributed to (spawns.compose_wandb_env). It was hardcoded `{}` here, so
    `cuttle run --node --project X` quietly logged into `ccc` instead of X.

    `tidepool` (docs/TODO.md) lets an Admin pin a Tide Pool the same way they pin any other Spawn —
    orthogonal to node-pinning, and threaded through for completeness rather than because it is the
    common path (the common path is unpinned, over `/submit`).

    `activation` is CCC activating a Feature's licence on an Arm being admitted (cuttle.enroll). It
    exists on THIS path and not on `/submit`, and that is the whole point: it binds the Arm owner's
    licence key into the Pod, so the only thing allowed to ask for it is an operator standing on the
    Head.
    """
    from . import spawns
    with db.connect() as conn:
        db.init_schema(conn)
        user = db.get_user(conn, username)
        if user is None:
            print(f"no such user {username!r}", file=sys.stderr)
            return 1
    try:
        job_id = spawns.submit_checked(user["id"], bool(user["is_admin"]), script or None, gpu, args,
                                       wandb_env, Path(tar), minutes, vram, outputs, node,
                                       features=features, tidepool=tidepool, manifest=manifest,
                                       activation=activation,
                                       # This process exits the moment it has printed the id, and the
                                       # Environment build runs in a DAEMON thread — so backgrounding
                                       # it here would kill the build at exit and strand a Spawn that
                                       # `sbatch --hold` has already made unrunnable without it. The
                                       # API service has a future to build in; a CLI does not.
                                       background_build=False)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    # The Environment echo goes to STDERR, deliberately. stdout is a contract: the caller
    # (cuttle.spawns._submit_pinned) reads the Spawn id off its last line, so a second line there
    # would be parsed as the id. stderr is already where this tool's prose goes.
    note = spawns.environment_note(job_id)
    if note:
        print(note, file=sys.stderr)
    print(job_id)
    return 0


def _feature_activation(name: str) -> int:
    """Print the project CCC submits to activate `name`'s licence on an Arm, as JSON `{file: body}`.

    Head-local rather than an HTTP route, and that is the authorization: `cuttle arm admit` runs on the
    Head, as the operator, and `cuttle setup --head` runs it before any account has ever logged in.
    Generated here rather than in the CLI so the library list an activation builds against is the same
    one `cuttle feature show` hands a submitter — two copies would drift, and the symptom would be a
    donated box activated against a library set no Spawn actually runs.

    Exit 1 for a Feature that needs no activation, which is how the caller knows to skip the step.
    """
    from . import features as features_mod
    try:
        print(json.dumps(features_mod.activation_project(name)))
    except KeyError:
        print(f"{name!r} needs no activation", file=sys.stderr)
        return 1
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
    s = sub.add_parser("submit", help="submit a Spawn pinned to an Arm (prints the Spawn id).")
    fa = sub.add_parser("feature-activation",
                        help="the project that activates a Feature's licence on an Arm (JSON).")
    fa.add_argument("name")
    s.add_argument("--user", required=True, help="the account to attribute the Spawn to.")
    s.add_argument("--script", default=None, help="omit only for --tidepool.")
    s.add_argument("--tar", required=True, help="gzip-tar of the project tree (empty for --tidepool).")
    s.add_argument("--minutes", type=int, required=True)
    s.add_argument("--vram", type=int, required=True)
    s.add_argument("--gpu", type=int, default=1)
    s.add_argument("--node")
    s.add_argument("--output", action="append", default=[], dest="outputs",
                   help="declared output subdir name (repeatable; required unless --tidepool).")
    s.add_argument("--feature", action="append", default=[], dest="features",
                   help="required Arm Feature (repeatable; optional — default none).")
    s.add_argument("--activation", action="store_true",
                   help="CCC activating a Feature's licence on an Arm being admitted. Binds that "
                        "Arm's licence key into the Pod — Head-local only, never over HTTP.")
    s.add_argument("--tidepool", action="store_true",
                   help="a live shell instead of a script (docs/TODO.md, Tide Pool).")
    s.add_argument("--manifest", help="dependency file to build this Spawn's environment from, "
                                      "relative to the project root, or 'none' to build none "
                                      "(default: auto-detect in the project root).")
    # The client's project/entity hint, as the JSON dict submit_checked takes — not two --project/
    # --entity flags, so the CLI and the HTTP route hand over the *same* value and there is one place
    # that decides what a W&B run is called (cuttle.settings.wandb_env).
    s.add_argument("--wandb-env", default="{}", help="JSON WANDB_PROJECT/WANDB_ENTITY hint.")
    s.add_argument("args", nargs="*", help="extra arguments for the script.")

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
    if args.cmd == "submit":
        return _submit(args.user, args.script, args.tar, args.minutes, args.vram, args.gpu,
                       args.node, json.loads(args.wandb_env), args.args, args.outputs, args.features,
                       tidepool=args.tidepool, manifest=args.manifest,
                       activation=args.activation)
    if args.cmd == "feature-activation":
        return _feature_activation(args.name)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
