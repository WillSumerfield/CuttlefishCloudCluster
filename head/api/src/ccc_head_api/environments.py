"""Per-Spawn Python **Environment**s (docs/TODO.md "Python Environments") — declared by the User,
built by the Head, kept by nobody.

Until this, every Spawn ran against one image: `apptainer exec ... base.sif python <script>`, pinned
`pytorch/pytorch` + `wandb` and nothing else. A User needing one extra package had no supported way to
get it. Now a project declares its dependencies in the file its own tooling already writes, and the
Head builds that before the Spawn is placed.

**Built on the Head, before placement, and that is an economy decision.** Building inside the
allocation would make a User pay Karma to hold a GPU while pip downloads — Settlement bills the seconds
a Device is *held*, not the seconds it is busy. Building here costs no Karma and no Device, and needs no
per-Arm cache to start cold on every donated box.

**Built while the Spawn queues, not while the User waits.** `spawns.submit` `sbatch --hold`s the Spawn
first, so it has its real id, Attribution and Escrow immediately, and runs the build behind the request.
The hold is a dispatch interlock and nothing more: Slurm cannot place the Spawn before the env exists,
but the build overlaps the queue wait instead of being added to it. A Spawn that would have waited 40
minutes for a card spends the first 3 of them building, not 43.

**In a Pod, as the submitter's Guise.** A manifest is code: build backends, sdist `setup.py`, conda
post-link scripts all execute. The Head is the one machine holding the Mantle's key and every User's
work, so the build gets the same walls a Spawn gets — `base.sif`, entered as that User's Guise — rather
than a second, weaker kind of isolation invented for it. `render_build` returns argv the caller runs
through `guises.as_owner`; nothing here runs as the Mantle.

**One fixed path, so nothing needs relocating.** A venv bakes its own prefix into `pyvenv.cfg` and every
console-script shebang; a conda prefix bakes it into hundreds of files and is famously not relocatable.
Both are built AND run at `ENV_MOUNT`, bound there on the Head at build time and on the Arm at run time.
The directory underneath differs on the two machines; the path inside the Pod never does. That is
strictly cheaper than `uv venv --relocatable` plus conda-pack-style prefix rewriting — two fixup
mechanisms and one well-known source of subtle breakage.

**uv layers, conda replaces.** The uv paths build a venv with `--system-site-packages`, so the image's
torch/cuDNN/wandb are inherited and only the *delta* is built — the usual env is megabytes. A conda
env is its own prefix with its own interpreter and inherits nothing, so `wandb` is injected into it
explicitly: without that, the Head's promise that "a Spawn logs to its submitter's own account"
(CONTEXT.md) quietly stops holding for exactly the Users who declared the most. A manifest that names a
package the image already ships wins, silently — it is what the User declared.

**Nothing is cached except downloads.** The built env is destroyed with the Spawn's staging, like the
uploaded project (`projects.py`). That keeps the Head from growing a fourth permanent consumer of disk
beside projects, outputs and Recipes, and keeps **Allowance** meaning what CONTEXT.md says it means — a
ceiling on things a User would *lose*, which a rebuildable env is not. What persists is the package
*download* cache (`CACHE_DIR`): Mantle-owned, size-capped, never staged, and the only reason "rebuild
every time" is affordable — a 50-Spawn sweep costs 50 local relinks, not 50 re-downloads.

**Detection is a guess, so it is never insisted on.** `detect` reads the project root; `manifest=NONE`
declines a build outright and an explicit name overrides the guess. A `pyproject.toml` in a vendored
repo is not an instruction to the cluster.
"""

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import guises

# Where the env is built and run, INSIDE the Pod, on every machine. Absolute paths baked into the env
# resolve against this and only this — see the module docstring.
ENV_MOUNT = "/opt/ccc/env"

# The env's home inside the uploaded project, so it rides the transfer that already exists: `cp -a` on
# the Head's co-located Arm, the Siphon's `[proj-…]` module on a remote one. The JOB_SCRIPT moves it
# OUT of the project before running — it must not land in the Recovery Window copy, and an 8GB env
# counted against RECOVERY_MAX_BYTES would disable recovery for every conda Spawn.
ENV_DIRNAME = ".ccc-env"

# uv and micromamba are single static binaries, installed on the HEAD ONLY and bound into the build
# Pod. Deliberately not baked into BASE_DEF_TMPL: `base.sif` is built per node and skipped when it
# already exists (cuttle.setup), so baking them in would grow every Arm's image for work only the Head
# does and leave every already-enrolled node without them until a teardown/setup.
TOOLS_DIR = Path(os.environ.get("CCC_TOOLS_DIR", "/opt/ccc/tools"))
UV_BIN = TOOLS_DIR / "uv"
MICROMAMBA_BIN = TOOLS_DIR / "micromamba"

# The package download cache — NOT an env cache. Mantle-owned, never staged to an Arm, never per-User.
CACHE_DIR = Path(os.environ.get("CCC_ENV_CACHE", "/var/lib/ccc/head/envcache"))
CACHE_MAX_BYTES = int(os.environ.get("CCC_ENV_CACHE_MAX", 20 * 1024**3))

# Scratch for the build tools, on DISK. `--contain` gives the Pod a /tmp carved out of Apptainer's
# session directory, which is a RAM tmpfs capped by `sessiondir max size` in apptainer.conf (64 MB on
# this fleet). uv is already pointed at the cache below, but pip — which is what an environment.yml's
# `pip:` section runs — writes its downloads to TMPDIR, so any wheel bigger than what is left of that
# 64 MB dies. Observed 2026-08-03: nvidia-cudnn-cu13 is 366 MB, and pip reports the resulting ENOSPC
# as "Download failed ... This is an issue with network connectivity, not pip", which sends anyone
# reading it after the network instead of the disk.
#
# A sibling of CACHE_DIR rather than a directory inside it, because prune_cache below empties the
# WHOLE cache when it is over the cap — with the scratch in there, one build finishing could delete
# another's half-downloaded wheel out from under it. Nothing here is cache: it is per-build and
# deleted when that build ends.
TMP_DIR = Path(os.environ.get("CCC_ENV_TMP", "/var/lib/ccc/head/envtmp"))

# Clears a hung solver without killing a legitimately slow one: uv resolves in seconds, a real conda
# solve can take minutes.
BUILD_TIMEOUT_SECONDS = int(os.environ.get("CCC_ENV_BUILD_TIMEOUT", 15 * 60))

# A built env is not uploaded, so it escapes MAX_UPLOAD_BYTES entirely — yet it is still rsync'd onto a
# donor's disk over the Siphon, on every Spawn, because nothing is cached. 8GB admits a real torch conda
# env (the whole point of supporting environment.yml) and matches retention.py's per-User Allowance.
MAX_ENV_BYTES = int(os.environ.get("CCC_ENV_MAX", 8 * 1024**3))

# What the payload runs. No env built → the image's own interpreter, exactly as before this module.
BASE_INTERPRETER = "python"
ENV_INTERPRETER = f"{ENV_MOUNT}/bin/python"

# wandb, injected into a conda env that replaces the image's Python. Kept in step with
# cuttle.config.WANDB_PKG by hand: this package never imports the CLI (see spawns.py).
WANDB_PKG = "wandb==0.28.1"

# What a User writes to decline a build outright.
NONE = "none"


class BuildError(ValueError):
    """The declared environment could not be built.

    Raised at two quite different moments, and the difference is the whole reason this class carries a
    log. From `detect` it is a *rejection* — a manifest that does not exist or cannot be classified is
    bad input, decided in microseconds, and `main.submit` turns it into a 4xx before any Spawn exists.
    From `build` it is a failed **Spawn**: by then the Spawn has been accepted, Escrowed and held, so
    the failure is recorded as its `err` and a non-zero `exit`, and Settlement bills it nothing (it
    never held a Device). `.log` is what the User reads to find out why.
    """

    def __init__(self, message: str, log: str = "") -> None:
        super().__init__(message)
        self.log = log


@dataclass(frozen=True)
class Manifest:
    """One resolved declaration: which file, and which of the three build shapes it takes."""
    kind: str          # "uv-project" | "uv-requirements" | "conda"
    path: str          # project-relative, as the User would name it

    @property
    def summary(self) -> str:
        """The one line both clients echo at submit (docs/TODO.md) — see `describe` for the no-env case."""
        if self.kind == "conda":
            return f"{self.path} (conda — replaces the cluster base)"
        return f"{self.path} (on top of the cluster base)"


# Auto-detection order, most specific first. A lock file is preferred because it is the only one of
# these that makes a Recipe's "frozen" promise true of its Environment too (CONTEXT.md, **Recipe**).
# `pyproject.toml` alone still takes the uv path — uv locks it itself.
_AUTO = (
    ("uv.lock", "uv-project"),
    ("pyproject.toml", "uv-project"),
    ("requirements.txt", "uv-requirements"),
    ("environment.yml", "conda"),
    ("environment.yaml", "conda"),
)

# An explicitly named manifest is classified by its basename, so a User may keep several and point at
# one (`requirements-gpu.txt`, `envs/cuda12.yml`) without CCC having to parse it to find out what it is.
_SUFFIX_KINDS = (
    (("environment.yml", "environment.yaml"), "conda"),
    ((".yml", ".yaml"), "conda"),
    (("uv.lock", "pyproject.toml"), "uv-project"),
    ((".txt",), "uv-requirements"),
)


def detect(project: Path, manifest: str | None = None) -> Manifest | None:
    """Which declaration this project makes, if any. Pure — reads the tree, decides nothing else.

    `manifest=None` auto-detects at the project ROOT only (never a recursive walk: a `pyproject.toml`
    three directories down belongs to something the User is vendoring, not to this Spawn). `NONE`
    declines. Anything else is taken as the User's own word and must exist.
    """
    if manifest is not None and manifest.strip().lower() == NONE:
        return None
    if manifest:
        rel = manifest.strip()
        # The same containment rule the declared-output names get (spawns.OUTPUT_NAME_RE): a manifest
        # path is joined onto a directory the Head owns, so `../../etc` must not be a way out of it.
        # Resolved against the project and required to stay inside it.
        target = (project / rel).resolve()
        if not str(target).startswith(str(project.resolve()) + os.sep) or not target.is_file():
            raise BuildError(f"no such manifest in this project: {rel}")
        return Manifest(kind=_classify(rel), path=rel)
    for name, kind in _AUTO:
        if (project / name).is_file():
            return Manifest(kind=kind, path=name)
    return None


def _classify(rel: str) -> str:
    base = Path(rel).name.lower()
    for names, kind in _SUFFIX_KINDS:
        if base in names or any(base.endswith(n) for n in names if n.startswith(".")):
            return kind
    raise BuildError(
        f"cannot tell what kind of manifest {rel} is — name it *.txt (pip), "
        f"*.yml/*.yaml (conda), or pyproject.toml/uv.lock (uv)"
    )


def missing_tool(m: Manifest) -> str | None:
    """The build binary `m` needs and this Head has not got, or None.

    A Head set up before the Environment feature existed has an empty TOOLS_DIR, and every project
    with a `pyproject.toml` — which is most of them — would otherwise have its build fail and take the
    Spawn with it. A Head that cannot build must not turn a submission that used to work into a
    failure: it runs on the cluster base, exactly as it did before, and `describe` says so out loud.
    Re-running `cuttle setup --head` installs the tools (cuttle.setup.install_env_tools).
    """
    need = MICROMAMBA_BIN if m.kind == "conda" else UV_BIN
    return None if need.exists() else str(need)


def describe(m: Manifest | None, missing: str | None = None) -> str:
    """The submit-time echo. Said BEFORE the Spawn is accepted, because the worst case — a manifest one
    directory too deep — is not a build failure at all, so nothing else would ever mention it and the
    User discovers it as an ImportError after paying for a GPU."""
    if missing:
        return (f"environment: cluster base — {m.summary if m else 'a manifest'} was found, but this "
                f"Head has no {Path(missing).name} to build it with (re-run `cuttle setup --head`)")
    return f"environment: {m.summary}" if m else "environment: cluster base (no dependency file found)"


def render_build(m: Manifest) -> list[list[str]]:
    """The commands that build `m` at ENV_MOUNT, in order. Pure — runs nothing, so it is testable.

    Each is argv as run INSIDE the build Pod, with the project as cwd and ENV_MOUNT bound writable.
    """
    uv = str(UV_BIN)
    if m.kind == "conda":
        # Its own prefix, its own interpreter, inheriting nothing from the image — so wandb is injected
        # explicitly or a User who declared a conda env silently stops logging (see module docstring).
        return [
            [str(MICROMAMBA_BIN), "create", "--yes", "--prefix", ENV_MOUNT, "--file", m.path],
            [ENV_INTERPRETER, "-m", "pip", "install", "--no-cache-dir", WANDB_PKG],
        ]
    # --system-site-packages is what makes this the DELTA and not a second copy of torch: the image's
    # 2.5GB of torch/cuDNN is inherited, and only what the User added is built.
    venv = [uv, "venv", "--system-site-packages", ENV_MOUNT]
    if m.kind == "uv-requirements":
        return [venv, [uv, "pip", "install", "--python", ENV_INTERPRETER, "-r", m.path]]
    # Plain `uv sync` — NOT --no-install-project. uv already treats a project with no [build-system]
    # (or `package = false`, as this repo's own root pyproject does) as *virtual* and installs only its
    # dependencies, while a real src-layout package gets installed, which is the only way its imports
    # resolve. Suppressing that would break the packages and help nobody.
    # NOT --active: that only means "use $VIRTUAL_ENV", and nothing in a build Pod sets it, so uv fell
    # back to its default `.venv` beside the manifest — inside the Mantle-owned project the Guise
    # cannot write — and every pyproject.toml/uv.lock Spawn died on "Permission denied" (hardware,
    # 2026-08-02, Spawns 14/15). UV_PROJECT_ENVIRONMENT (set in `build`'s --env) names the path
    # outright, which is the same thing the `uv pip` branch does with --python. `uv sync` still writes
    # `uv.lock` into the project itself, unavoidably — see spawns._lend_project for why the Guise may.
    # --inexact leaves the inherited system packages alone instead of pruning to the lock.
    return [venv, [uv, "sync", "--inexact", "--no-dev"]]


def build(project: Path, m: Manifest, guise: str) -> str:
    """Build `m` into `project/ENV_DIRNAME`, as `guise`, inside a Pod. Returns the build log. Raises
    BuildError, whose text is what the Spawn shows as its `err`, with the log so far as `.log`.

    The env dir is created HERE rather than inside the Pod so its ownership is the Guise's from the
    start — apptainer binds it, it does not create it, and a root-owned mountpoint would leave the build
    unable to write into its own prefix.
    """
    env_dir = project / ENV_DIRNAME
    if env_dir.exists():
        shutil.rmtree(env_dir, ignore_errors=True)
    env_dir.mkdir(parents=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(TMP_DIR, 0o777)
    # Per-build, so two Spawns building at once cannot collide in each other's scratch, and so the
    # `finally` below can delete this one's leftovers without touching a build still running.
    build_tmp = Path(tempfile.mkdtemp(dir=TMP_DIR))
    # 0777 on every directory the Guise must write: the Mantle owns them, and a Guise is a separate
    # uid in a shared group. Deliberately not a chown — the Mantle cannot give away what it owns without
    # privilege it does not have, and this whole module is built on the Head never needing root.
    for d in (env_dir, CACHE_DIR, build_tmp):
        os.chmod(d, 0o777)

    try:
        return _run_build(project, m, guise, env_dir, build_tmp)
    finally:
        # Always: a timed-out or failed build leaves partial downloads behind, and nothing else ever
        # looks in here again. prune_cache does not cover this tree by design (see TMP_DIR).
        shutil.rmtree(build_tmp, ignore_errors=True)


def _run_build(project: Path, m: Manifest, guise: str, env_dir: Path, build_tmp: Path) -> str:
    """The build itself. Split out of `build` only so the scratch it is given is cleaned up whatever
    happens to it — every exit from here is inside that caller's `finally`."""
    from .spawns import CCC_SIF  # local: spawns imports this module, and the constant lives there

    log = [f"$ ccc build {m.summary}"]
    for argv in render_build(m):
        # --contain: the build gets the same walls the payload gets, and nothing of the Head's
        # filesystem beyond the five paths named here. TOOLS_DIR is bound read-only because uv and
        # micromamba are the one thing in this command that lives on the HEAD rather than in the image
        # — without the bind, `--contain` means the binary named in argv does not exist inside the Pod.
        # No --nv: a build needs no GPU, and asking for one on a Head whose cards are all busy would
        # fail for no reason.
        cmd = guises.as_owner([
            "apptainer", "exec", "--contain", "--pid", "--pwd", str(project),
            "--bind", f"{project},{env_dir}:{ENV_MOUNT},{CACHE_DIR}:/ccc-cache,"
                      f"{build_tmp}:/ccc-tmp,{TOOLS_DIR}:{TOOLS_DIR}:ro",
            # UV_PROJECT_ENVIRONMENT is what points `uv sync` at ENV_MOUNT instead of the `.venv` it
            # would otherwise create beside the manifest, in a project directory the Guise cannot write
            # (see render_build). Harmless on the other two paths, which never consult it.
            #
            # TMPDIR moves every tool's scratch off the Pod's 64 MB RAM /tmp and onto disk (see
            # TMP_DIR) — set for all three build paths, not just pip's: uv stages large artifacts
            # through TMPDIR too, and the failure mode is identical.
            # PIP_CACHE_DIR is the download cache, so it belongs with the OTHER caches rather than in
            # the scratch that is deleted after every build — without it pip re-fetches every wheel on
            # every rebuild, and its HTTP cache would land back on the tmpfs this is fixing.
            "--env", f"UV_CACHE_DIR=/ccc-cache/uv,UV_PROJECT_ENVIRONMENT={ENV_MOUNT},"
                     f"MAMBA_ROOT_PREFIX=/ccc-cache/mamba,TMPDIR=/ccc-tmp,"
                     f"PIP_CACHE_DIR=/ccc-cache/pip,LC_ALL=C.UTF-8,LANG=C.UTF-8",
            str(CCC_SIF), *argv,
        ], guise)
        log.append(f"$ {' '.join(argv)}")
        try:
            # cwd="/" — a build has no relationship to the directory its caller happened to be
            # standing in, and apptainer's starter chdirs to the inherited cwd on the HOST before
            # `--pwd` ever applies (which is inside). Run as the Guise, that chdir fails on anything
            # the Guise cannot traverse, and "container creation failed: can't change directory to
            # /home/<operator>/..." is what a Spawn's build reports instead of anything about itself.
            # Never bit the API service, whose cwd is systemd's; bites every Head-local caller, which
            # is now every pinned Spawn's build (hardware, 2026-08-04, `setup --head` run from a
            # project directory).
            r = subprocess.run(cmd, cwd="/", capture_output=True, text=True,
                               timeout=BUILD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            shutil.rmtree(env_dir, ignore_errors=True)
            raise BuildError(
                f"environment build timed out after {BUILD_TIMEOUT_SECONDS // 60} minutes "
                f"({m.path}) — a dependency solve that cannot finish is a Spawn that can never start",
                log="\n".join(log),
            )
        # Both streams, in the log, on success too: `uv sync` reports what it resolved on stderr, and
        # that is the answer to "why did my Spawn get that version" long after the build succeeded.
        log.append((r.stdout + r.stderr).strip())
        if r.returncode != 0:
            shutil.rmtree(env_dir, ignore_errors=True)
            raise BuildError(f"environment build failed ({m.path}):\n{r.stderr.strip()}",
                             log="\n".join(log))

    size = _tree_bytes(env_dir)
    if size > MAX_ENV_BYTES:
        shutil.rmtree(env_dir, ignore_errors=True)
        raise BuildError(
            f"built environment is {size // 1024**3}GB, over the {MAX_ENV_BYTES // 1024**3}GB ceiling "
            f"({m.path}) — nothing is cached, so this would be copied to the Arm on every Spawn",
            log="\n".join(log),
        )
    return "\n".join(log)


def _tree_bytes(root: Path) -> int:
    """Apparent size of the built env. Follows no symlinks — a --system-site-packages venv links back
    into the image, and counting the image's own torch against the User's ceiling would reject every
    env that inherited anything."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for f in filenames:
            p = Path(dirpath) / f
            if not p.is_symlink():
                total += p.stat().st_size
    return total


def prune_cache() -> None:
    """Keep the download cache under CACHE_MAX_BYTES. Whole-cache, not LRU-by-entry: uv and micromamba
    both hard-link out of their caches, so per-entry eviction would need their own bookkeeping to be
    safe. Emptying it costs re-downloads on the next build and nothing else — it holds no state any
    Spawn depends on."""
    if not CACHE_DIR.exists():
        return
    if _tree_bytes(CACHE_DIR) > CACHE_MAX_BYTES:
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(CACHE_DIR, 0o777)
