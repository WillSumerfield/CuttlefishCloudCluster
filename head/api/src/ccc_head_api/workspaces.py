"""Workspaces: a User's durable working directories, each holding a **Storage** carved out of a 32GB pool.

A **Workspace** is the Tide Pool's durable form (CONTEXT.md). A Tide Pool keeps nothing — its scratch
dir is gone when the session ends, and only the Recovery Window's day-long copy stands between a User
and losing it. A Workspace keeps everything: the session's end mirrors the tree home, so opening it
again resumes where the last visit stopped. That is why it needs no Recovery Window at all.

**Sibling of `recipes.py`, and shaped like it on purpose** — same `<user_id>/<name>` layout, same
owner-scoped lookups, same "the name is the only handle" rule. What differs is what each is FOR: a
Recipe is frozen and re-run in batch, a Workspace is mutable and visited interactively.

**The Allowance is divided up by its holder, and this is the only one that is.** A User's 32GB does not
sit as one pool that sessions race each other into; each Workspace reserves a **Storage** out of it when
it is created, and what a session runs out of is that one Workspace's Storage. Creating one is refused
when the pool has no room; a visit that fills its Storage is warned in-session and evicts nothing.

**Usage is measured off the disk, never stored.** See the `_SCHEMA_V17` comment in db.py: a Workspace's
tree changes every 60 seconds from inside a live session, over rsyncd, with nothing here watching, so a
cached figure would be stale exactly when it is load-bearing (refusing a shrink below what is held).

**Holding one costs no Karma.** Storage never does; visiting does, because a visit IS a Tide Pool.
"""

import json
import os
import re
import shutil
import tarfile
from pathlib import Path

from . import db
from . import tarstream

# Per-User, and a POOL rather than a wall: the sum of every Storage its owner has reserved. 32GiB, the
# same figure as the Recipe Allowance — the two are independent in both directions (CONTEXT.md,
# **Allowance**).
MAX_USER_BYTES = int(os.environ.get("CCC_WORKSPACE_MAX_USER", 32 * 1024**3))

# A Workspace's name is a directory component, an URL path segment and an rsyncd module suffix, so it
# is checked rather than escaped. Same shape as `recipes.NAME_RE`.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class PoolExceeded(Exception):
    """The 32GB pool has no room for the **Storage** being asked for. 413 at the boundary, carrying both
    numbers — a User divides this Allowance up themselves, so they can only act on it if told what is
    left."""

    def __init__(self, used: int, incoming: int) -> None:
        self.used, self.incoming = used, incoming
        super().__init__(f"{incoming} bytes over a pool with {MAX_USER_BYTES - used} free")


class SeedTooBig(Exception):
    """The directory being seeded in is larger than the **Storage** the new Workspace reserves. 413.

    Distinct from `PoolExceeded` because the remedy is different: this one is fixed by asking for a
    bigger Storage (or seeding less), not by deleting another Workspace."""

    def __init__(self, size: int, storage: int) -> None:
        self.size, self.storage = size, storage
        super().__init__(f"{size} bytes seeded into a {storage}-byte Storage")


class StorageTooSmall(Exception):
    """A resize below what the tree already holds. Refused with the real number rather than clamped:
    silently keeping the old Storage would report a size its owner did not ask for, and truncating the
    tree to fit is the one thing no resize may ever do."""

    def __init__(self, used: int, asked: int) -> None:
        self.used, self.asked = used, asked
        super().__init__(f"{asked} bytes asked for, {used} bytes already held")


class Live(Exception):
    """The operation needs the Workspace idle and a visit is in progress. 409 at the boundary."""

    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        super().__init__(f"spawn {job_id} is visiting")


def _root() -> Path:
    # Lazy import, like `recipes._root`: keeps the whole submit path out of a module that mostly
    # writes files.
    from . import spawns
    return spawns.HEAD_STATE_DIR / "workspaces"


def dir_of(user_id: int, name: str) -> Path:
    """The tree itself. Public because the Siphon serves it and the submit path names it — unlike a
    Recipe's directory, which nothing outside this module ever needs a path to."""
    return _root() / str(user_id) / name


def usage(user_id: int, name: str) -> int:
    """What the tree holds right now, in bytes, off the disk.

    `scandir` rather than `rglob`: this runs once per Workspace on every listing, and the tree it walks
    is the one thing in CCC deliberately allowed to hold a venv. Symlinks are counted as links and never
    followed, so a link into someone else's tree adds its own size and not its target's.

    Slightly SMALLER than the `du -sb` the in-session Storage gate uses, which also counts directory
    inodes. That is the safe direction: the Arm refuses a sync marginally before the Head would say the
    Storage is full, rather than after.
    """
    total = 0
    stack = [str(dir_of(user_id, name))]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue  # raced with a mirror deleting it, or the tree is not there at all
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                else:
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def _needs(meta: dict) -> tuple[int, int, int, str, list[str], list[str], str]:
    """The declaration a visit is submitted from — validated the way `recipes.save` validates a
    Recipe's, minus the two a Workspace does not have.

    No `script`: a visit is a shell, not a run. `manifest` and `outputs` are OPTIONAL, exactly as they
    are for the Tide Pool a visit is (`spawns.submit_checked` exempts `tidepool=True` from the
    at-least-one-output rule) — a Workspace opened to poke at a box has no environment to declare and
    nowhere it wants results sent.
    """
    for field in ("minutes", "vram_mb"):
        if not meta.get(field):
            raise ValueError(f"missing '{field}' — a Workspace carries the same declared need and "
                             f"length any Spawn does, and a visit is submitted from it")
    from .spawns import output_plan
    outputs = meta.get("outputs") or []
    if not isinstance(outputs, list):
        raise ValueError("'outputs' must be a list of strings")
    output_plan(outputs)
    features = meta.get("features") or []
    if not isinstance(features, list) or not all(isinstance(f, str) for f in features):
        raise ValueError("'features' must be a list of strings")
    return (max(1, int(meta.get("gpus", 1))), int(meta["vram_mb"]), int(meta["minutes"]),
            meta.get("manifest") or "", outputs, features, meta.get("substrate") or "")


def _storage(meta: dict) -> int:
    storage = meta.get("storage_bytes")
    if not storage or int(storage) <= 0:
        raise ValueError("missing 'storage_bytes' — a Workspace reserves its Storage out of your Allowance "
                         "when you create it, so how much it may hold is yours to say")
    return int(storage)


def weigh(tar_path: Path, manifest: str, substrate: str = "") -> int:
    """Uncompressed bytes in the seed upload, and the is-it-really-in-there checks for the two files a
    visit is declared from.

    All before anything is written, like `recipes.weigh`. There is no entrypoint to look for, because
    nothing about a Workspace ever runs a script — and each of these is checked only when it was
    named, since both are optional for a shell."""
    with tarfile.open(tar_path, mode="r:gz") as tf:
        members = tf.getmembers()
        names = [m.name for m in members]
        for label, rel in (("manifest", manifest), ("substrate", substrate)):
            if rel and rel not in names:
                raise ValueError(f"{label} {rel!r} was not found in the uploaded directory — check "
                                 f"the path matches exactly (and isn't excluded by .gitignore)")
        return sum(m.size for m in members if m.isfile())


def create(conn, user_id: int, meta: dict, tar_path: Path) -> dict:
    """Seed a directory into a new Workspace. Returns it.

    The **only** upload a Workspace ever takes. Everything after this is written from inside its own
    sessions and mirrored home, which is what makes it durable rather than a Recipe you re-freeze.

    Two separate limits, refused in the order a User can act on them: the pool (delete a Workspace, or
    ask for less) and then the Storage itself (ask for more, or seed less).
    """
    name = meta.get("name") or ""
    if not NAME_RE.match(name):
        raise ValueError(f"invalid workspace name {name!r} — letters/digits/'.'/'_'/'-' only, must "
                         f"start with a letter or digit, 64 characters or fewer")
    if db.get_workspace(conn, user_id, name) is not None:
        raise ValueError(f"you already have a workspace named {name!r} — open it, or delete it first")
    gpus, vram_mb, minutes, manifest, outputs, features, substrate = _needs(meta)
    storage = _storage(meta)

    used = db.user_workspace_storage(conn, user_id)
    if used + storage > MAX_USER_BYTES:
        raise PoolExceeded(used, storage)
    size = weigh(tar_path, manifest, substrate)
    if size > storage:
        raise SeedTooBig(size, storage)

    dest = dir_of(user_id, name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, mode="r:gz") as tf:
        dest.mkdir()
        tf.extractall(dest, filter="data")  # 'data' filter blocks path-traversal entries
    row = db.add_workspace(conn, user_id, name, storage, gpus, vram_mb, minutes, manifest,
                           outputs, features, substrate)
    return to_json(row, size)


def update_meta(conn, user_id: int, name: str, meta: dict) -> dict:
    """Change what a visit asks for, and the **Storage** the tree may fill — never the tree.

    A resize during a live visit lands here fine and takes effect on the NEXT visit: the running
    session's gate is a constant baked into its job script, and reaching into a live Pod to change it
    would mean a session whose limit moved under it mid-write.
    """
    row = db.get_workspace(conn, user_id, name)
    if row is None:
        raise ValueError(f"no workspace named {name!r}")
    gpus, vram_mb, minutes, manifest, outputs, features, substrate = _needs(meta)
    storage = _storage(meta)

    # Weighed on what it GROWS BY: this Workspace's own reservation comes out of the pool first, the
    # same delta arithmetic `recipes.save` does for an update.
    used = db.user_workspace_storage(conn, user_id, excluding=row["id"])
    if used + storage > MAX_USER_BYTES:
        raise PoolExceeded(used, storage)
    held = usage(user_id, name)
    if storage < held:
        raise StorageTooSmall(held, storage)
    # Checked against the tree on disk, like `recipes.update_meta` checks a script — a settings edit
    # must not be able to point a Workspace at a manifest it has not got. Only when one is named:
    # dropping the manifest entirely is a legitimate edit for a shell.
    if manifest and not (dir_of(user_id, name) / manifest).is_file():
        raise ValueError(f"manifest {manifest!r} is not in this workspace — open it and add the file, "
                         f"or name one that is there")
    if substrate and not (dir_of(user_id, name) / substrate).is_file():
        raise ValueError(f"substrate {substrate!r} is not in this workspace — open it and add the "
                         f"file, or name one that is there")

    updated = db.update_workspace_meta(conn, user_id, name, storage, gpus, vram_mb, minutes, manifest,
                                       outputs, features, substrate)
    return to_json(updated, held)


def visit(conn, user_id: int, name: str, is_admin: bool, overrides: dict | None = None,
          dek: bytes | None = None) -> tuple[int, bool]:
    """Open a Workspace: return `(spawn id, attached)`.

    **Submit-or-attach, decided by the claim.** A Workspace holds one visit at a time — two sessions
    `--delete`-mirroring one tree from different Arms is the failure that matters — so a second `open`
    while one is live does not queue a second Spawn, it hands back the one already running and the
    client attaches to that. `attached` is which of the two happened, so a client can say so.

    A visit IS a Tide Pool (`tidepool=True`), priced, queued and Escrowed like any other Spawn. What
    makes it a visit is `workspace=`: the Siphon is rooted at the durable tree instead of a disposable
    upload, so the session both starts from and writes back into it.

    `overrides` are this visit's flags — vram/time/gpus/features/outputs/manifest — defaulting to what
    the Workspace declares. They change THIS session only and are never written back: `update` is how
    the declaration itself moves.

    The claim is taken BEFORE `sbatch` and rebound to the real id after, because sbatch is what mints
    an id and claiming afterwards would let two opens both submit. A submit that raises releases it, so
    a failure leaves the Workspace openable rather than stuck.
    """
    from . import spawns

    row = db.get_workspace(conn, user_id, name)
    if row is None:
        raise ValueError(f"no workspace named {name!r}")
    if row["live_job_id"] is not None and row["live_job_id"] > 0:
        return row["live_job_id"], True
    if not db.claim_workspace(conn, row["id"]):
        # Lost the race by milliseconds. Re-read rather than raise: the winner's session is exactly
        # what this caller wanted, and it is running.
        again = db.get_workspace(conn, user_id, name)
        if again is not None and (again["live_job_id"] or 0) > 0:
            return again["live_job_id"], True
        raise ValueError(f"workspace {name!r} is already being opened — try again in a moment")

    over = overrides or {}
    try:
        job_id = spawns.submit_checked(
            user_id, is_admin, None, max(1, int(over.get("gpus") or row["gpus"])), [], {},
            None, int(over.get("minutes") or row["minutes"]),
            int(over.get("vram_mb") or row["vram_mb"]) // 1024,
            list(over.get("outputs") or json.loads(row["outputs"])),
            tidepool=True,
            manifest=(over.get("manifest") if over.get("manifest") is not None else row["manifest"])
            or None,
            substrate=(over.get("substrate") if over.get("substrate") is not None
                       else row["substrate"]) or None,
            features=list(over.get("features") or json.loads(row["features"])),
            workspace=dir_of(user_id, name), workspace_storage=row["storage_bytes"],
            # Inherited, exactly as a Recipe's is: the Workspace was named by its owner when they
            # created it, so a visit needs no name of its own. Without this a visit was the one Spawn
            # with nothing in its Name at all — it carries no script either, so the archive had nothing
            # whatsoever to title the row with.
            name=name, dek=dek,
        )
    except BaseException:
        db.release_workspace(conn, row["id"], db.SUBMITTING)
        raise
    db.rebind_workspace_claim(conn, row["id"], job_id)
    return job_id, False


def full_marker(job_id: int) -> int:
    """How far over its **Storage** a live visit's last mirror found the tree, or 0 when it is under.

    Written by the session every 60s and sent home through `[out-…]`, because the Head cannot see the
    tree the Arm is holding — only what the mirror tells it. 0 rather than a missing file once a visit
    has started, so recovering from full is visible and not just the absence of a warning.
    """
    from . import spawns
    try:
        return int((spawns.SPAWNS_DIR / str(job_id) / "workspace.full").read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def delete(conn, user_id: int, name: str) -> bool:
    """Delete a Workspace and free its **Storage**. The only thing that ever removes one — a Workspace
    never expires and is never evicted.

    Refused while a visit is live, and it is the refusal that matters most in this module: the tree is
    what the live session is mirroring INTO, so deleting it out from under one would race a writer that
    recreates parts of it seconds later and leave a directory no row points at.
    """
    row = db.get_workspace(conn, user_id, name)
    if row is None:
        return False
    if row["live_job_id"] is not None:
        raise Live(row["live_job_id"])
    shutil.rmtree(dir_of(user_id, name), ignore_errors=True)
    db.delete_workspace(conn, user_id, name)
    return True


def reconcile(conn, live_job_ids: list[int]) -> int:
    """Clear the claim of every Workspace whose visit has left the queue. Returns how many.

    Called by the karma tick, the same pass that reaps dead Tide Pools. A visit ends in ways nothing on
    the Head observes — `--time` expiry, a crashed Arm, a Head restart mid-session — and a claim nobody
    cleared is a Workspace its owner can never open again.
    """
    stale = db.stale_workspace_claims(conn, live_job_ids)
    for row in stale:
        db.release_workspace(conn, row["id"], row["live_job_id"])
    return len(stale)


def sweep_orphans(conn) -> int:
    """Delete the trees of Users who no longer exist. Returns how many.

    Exactly `recipes.sweep_orphans`, for the same reason: the rows CASCADE with the User and nothing in
    SQLite can reach the files, and a Workspace has no TTL that would eventually collect them.
    """
    live = db.recipe_owner_ids(conn)
    swept = 0
    try:
        entries = sorted(_root().iterdir())
    except OSError:
        return 0  # nobody has ever made one here
    for entry in entries:
        if entry.name.isdigit() and int(entry.name) not in live:
            shutil.rmtree(entry, ignore_errors=True)
            swept += 1
    return swept


def resolve(user_id: int, name: str, subpath: str | None) -> Path:
    """A path inside one Workspace's tree, or ValueError. The tree itself when `subpath` is empty.

    Resolved and then checked to be inside, rather than pattern-matched: `..`, an absolute path and a
    symlink pointing out of the tree all reach the same place and only the resolved answer catches all
    three. A Workspace is the one tree in CCC its owner can put arbitrary symlinks in, so this is the
    check that keeps a download of one from serving the Head's own disk.
    """
    root = dir_of(user_id, name).resolve()
    if not subpath:
        return root
    target = (root / subpath).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"{subpath!r} is not inside this workspace")
    if not target.exists():
        raise ValueError(f"{subpath!r} is not in this workspace")
    return target


def tar_stream(target: Path, arcbase: str):
    """A gzip-tar of `target`, yielded in chunks — `tarstream.chunks`, which this used to be.

    Kept as a name here because a Workspace download is what first needed it: a Storage runs to 16GB
    and up, so building one in memory would have been fatal to the Head rather than merely slow. That
    reasoning turned out to apply to a Spawn's results too, hence the shared module.
    """
    return tarstream.chunks(target, arcbase)


def to_json(row, used_bytes: int) -> dict:
    """The wire shape both clients read.

    `used_bytes` is handed in rather than measured here: a listing walks every tree once already, and
    doing it again per row would be a second disk walk for each card.

    `live` is the claimed Spawn id or None — the id `open` would attach to, said as a number because
    "live" alone leaves a User with a session they cannot find and cannot cancel. A claim still in
    `db.SUBMITTING` has no id to give yet, so it reads as `live: null, starting: true` rather than
    handing a client a negative number to try to attach to.
    """
    from datetime import datetime, timezone

    def _ms(value):
        if not value:
            return None
        stamp = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return int(stamp.timestamp() * 1000)

    return {
        "name": row["name"],
        "storage_bytes": row["storage_bytes"],
        "used_bytes": used_bytes,
        "gpus": max(1, row["gpus"]),
        "vram_mb": row["vram_mb"],
        "minutes": row["minutes"],
        "manifest": row["manifest"],
        "substrate": row["substrate"],
        "outputs": json.loads(row["outputs"]),
        "features": json.loads(row["features"]),
        "created_at": _ms(row["created_at"]),
        "last_visited_at": _ms(row["last_visited_at"]),
        "live": row["live_job_id"] if (row["live_job_id"] or 0) > 0 else None,
        "starting": row["live_job_id"] == db.SUBMITTING,
    }
