"""**Guises** — who a Spawn is on the machine that runs it (temp/spawn-isolation.md Phase 3).

Before this, every Spawn on every node ran as the **Mantle** (`ccc`), the account that owns head.db,
head.key, every User's upload and every other Spawn's output. Apptainer's own model is "the user
inside is the user outside", so containment flags could only ever lock the door; a Guise moves the
valuables out of the room. One per User, the same one wherever the cluster places their work.

A Guise is `ccc-u<uid>` and nothing else, because of where the name has to be formed: the Prolog
creates the account on whatever Arm Slurm picked, as root, with `SLURM_JOB_UID` as its ONLY input —
no network, no head.db, no lookup of any kind. See config.guise_name (the CLI's copy of this) and
templates.PROLOG_TMPL.

Constants are duplicated from cuttle.config rather than imported: this package never imports the CLI
(same split as FEATURES_CATALOG between enroll.py and slurm.py). They must agree.
"""

import os

PREFIX = "ccc-u"
CLUSTER_UID = 60001   # the cluster's own Guise: the enrollment benchmark, submitted by no User
UID_BASE = 60002      # Users allocate from here
# The Mantle. Present here only so the transitional path can RECOGNISE it: a job Slurm says is owned
# by `ccc` is one queued before this phase deployed (or the pre-3g benchmark), and it is run against
# plain, un-sudo'd commands exactly as it always was. Nothing new is ever submitted as this.
MANTLE = os.environ.get("CCC_MANTLE_USER", "ccc")


def name(uid: int) -> str:
    """The Unix account a Spawn owned by this uid runs as. The ONLY place the name is formed."""
    return f"{PREFIX}{uid}"


def as_owner(argv: list[str], owner: str | None) -> list[str]:
    """`argv`, wrapped so it runs as the Spawn's own owner.

    Every Slurm operation CCC performs on a Spawn — re-nicing it for Karma, cancelling it, signalling
    it on a Retract — is one the job's OWNER may perform with no privilege at all. That was free while
    every Spawn was the Mantle and the Head *was* the owner; now the Head has to ask sudo to become
    them. The grant is `(%ccc-guises)`, never root (templates._SUDOERS_CCC_SLURM).

    `owner` comes from squeue (`user_name`), not from the Attribution row, because it is what sudo has
    to MATCH — so squeue is ground truth by construction. It also gets three cases right for free that
    the ledger gets wrong: a Spawn queued before this phase deployed, the enrollment benchmark, and any
    job with no Attribution row at all. All three come back as the Mantle and run plain, below.

    `-n`: never prompt. This fires inside a request handler and inside the Karma tick, neither of which
    has a tty — a sudo that asks for a password there would hang, not fail.
    """
    if not owner or owner == MANTLE:
        return list(argv)
    return ["sudo", "-n", "-u", owner, *argv]
