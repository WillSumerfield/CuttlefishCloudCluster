"""Per-Spawn Python **Environment**s — declared by the User, built by the Head, kept by nobody.

A project declares its dependencies in the file its own tooling already writes, and the Head builds that
before the Spawn is placed.

**Built on the Head, before placement — an economy decision.** Building inside the allocation would make
a User pay Karma to hold a GPU while pip downloads — Settlement bills seconds *held*, not seconds busy.
Building here costs no Karma, no Device, and needs no per-Arm cache.

**Built while the Spawn queues, not while the User waits.** `spawns.submit` `sbatch --hold`s the Spawn
first, so it has its real id, Attribution and Escrow immediately, and runs the build behind the request.
The hold is a dispatch interlock only — Slurm can't place the Spawn before the env exists, but the build
overlaps the queue wait instead of adding to it.

**In a Pod, as the submitter's Guise.** A manifest is code: build backends, sdist `setup.py`, conda
post-link scripts all execute. So the build gets the same walls a Spawn gets — `base.sif`, entered as
that User's Guise — rather than a second, weaker isolation invented for it. `render_build` returns argv
the caller runs through `guises.as_owner`; nothing here runs as the Mantle.

**One fixed path, so nothing needs relocating.** A venv bakes its own prefix into `pyvenv.cfg` and every
console-script shebang; a conda prefix bakes it into hundreds of files and is famously not relocatable.
Both are built AND run at `ENV_MOUNT`, bound there on the Head at build time and on the Arm at run time —
the directory underneath differs, the path inside the Pod never does.

**Self-contained: an Environment carries its own interpreter and inherits nothing** (CONTEXT.md,
**Environment**). A conda prefix always did. A uv venv is made to, by two settings that are the whole
mechanism: `UV_PYTHON_PREFERENCE=only-managed` forbids uv from adopting an interpreter it did not
install, and `UV_PYTHON_INSTALL_DIR` puts the one it downloads INSIDE the shipped tree. Both the venv's
`bin/python` symlink and its `pyvenv.cfg home` then point within `ENV_MOUNT`, so the "one fixed path"
invariant above already covers them.

That is what makes a `requires-python` pin mean anything: `uv sync` reads it, fetches that exact
standalone CPython, and builds against it — a version the Pod image neither has nor needs to have. It is
also what makes a **Recipe**'s freeze true of what its work runs *in*, not merely of what it declared.
Inheriting was the alternative, and it cannot be frozen: `--system-site-packages` is a runtime flag, not
a set of per-package links, so an inherited torch is whatever the image holds on the day the Spawn runs.

**Two packages are seeded into every env that its manifest did not ask for**: `wandb`, or "a Spawn
logs to its submitter's own account" (CONTEXT.md) quietly stops holding now that nothing is inherited;
and `pip`, which uv does not install into a venv on its own and whose absence would break a **Tide
Pool**'s promise that `pip install` at the shell works. Both unpinned, because an already-satisfied
requirement is a no-op for both installers: a User who declared their own version keeps it, exactly as
they used to when the image supplied one.

**Nothing is cached except downloads.** The built env is destroyed with the Spawn's staging, like the
uploaded project (`projects.py`) — keeps the Head from growing a fourth permanent disk consumer, and
keeps **Allowance** meaning a ceiling on things a User would *lose*, which a rebuildable env is not. The
package *download* cache (`CACHE_DIR`) persists: Mantle-owned, size-capped, never staged — the only
reason "rebuild every time" is affordable.

**Nothing is detected, because detection is a guess.** Every Spawn NAMES the file it declares, and
`detect` only checks that the named file is there and is inside the project. There is no auto-detection
order to learn and no way to decline: a `pyproject.toml` in a vendored repo is not an instruction to the
cluster, and an Environment is too load-bearing to be inferred from a file someone happened to commit.
A **Tide Pool** is the sole exception — it is a shell, not a run, and may decline to declare.
"""

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import features, guises

# Where the env is built and run, INSIDE the Pod, on every machine. Absolute paths baked into the env
# resolve against this and only this — see the module docstring.
ENV_MOUNT = "/opt/ccc/env"

# The two shapes ENV_MOUNT can hold. A conda prefix IS ENV_MOUNT — micromamba owns the whole directory
# and puts its interpreter at `bin/python`. A uv env is two siblings under it: the venv, and the
# standalone CPython the venv points at. They are siblings rather than nested because the venv is uv's
# to manage (`uv sync --inexact` prunes inside it) and the interpreter is not.
VENV_DIR = f"{ENV_MOUNT}/venv"
PY_INSTALL_DIR = f"{ENV_MOUNT}/py"

# What a manifest that names no Python version gets. `requirements.txt` says nothing about interpreters
# (a `pyproject.toml` has `requires-python` and uv obeys it), so one has to be chosen for it — and
# choosing is better than inheriting, which is what CCC no longer does. A `.python-version` beside the
# manifest overrides this; uv reads that file itself, so `render_build` simply omits `--python`.
DEFAULT_PYTHON = "3.12"

# The env's home inside the uploaded project, so it rides the transfer that already exists: `cp -a` on
# the Head's co-located Arm, the Siphon's `[proj-…]` module on a remote one. The JOB_SCRIPT moves it
# OUT of the project before running — it must not land in the Recovery Window copy, and a multi-GB env
# counted against RECOVERY_MAX_BYTES would disable recovery for every Spawn that declared much.
ENV_DIRNAME = ".ccc-env"

# The **Arm Feature** library prefix's home inside the uploaded project, for the same reason and by the
# same route as ENV_DIRNAME above: it is built on the Head, it has to reach the Arm, and the project is
# the transfer that already exists. A sibling of the env rather than a directory inside it, because it
# has a different owner — the submitter's Environment is theirs, this is CCC's — and because putting a
# CCC-owned directory inside a live conda prefix is asking for one of the two to prune the other.
FEATLIB_DIRNAME = ".ccc-featlibs"

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

# Scratch for the build tools, on DISK. `--contain` gives the Pod a /tmp carved from Apptainer's session
# directory, a RAM tmpfs capped by `sessiondir max size` in apptainer.conf (64 MB on this fleet). uv is
# pointed at the cache below, but pip (what an environment.yml's `pip:` section runs) writes downloads
# to TMPDIR, so any wheel bigger than what's left of that 64 MB dies — nvidia-cudnn-cu13 alone is 366 MB,
# and pip reports the resulting ENOSPC as "This is an issue with network connectivity, not pip", which
# misdirects debugging toward the network instead of the disk.
#
# A sibling of CACHE_DIR rather than a directory inside it: prune_cache below empties the WHOLE cache
# when over the cap, and one build finishing could otherwise delete another's half-downloaded wheel.
# Nothing here is cache — it's per-build and deleted when that build ends.
TMP_DIR = Path(os.environ.get("CCC_ENV_TMP", "/var/lib/ccc/head/envtmp"))

# Clears a hung solver without killing a legitimately slow one: uv resolves in seconds, a real conda
# solve can take minutes — and a self-contained env now downloads its own interpreter and its own torch
# rather than inheriting either, so the honest floor is a cold full install, not a delta.
BUILD_TIMEOUT_SECONDS = int(os.environ.get("CCC_ENV_BUILD_TIMEOUT", 30 * 60))

# A built env is not uploaded, so it escapes MAX_UPLOAD_BYTES entirely — yet it is still rsync'd onto a
# donor's disk over the Siphon, on every Spawn, because nothing is cached. 12GB, not the 8GB that was
# enough when uv envs were a delta over the image: a self-contained env holds its own interpreter (~90MB)
# and its own torch/cuDNN (~2.5GB) rather than borrowing the image's, so the same declaration weighs
# several GB more than it used to.
MAX_ENV_BYTES = int(os.environ.get("CCC_ENV_MAX", 12 * 1024**3))

# wandb, injected into every env because nothing is inherited any more (module docstring). UNPINNED on
# purpose: an already-satisfied requirement is a no-op, so a User who declared their own version keeps
# it — the same way a manifest naming a package the image shipped used to win silently.
WANDB_PKG = "wandb"


class BuildError(ValueError):
    """The declared environment could not be built.

    Raised at two different moments, which is why this class carries a log. From `detect` or
    `require_tool` it's a *rejection* — bad input or an unequipped Head, decided in microseconds and
    turned into a 4xx before any Spawn exists, so nothing is Escrowed and nothing queues. From
    `build` it's a failed **Spawn**: already accepted, Escrowed and held, so the failure is recorded as
    its `err` and a non-zero `exit`, and Settlement bills it nothing (never held a Device). `.log` is
    what the User reads to find out why.
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
        """The one line both clients echo at submit — see `describe` for the no-env case."""
        return f"{self.path} ({'conda' if self.kind == 'conda' else 'uv'})"

    @property
    def bin_dir(self) -> str:
        """Where this env's executables live inside the Pod. Depends on the SHAPE, not on the Head: a
        conda prefix is ENV_MOUNT itself, a uv env keeps its venv one level down (see VENV_DIR)."""
        return f"{ENV_MOUNT}/bin" if self.kind == "conda" else f"{VENV_DIR}/bin"

    @property
    def interpreter(self) -> str:
        """What the payload runs, and what `uv pip` is pointed at. A per-Spawn value rather than the
        module constant it used to be — the two shapes no longer share a path."""
        return f"{self.bin_dir}/python"


# A manifest is classified by its BASENAME, never by parsing it, so a User may keep several and point
# at one (`requirements-gpu.txt`, `envs/cuda12.yml`) without CCC having to read a file to find out what
# kind it is. The names here are uv's and conda's own conventions, not CCC's.
_SUFFIX_KINDS = (
    (("environment.yml", "environment.yaml"), "conda"),
    ((".yml", ".yaml"), "conda"),
    (("uv.lock", "pyproject.toml"), "uv-project"),
    ((".txt",), "uv-requirements"),
)

# What a User is told when they named nothing. Long, because it is the message that meets everyone who
# submitted the way they used to, and "required" alone would not tell them what to type.
_NOTHING_DECLARED = (
    "this Spawn declares no environment — name the dependency file to build it from, e.g. "
    "--manifest pyproject.toml (also uv.lock, requirements.txt, environment.yml). CCC does not "
    "guess: a dependency file in a repo is not by itself an instruction to the cluster, and the "
    "Environment your work runs in is too load-bearing to be inferred from one"
)


def detect(project: Path, manifest: str | None) -> Manifest:
    """Which declaration this Spawn makes. Pure — reads the tree, decides nothing else.

    **Naming one is mandatory** (CONTEXT.md, **Environment**). There is no auto-detection and no
    default: CCC infers nothing about an Environment, because a `pyproject.toml` in a repo may belong
    to something the User is vendoring, and because the Environment is what all their work runs in. The
    file must exist and must be inside the project.
    """
    rel = (manifest or "").strip()
    if not rel:
        raise BuildError(_NOTHING_DECLARED)
    # The same containment rule the declared-output names get (spawns.OUTPUT_NAME_RE): a manifest
    # path is joined onto a directory the Head owns, so `../../etc` must not be a way out of it.
    # Resolved against the project and required to stay inside it.
    target = (project / rel).resolve()
    if not str(target).startswith(str(project.resolve()) + os.sep) or not target.is_file():
        raise BuildError(f"no such manifest in this project: {rel}")
    return Manifest(kind=_classify(rel), path=rel)


def _classify(rel: str) -> str:
    base = Path(rel).name.lower()
    for names, kind in _SUFFIX_KINDS:
        if base in names or any(base.endswith(n) for n in names if n.startswith(".")):
            return kind
    raise BuildError(
        f"cannot tell what kind of manifest {rel} is — name it *.txt (pip), "
        f"*.yml/*.yaml (conda), or pyproject.toml/uv.lock (uv)"
    )


def require_tool(m: Manifest, feature_names: list[str] | None = None) -> None:
    """Refuse now if this Head has not got a binary this Spawn's build needs. Raises BuildError.

    `feature_names` matters because a Feature's system libraries are a conda prefix whatever the
    manifest is (`build_feature_libs`) — so a uv Spawn asking for one still needs micromamba, and
    finding that out at build time would cost the User an Escrow for a Head problem.

    A REJECTION, not a degradation. It used to be the latter — a Head provisioned before Environments
    existed ran the cluster base rather than refusing Spawns that had worked for months. There is no
    such Spawn left: every one names a manifest, so degrading would mean running the User's work
    against an Environment they did not ask for and cannot see. Better to say the Head is misconfigured
    and name the one command that fixes it. Nothing is Escrowed — this is decided before any Spawn
    exists.
    """
    needs = [MICROMAMBA_BIN if m.kind == "conda" else UV_BIN]
    if features.lib_specs(feature_names or []):
        needs.append(MICROMAMBA_BIN)
    for need in needs:
        if not need.exists():
            raise BuildError(
                f"this Head cannot build Environments — there is no {need.name} at {need}. Ask the "
                f"operator to re-run `cuttle setup --head`, which installs it."
            )


def describe(m: Manifest | None) -> str:
    """The submit-time echo. Said BEFORE the Spawn is accepted, because the worst case — a manifest one
    directory too deep — is not a build failure at all, so nothing else would ever mention it and the
    User discovers it as an ImportError after paying for a GPU.

    `None` only ever reaches here from a **Tide Pool**, the one Spawn that may decline to declare."""
    return f"environment: {m.summary}" if m else "environment: none declared (Tide Pool)"


def render_build(m: Manifest, project: Path | None = None) -> list[list[str]]:
    """The commands that build `m` at ENV_MOUNT, in order. Pure — runs nothing, so it is testable.

    Each is argv as run INSIDE the build Pod, with the project as cwd and ENV_MOUNT bound writable.
    `project` is read for one thing only — whether a `.python-version` sits beside the manifest — and
    only the `requirements.txt` path consults it.
    """
    uv = str(UV_BIN)
    if m.kind == "conda":
        return [
            [str(MICROMAMBA_BIN), "create", "--yes", "--prefix", ENV_MOUNT, "--file", m.path],
            [m.interpreter, "-m", "pip", "install", "--no-cache-dir", WANDB_PKG],
        ]
    # Two packages every uv env gets that its manifest did not ask for, in one command.
    #
    # `pip` because uv does not seed one and the env's own interpreter would otherwise answer "No
    # module named pip" — which breaks a **Tide Pool**'s promise that `pip install` at the shell works
    # (spawns.PAYLOAD_TIDEPOOL). ~10MB, shipped on every Spawn; the alternative was putting uv itself in
    # every Arm's image. A conda prefix already has one, so its own line below still uses `python -m pip`.
    #
    # `wandb` because nothing is inherited any more, and without it "a Spawn logs to its submitter's own
    # account" (CONTEXT.md) quietly stops holding.
    #
    # Both UNPINNED, and `uv pip` rather than `python -m pip` — which could not run before pip is there.
    # An already-satisfied requirement is a no-op, so a User who declared either one keeps their version.
    seed = [uv, "pip", "install", "--python", m.interpreter, "pip", WANDB_PKG]
    if m.kind == "uv-requirements":
        # A `requirements.txt` names no interpreter, so one is named here. `.python-version` is uv's own
        # convention and it reads the file itself — passing --python as well would override the User's
        # answer with ours, so the flag is omitted entirely when the file is there.
        venv = [uv, "venv", VENV_DIR]
        if project is None or not (project / ".python-version").is_file():
            venv = [uv, "venv", "--python", DEFAULT_PYTHON, VENV_DIR]
        return [venv, [uv, "pip", "install", "--python", m.interpreter, "-r", m.path], seed]
    # ONE command, no `uv venv` prestep: `uv sync` reads `requires-python`, fetches that exact standalone
    # CPython and builds the venv against it. A prestep could only guess the version uv is about to
    # resolve, and guessing wrong is what a `requires-python = "==3.11.14"` project would meet as a
    # resolver error about the interpreter it never asked for.
    #
    # Plain `uv sync` — NOT --no-install-project. uv already treats a project with no [build-system]
    # (or `package = false`) as *virtual* and installs only its dependencies, while a real src-layout
    # package gets installed, which is the only way its imports resolve.
    # NOT --active: that only means "use $VIRTUAL_ENV", and nothing in a build Pod sets it, so uv falls
    # back to its default `.venv` beside the manifest — inside the Mantle-owned project the Guise cannot
    # write, which fails every pyproject.toml/uv.lock Spawn with "Permission denied". UV_PROJECT_ENVIRONMENT
    # (set in `build`'s --env) names the path outright instead, same as the `uv pip` branch's --python.
    # `uv sync` still writes `uv.lock` into the project itself, unavoidably — see spawns._lend_project
    # for why the Guise may. --inexact keeps the wandb below from being pruned away by a later sync.
    return [[uv, "sync", "--inexact", "--no-dev"], seed]


@dataclass(frozen=True)
class Supplied:
    """One dependency the submitter declared that an **Arm Feature** supplies instead.

    `declared` is the path exactly as written in `[tool.uv.sources]`; `pod_dir` is the directory it
    resolves to as the build Pod will see it, which is where a stub of that wheel has to appear.
    `inside_project` means the upload already carries that directory, so the stub overwrites what was
    uploaded and needs no bind of its own.
    """
    package: str
    feature: str
    declared: str
    pod_dir: Path
    filename: str
    inside_project: bool


def _uv_sources(project: Path, m: Manifest) -> tuple[Path, dict]:
    """`(project_root, [tool.uv.sources])` for a uv project, or `(root, {})` for anything else.

    Only `pyproject.toml` has this table. A `uv.lock` manifest is named alongside one and uv reads both,
    so the root is the manifest's own directory either way.
    """
    root = (project / m.path).parent
    pp = root / "pyproject.toml"
    if m.kind != "uv-project" or not pp.is_file():
        return root, {}
    try:
        data = tomllib.loads(pp.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError):
        # Not this module's error to report. uv is about to read the same file and say something far
        # more useful about it than "could not parse", and that message reaches the User as the build log.
        return root, {}
    src = data.get("tool", {}).get("uv", {}).get("sources", {})
    return root, src if isinstance(src, dict) else {}


def supplied(project: Path, m: Manifest) -> list[Supplied]:
    """Which of this project's declared dependencies come from an **Arm Feature** instead.

    Reads `[tool.uv.sources]` and nothing else — a package is only claimed here if the User pointed at
    a local file for it, which is the one case CCC can honour by substitution. A bare `vlearn` in
    `dependencies` is left alone: it means the index, uv will fail to find it there, and uv's own
    message about that is clearer than anything invented here.

    Pure. Raises nothing — `check_supplied` is what turns a bad one into a refusal.
    """
    root, sources = _uv_sources(project, m)
    out = []
    for pkg, spec in sources.items():
        # PEP 503 normalisation, so `Vlearn` and `v_learn` find the same profile as `vlearn`.
        feat = features.SUPPLIED_BY.get(re.sub(r"[-_.]+", "-", pkg).lower())
        if not feat:
            continue
        for one in (spec if isinstance(spec, list) else [spec]):
            if not isinstance(one, dict) or not isinstance(one.get("path"), str):
                continue
            declared = one["path"]
            target = (root / declared).resolve()
            out.append(Supplied(
                package=pkg, feature=feat, declared=declared,
                pod_dir=target.parent, filename=target.name,
                inside_project=str(target).startswith(str(project.resolve()) + os.sep),
            ))
    return out


def check_supplied(project: Path, m: Manifest, requested: list[str]) -> list[Supplied]:
    """Validate every Feature-supplied dependency at SUBMIT time, and return them. Raises BuildError.

    All three checks are refusals rather than build failures, and that is the point of doing them here:
    each one is certain before the Spawn exists, and each would otherwise surface as an ImportError
    after the User has paid Karma to hold a GPU.
    """
    found = supplied(project, m)
    for s in found:
        # Asking for the package without asking for the Feature is always a mistake, never a choice:
        # CCC is about to replace the wheel with an empty stub (licensing — see features.VLEARN_SETUP),
        # and only the Feature puts the Arm's real one back. Without it the Spawn would build cleanly,
        # place anywhere, and die at `import`.
        if s.feature not in requested:
            raise BuildError(
                f"{m.path} declares '{s.package}', which comes from the '{s.feature}' Arm Feature, but "
                f"this Spawn did not request it. Add --feature {s.feature}, or drop the dependency — "
                f"CCC replaces a Feature's package with a placeholder and only the Feature puts the "
                f"Arm's own licensed build back."
            )
        # A wheel, because a stub of one is a file CCC can write. A directory or sdist source would
        # need a whole synthesized project, and no Feature has ever wanted one.
        if not s.filename.endswith(".whl"):
            raise BuildError(
                f"{m.path} points '{s.package}' at {s.declared}, which is not a wheel. A Feature's "
                f"package is replaced by a placeholder before the build, and CCC can only do that for "
                f"a `.whl` — point the path at one."
            )
        # The same containment rule `detect` applies to the manifest itself. The stub's directory is
        # BOUND into the build Pod at the path the source resolves to, so without this a `path` of
        # `../../../etc` would name a system directory as the mountpoint. One `..` reaches the staging
        # root, which is what the ordinary `../my-wheels/x.whl` layout needs and as far as anyone gets.
        limit = project.resolve().parent
        if not str(s.pod_dir).startswith(str(limit) + os.sep) and s.pod_dir != limit:
            raise BuildError(
                f"{m.path} points '{s.package}' at {s.declared}, which reaches outside the directory "
                f"CCC unpacked this project into. A path source may name a file in the project or in "
                f"a sibling directory beside it, and no further up than that."
            )
    return found


def _stub_wheel(dest: Path, package: str, filename: str) -> str:
    """Write an empty wheel at `dest` and return its sha256. Enough to satisfy a resolver and nothing
    more: name, version, the compatibility tag, and a package directory with nothing in it.

    Name, version and tag are read out of the FILENAME the User declared, never out of the real wheel —
    the Head has never seen that file. It is on the submitter's own machine (`../vlearn-main/...`) and
    is not uploaded, which is exactly why this has to be synthesized rather than emptied.

    No `Requires-Dist`: nothing in a User's dependency tree depends on a Feature's package, so the stub
    has no edges to preserve. A RECORD is written but left empty, which every installer accepts.
    """
    # `name-version(-build)?-pytag-abitag-plattag.whl`
    parts = filename[:-len(".whl")].split("-")
    version = parts[1] if len(parts) > 1 else "0"
    tag = "-".join(parts[-3:]) if len(parts) >= 5 else "py3-none-any"
    dist = f"{package}-{version}.dist-info"
    entries = [
        (f"{package}/__init__.py", ""),
        (f"{dist}/METADATA", f"Metadata-Version: 2.1\nName: {package}\nVersion: {version}\n"),
        (f"{dist}/WHEEL",
         f"Wheel-Version: 1.0\nGenerator: ccc\nRoot-Is-Purelib: false\nTag: {tag}\n"),
        (f"{dist}/RECORD", ""),
    ]
    with zipfile.ZipFile(dest, "w") as z:
        for name, body in entries:
            # A FIXED timestamp, so the same declaration always produces the same bytes and therefore
            # the same hash. zipfile stamps the current time by default, which would give every rebuild
            # of an unchanged project a different sha256 and make `_relock` rewrite the lock every time
            # — churn in a file the User can read, for no reason. 1980-01-01 is the zip epoch.
            z.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), body)
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def _relock(lock: Path, package: str, digest: str) -> bool:
    """Point `package`'s recorded hash in `uv.lock` at the stub. Returns whether anything changed.

    Not an optimisation — without it the build cannot succeed. uv records a sha256 of a path-sourced
    wheel in the lock and verifies it on EVERY sync, plain or `--locked`; there is no re-resolve escape
    from a mismatch. The stub is a different file, so its hash has to be the one in the lock.

    A targeted text edit rather than a TOML round-trip: `uv.lock` is uv's file, written in its own
    style, and rewriting the whole thing to change one string would hand back a file it has to accept
    in full. Only the `hash` inside this package's own `[[package]]` block moves; every other package
    stays frozen at exactly what it was resolved to.
    """
    if not lock.is_file():
        return False
    text = lock.read_text(encoding="utf-8", errors="replace")
    # The block runs from its own `[[package]]` header to the next one (or the end of the file).
    block = re.compile(
        r'\[\[package\]\]\nname = "' + re.escape(package) + r'"\n(?:(?!\n\[\[package\]\]).)*',
        re.S,
    )
    hit = block.search(text)
    if not hit:
        return False
    fixed = re.sub(r'hash = "sha256:[0-9a-f]+"', f'hash = "sha256:{digest}"', hit.group(0))
    if fixed == hit.group(0):
        return False
    lock.write_text(text[:hit.start()] + fixed + text[hit.end():], encoding="utf-8")
    return True


def substitute_supplied(project: Path, m: Manifest, work: Path) -> tuple[list[str], list[str]]:
    """Replace every Feature-supplied wheel with a stub. Returns `(extra binds, log lines)`.

    Two shapes, and the difference is only where the stub is written:

    * **Inside the project** — the User's real wheel rode the upload, and the stub overwrites it in
      place. No bind: the project is already mounted at its own path.
    * **Outside it** (`../vlearn-main/...`, the ordinary layout) — the file was never uploaded and does
      not exist on the Head at all. A directory is made under this build's scratch and BOUND at the
      path the source resolves to, so the wheel is there when uv looks. Apptainer creates the
      mountpoint even though its parent is not itself bound, which is what makes this work under
      `--contain`.

    Either way `uv.lock`'s recorded hash for that package is repointed at the stub, or the sync fails
    on a mismatch it has no way to resolve past.
    """
    root, _ = _uv_sources(project, m)
    binds, log, dirs = [], [], {}
    for s in supplied(project, m):
        if s.inside_project:
            host_dir = s.pod_dir
            host_dir.mkdir(parents=True, exist_ok=True)
        else:
            host_dir = dirs.get(s.pod_dir)
            if host_dir is None:
                # Named by index, not by the declared basename: two sources could name directories with
                # the same basename at different depths, and one scratch dir per POD path keeps them
                # apart. 0777 for the same reason every other build directory is — the Guise writes
                # nothing here, but it has to read it, and the Mantle cannot chown.
                host_dir = work / f"src{len(dirs)}"
                host_dir.mkdir(parents=True)
                os.chmod(host_dir, 0o777)
                dirs[s.pod_dir] = host_dir
                binds.append(f"{host_dir}:{s.pod_dir}")
        dest = host_dir / s.filename
        digest = _stub_wheel(dest, s.package, s.filename)
        os.chmod(dest, 0o644)
        relocked = _relock(root / "uv.lock", s.package, digest)
        log.append(f"ccc: '{s.package}' comes from the '{s.feature}' Arm Feature — replaced "
                   f"{s.declared} with an empty placeholder for the build"
                   + (" (and repointed uv.lock at it)" if relocked else ""))
    return binds, log


def build_feature_libs(project: Path, specs: dict[str, list[str]], guise: str,
                       build_tmp: Path) -> list[str]:
    """Build each requested **Arm Feature**'s system libraries into `project/FEATLIB_DIRNAME`, one conda
    prefix per Feature. Returns log lines. Raises BuildError.

    Conda, and only conda, because these are not Python packages: `libassimp.so.5` and `libSDL2` have no
    wheel and no manifest a submitter could put them in. They used to arrive inside the submitter's own
    `environment.yml` — which is why a vlearn Spawn had to be a conda Spawn. Now that an Environment
    carries its own interpreter, the manifest can be anything, and these have to come from somewhere
    that does not care.

    No interpreter is installed into this prefix and none is wanted: it is bound for its `lib/`
    directory alone (features.FEATLIB_MOUNT, on LD_LIBRARY_PATH), never run.
    """
    from .spawns import CCC_SIF

    out = project / FEATLIB_DIRNAME
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o777)
    log = []
    for feat, want in specs.items():
        argv = [str(MICROMAMBA_BIN), "create", "--yes", "--quiet",
                "--prefix", f"{features.FEATLIB_MOUNT}/{feat}", "-c", "conda-forge", *want]
        cmd = guises.as_owner([
            "apptainer", "exec", "--contain", "--pid", "--pwd", str(project),
            "--bind", f"{project},{out}:{features.FEATLIB_MOUNT},{CACHE_DIR}:/ccc-cache,"
                      f"{build_tmp}:/ccc-tmp,{TOOLS_DIR}:{TOOLS_DIR}:ro",
            "--env", "MAMBA_ROOT_PREFIX=/ccc-cache/mamba,TMPDIR=/ccc-tmp,"
                     "LC_ALL=C.UTF-8,LANG=C.UTF-8",
            str(CCC_SIF), *argv,
        ], guise)
        log.append(f"$ ccc feature-libs {feat}")
        try:
            r = subprocess.run(cmd, cwd="/", capture_output=True, text=True,
                               timeout=BUILD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise BuildError(f"building the '{feat}' Feature's system libraries timed out",
                             log="\n".join(log))
        log.append((r.stdout + r.stderr).strip())
        if r.returncode != 0:
            raise BuildError(
                f"could not build the '{feat}' Arm Feature's system libraries:\n{r.stderr.strip()}",
                log="\n".join(log))
    return log


def build(project: Path, m: Manifest, guise: str, feature_names: list[str] | None = None) -> str:
    """Build `m` into `project/ENV_DIRNAME`, as `guise`, inside a Pod. Returns the build log. Raises
    BuildError, whose text is what the Spawn shows as its `err`, with the log so far as `.log`.

    The env dir is created HERE rather than inside the Pod so its ownership is the Guise's from the
    start — apptainer binds it, doesn't create it, and a root-owned mountpoint would leave the build
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
        return _run_build(project, m, guise, env_dir, build_tmp, feature_names or [])
    finally:
        # Always: a timed-out or failed build leaves partial downloads behind, and nothing else ever
        # looks in here again. prune_cache does not cover this tree by design (see TMP_DIR).
        shutil.rmtree(build_tmp, ignore_errors=True)


def _run_build(project: Path, m: Manifest, guise: str, env_dir: Path, build_tmp: Path,
               feature_names: list[str]) -> str:
    """The build itself. Split out of `build` only so the scratch it is given is cleaned up whatever
    happens to it — every exit from here is inside that caller's `finally`."""
    from .spawns import CCC_SIF  # local: spawns imports this module, and the constant lives there

    log = [f"$ ccc build {m.summary}"]
    # BEFORE the build, because it changes what the build resolves: any dependency an **Arm Feature**
    # supplies is replaced by an empty stub of the same name and version, and the lock repointed at it.
    # `check_supplied` already refused everything invalid about these at submit time.
    src_binds, src_log = substitute_supplied(project, m, build_tmp)
    log += src_log
    for argv in render_build(m, project):
        # --contain: the build gets the same walls the payload gets, nothing of the Head's filesystem
        # beyond the five paths named here. TOOLS_DIR is bound read-only since uv/micromamba live on the
        # HEAD, not the image — without the bind the binary named in argv wouldn't exist in the Pod.
        # No --nv: a build needs no GPU, and asking for one on a Head whose cards are all busy would
        # fail for no reason.
        cmd = guises.as_owner([
            "apptainer", "exec", "--contain", "--pid", "--pwd", str(project),
            # `src_binds` is empty for almost every Spawn: it appears only when a path source names a
            # wheel OUTSIDE the upload, and carries the stub written in its place.
            "--bind", f"{project},{env_dir}:{ENV_MOUNT},{CACHE_DIR}:/ccc-cache,"
                      f"{build_tmp}:/ccc-tmp,{TOOLS_DIR}:{TOOLS_DIR}:ro"
                      + "".join(f",{b}" for b in src_binds),
            # UV_PROJECT_ENVIRONMENT points `uv sync` at VENV_DIR instead of the `.venv` it'd otherwise
            # create beside the manifest, in a project directory the Guise cannot write (render_build).
            # Harmless on the other two paths, which never consult it.
            #
            # The two settings that make an Environment SELF-CONTAINED, and the reason a `requires-python`
            # pin the image cannot satisfy is now buildable at all:
            #   only-managed  — uv may not adopt an interpreter it did not install. Without it uv happily
            #                   picks up a system python of the right version, and the venv it builds
            #                   points at a path that exists on the Head and not on the Arm.
            #   INSTALL_DIR   — the interpreter it downloads lands INSIDE the shipped tree rather than
            #                   under $HOME (which a Guise has not got) or on the Pod's 64MB /tmp.
            #
            # TMPDIR moves every tool's scratch off the Pod's 64 MB RAM /tmp and onto disk (TMP_DIR) —
            # set for all three build paths since uv stages large artifacts through TMPDIR too.
            # PIP_CACHE_DIR belongs with the OTHER caches, not the scratch deleted after every build —
            # without it pip re-fetches every wheel on every rebuild.
            "--env", f"UV_CACHE_DIR=/ccc-cache/uv,UV_PROJECT_ENVIRONMENT={VENV_DIR},"
                     f"UV_PYTHON_PREFERENCE=only-managed,UV_PYTHON_INSTALL_DIR={PY_INSTALL_DIR},"
                     f"MAMBA_ROOT_PREFIX=/ccc-cache/mamba,TMPDIR=/ccc-tmp,"
                     f"PIP_CACHE_DIR=/ccc-cache/pip,LC_ALL=C.UTF-8,LANG=C.UTF-8",
            str(CCC_SIF), *argv,
        ], guise)
        log.append(f"$ {' '.join(argv)}")
        try:
            # cwd="/" — a build has no relationship to the directory its caller happened to be standing
            # in, and apptainer's starter chdirs to the inherited cwd on the HOST before `--pwd` ever
            # applies (which is inside). Run as the Guise, that chdir fails on anything the Guise cannot
            # traverse, and "container creation failed: can't change directory to /home/<operator>/..."
            # is what a Spawn's build reports instead of anything about itself. Bites every Head-local
            # caller (every pinned Spawn's build), never the API service, whose cwd is systemd's.
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

    # AFTER the Environment, and measured separately: this prefix is CCC's, not the submitter's, so a
    # Feature's libraries must not be what pushes their own declaration over MAX_ENV_BYTES. Nothing in
    # it depends on the env and nothing in the env depends on it — they meet on LD_LIBRARY_PATH, on the
    # Arm. Last because it is the cheaper of the two to lose if the env build has already failed.
    specs = features.lib_specs(feature_names)
    if specs:
        log += build_feature_libs(project, specs, guise, build_tmp)
    return "\n".join(log)


def _tree_bytes(root: Path) -> int:
    """Apparent size of the built env. Follows no symlinks, and counts none: a uv env's `bin/python` and
    the minor-version alias beside its interpreter both point back into this same tree, so following
    them would bill the User twice for one copy of CPython."""
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
