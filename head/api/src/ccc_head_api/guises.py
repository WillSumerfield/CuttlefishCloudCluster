"""**Guises** — who a Spawn is on the machine that runs it.

Every Spawn runs as a Guise, not the **Mantle** (`ccc`, which owns head.db, head.key, every User's
upload, every Spawn's output) — Apptainer's "user inside is user outside" means containment flags alone
can't keep a Spawn from touching those. One Guise per User, the same one wherever the cluster places
their work.

A Guise is `ccc-u<uid>` and nothing else, because of where the name has to be formed: the Prolog creates
the account on whatever Arm Slurm picked, as root, with `SLURM_JOB_UID` as its ONLY input — no network,
no head.db, no lookup of any kind. See config.guise_name (the CLI's copy of this) and
templates.PROLOG_TMPL.

Constants are duplicated from cuttle.config rather than imported: this package never imports the CLI
(same split as FEATURE_NAMES between enroll.py and slurm.py). They must agree.
"""

import os

PREFIX = "ccc-u"
CLUSTER_UID = 60001   # the cluster's own Guise: the enrollment benchmark, submitted by no User
UID_BASE = 60002      # Users allocate from here
# The Mantle. A job Slurm reports as owned by `ccc` — the enrollment benchmark, or one with no
# Attribution row — runs plain and unprivileged. Nothing new is ever submitted as this.
MANTLE = os.environ.get("CCC_MANTLE_USER", "ccc")


def name(uid: int) -> str:
    """The Unix account a Spawn owned by this uid runs as. The ONLY place the name is formed."""
    return f"{PREFIX}{uid}"


def as_owner(argv: list[str], owner: str | None) -> list[str]:
    """`argv`, wrapped so it runs as the Spawn's own owner.

    Every Slurm operation CCC performs on a Spawn — re-nicing it for Karma, cancelling it, signalling
    it on a Retract — is one the job's OWNER may perform with no privilege at all; the Head asks sudo to
    become them. The grant is `(%ccc-guises)`, never root (templates._SUDOERS_CCC_SLURM).

    `owner` comes from squeue (`user_name`), not the Attribution row, because it's what sudo has to
    MATCH. It also naturally covers the enrollment benchmark and any job with no Attribution row: both
    come back as the Mantle and run plain, below.

    `-n`: never prompt. This fires inside a request handler and inside the Karma tick, neither of which
    has a tty — a sudo that asks for a password there would hang, not fail.
    """
    if not owner or owner == MANTLE:
        return list(argv)
    return ["sudo", "-n", "-u", owner, *argv]
