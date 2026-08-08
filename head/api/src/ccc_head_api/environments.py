"""Per-Spawn Python **Environment**s — declared by the User, built by the Arm that runs them, kept by
nobody.

A project declares its dependencies in the file its own tooling already writes. This module decides what
that declaration MEANS — which file, which shape, which commands build it — and the Arm runs those
commands at job start.

**Built on the Arm, because a built env cannot be moved cheaply.** It used to be built here and shipped
inside the uploaded project. That is several GB across the mesh on every Spawn, and the mesh is not a
LAN: measured Head→remote Arm at 2.82 MB/s, so a 4.6 GB torch env is ~29 minutes of transfer before the
payload's first line. The manifest is kilobytes and every donor box has its own link to PyPI, so the
declaration travels and the download does not.

**Decided here, executed there.** `detect`, `require_tool`, `check_supplied` and `render_build` all still
run on the Head, at submit, and every refusal they can raise is still a 4xx with no Spawn in existence.
Only the *running* moved: `render_build` returns argv, `spawns._render` splices it into the job script,
and the Arm executes it inside the Pod. One tested place still says what a manifest means.

**What that costs, stated plainly.** A build failure is now a failed **Spawn** rather than a refused
submission — it happens after the queue place and inside the Escrow, and the Escrow is not refunded (it
is not refunded for any other failed Spawn either). In exchange every Spawn can actually start.

**In a Pod, as the submitter's Guise.** A manifest is code: build backends, sdist `setup.py`, conda
post-link scripts all execute. So the build gets the same walls the payload gets — `base.sif`, entered as
that User's Guise, `--contain --pid` — rather than a second, weaker isolation invented for it. On the Arm
that is the same Pod flags the payload uses, one step earlier in the same job script.

**One fixed path, so nothing needs relocating.** A venv bakes its own prefix into `pyvenv.cfg` and every
console-script shebang; a conda prefix bakes it into hundreds of files and is famously not relocatable.
It is built AND run at `ENV_MOUNT`, in the same job, on the same box — which is now trivially true rather
than something two machines had to agree on.

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

**Nothing is cached except downloads.** The built env lives in the Spawn's node-local scratch and dies
with it, like the working directory around it — no box grows a permanent env store, and **Allowance**
keeps meaning a ceiling on things a User would *lose*, which a rebuildable env is not. The package
*download* cache on each Arm persists: node-local, size-capped, never staged, shared by every Guise on
the box — the only reason "rebuild every time" is affordable. The second Spawn to want a given torch
hard-links it out of that cache instead of fetching it.

**Nothing is detected, because detection is a guess.** Every Spawn NAMES the file it declares, and
`detect` only checks that the named file is there and is inside the project. There is no auto-detection
order to learn and no way to decline: a `pyproject.toml` in a vendored repo is not an instruction to the
cluster, and an Environment is too load-bearing to be inferred from a file someone happened to commit.
A **Tide Pool** is the sole exception — it is a shell, not a run, and may decline to declare.
"""

import hashlib
import os
import re
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import features

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

# Where the stub wheels an **Arm Feature** substitutes for a submitter's declared path source are
# written, INSIDE the uploaded project so they ride the transfer that already exists (`cp -a` on the
# Head's co-located Arm, the Siphon's `[proj-…]` module on a remote one). Kilobytes: each is an empty
# wheel of the right name and version. Only ever non-empty for a project whose `[tool.uv.sources]`
# points OUTSIDE its own upload — see `substitute_supplied`.
STUB_DIRNAME = ".ccc-stubs"

# uv and micromamba are single static binaries, installed on EVERY node (cuttle.setup.install_env_tools)
# and bound into the build Pod read-only. Deliberately not baked into BASE_DEF_TMPL: `base.sif` is built
# per node and skipped when it already exists, so a tool pinned inside the image could only be bumped by
# a full teardown of every enrolled box.
TOOLS_DIR = Path(os.environ.get("CCC_TOOLS_DIR", "/opt/ccc/tools"))
UV_BIN = TOOLS_DIR / "uv"
MICROMAMBA_BIN = TOOLS_DIR / "micromamba"

# The PARENT of the node-local package download caches — one subtree per **Guise**, never one shared
# tree (cuttle.config.NODE_ENV_CACHE_DIR says why: ownership does not propagate, and a shared writable
# cache is a supply chain between Users). Each is made by its own Guise in the job script, capped there,
# and swept wholesale by teardown. The cap is PER GUISE.
#
# Mirrors cuttle.config.NODE_ENV_CACHE_DIR — the same duplication TOOLS_DIR already carries across two
# packages that do not import each other.
CACHE_DIR = Path(os.environ.get("CCC_ENV_CACHE", "/var/lib/ccc/envcache"))
CACHE_MAX_GB = int(os.environ.get("CCC_ENV_CACHE_MAX_GB", 20))

# Where the build tools put scratch, INSIDE the Pod. `--contain` gives the Pod a /tmp carved from
# Apptainer's session directory, a RAM tmpfs capped by `sessiondir max size` in apptainer.conf (64 MB on
# this fleet). uv is pointed at the cache above, but pip (what an environment.yml's `pip:` section runs)
# writes downloads to TMPDIR, so any wheel bigger than what is left of that 64 MB dies — nvidia-cudnn-cu13
# alone is 366 MB, and pip reports the resulting ENOSPC as "This is an issue with network connectivity,
# not pip", which misdirects debugging toward the network instead of the disk.
#
# The job script binds the Spawn's own node-local scratch here, so it is per-Spawn and the EXIT trap
# already destroys it. Deliberately NOT inside the cache: the prune empties the whole cache at the cap,
# and one Spawn's prune must not delete another's half-downloaded wheel.
POD_TMP = "/ccc-tmp"
POD_CACHE = "/ccc-cache"

# wandb, injected into every env because nothing is inherited any more (module docstring). UNPINNED on
# purpose: an already-satisfied requirement is a no-op, so a User who declared their own version keeps
# it — the same way a manifest naming a package the image shipped used to win silently.
WANDB_PKG = "wandb"

# The build Pod's environment, identical on every node because the Pod's own paths are. Rendered into
# the job script by `spawns._render`; nothing here reads it at build time any more.
#
#   UV_PROJECT_ENVIRONMENT  points `uv sync` at VENV_DIR instead of the `.venv` it would otherwise create
#                           beside the manifest, inside a project directory the Guise cannot write.
#                           Harmless on the other two shapes, which never consult it.
#   only-managed            uv may not adopt an interpreter it did not install. Without it uv happily
#                           picks up whatever system python the image or the box happens to have, and the
#                           env stops being self-contained (module docstring).
#   UV_PYTHON_INSTALL_DIR   the interpreter it downloads lands INSIDE the env tree rather than under
#                           $HOME (which a Guise has not got) or on the Pod's 64MB /tmp.
#   TMPDIR                  every tool's scratch onto disk — see POD_TMP.
#   PIP_CACHE_DIR           with the OTHER caches, not the per-Spawn scratch, or pip re-fetches every
#                           wheel on every build.
BUILD_POD_ENV = (f"UV_CACHE_DIR={POD_CACHE}/uv,UV_PROJECT_ENVIRONMENT={VENV_DIR},"
                 f"UV_PYTHON_PREFERENCE=only-managed,UV_PYTHON_INSTALL_DIR={PY_INSTALL_DIR},"
                 f"MAMBA_ROOT_PREFIX={POD_CACHE}/mamba,TMPDIR={POD_TMP},"
                 f"PIP_CACHE_DIR={POD_CACHE}/pip,LC_ALL=C.UTF-8,LANG=C.UTF-8")


class BuildError(ValueError):
    """The declared environment cannot be built, decided at SUBMIT.

    Only ever a *rejection* now — from `detect`, `require_tool` or `check_supplied`: bad input or an
    unequipped node, decided in microseconds and turned into a 4xx before any Spawn exists, so nothing is
    Escrowed and nothing queues.

    A build that FAILS is a different thing entirely and does not come through here: it happens on the
    Arm, inside the job, and it ends that Spawn the way every other failed Spawn ends — the build's own
    output in `err`, a non-zero `exit`, and no refund of the Escrow.
    """


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


def substitute_supplied(project: Path, m: Manifest) -> tuple[list[str], list[str]]:
    """Replace every Feature-supplied wheel with a stub. Returns `(extra binds, log lines)`.

    Done HERE, at submit, even though the build runs on the Arm: it is pure file-writing — a synthesized
    wheel and a one-line edit to `uv.lock` — and it is the same class of decision as `detect`. The Arm
    runs commands, it does not reason about manifests.

    Two shapes, and the difference is only where the stub is written:

    * **Inside the project** — the User's real wheel rode the upload, and the stub overwrites it in
      place. No bind: the project is already mounted at its own path on the Arm.
    * **Outside it** (`../vlearn-main/...`, the ordinary layout) — the file was never uploaded and does
      not exist on the Head at all. A directory is made under STUB_DIRNAME *inside the project*, so it
      rides the transfer, and BOUND on the Arm at the path the source resolves to. Apptainer creates the
      mountpoint even though its parent is not itself bound, which is what makes this work under
      `--contain`.

    Each returned bind is `<project-relative dir>:<pod path>`. Project-relative because the host path
    differs per node and only the Arm knows its own — `spawns._render` prefixes `$proj`.

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
                # the same basename at different depths, and one dir per POD path keeps them apart.
                rel = f"{STUB_DIRNAME}/src{len(dirs)}"
                host_dir = project / rel
                host_dir.mkdir(parents=True)
                # 0777 for the same reason every other build directory is: the Guise writes nothing here
                # but has to read it, and on the Head branch it is copied as that Guise.
                os.chmod(host_dir, 0o777)
                dirs[s.pod_dir] = host_dir
                binds.append(f"{rel}:{s.pod_dir}")
        dest = host_dir / s.filename
        digest = _stub_wheel(dest, s.package, s.filename)
        os.chmod(dest, 0o644)
        relocked = _relock(root / "uv.lock", s.package, digest)
        log.append(f"ccc: '{s.package}' comes from the '{s.feature}' Arm Feature — replaced "
                   f"{s.declared} with an empty placeholder for the build"
                   + (" (and repointed uv.lock at it)" if relocked else ""))
    return binds, log


def render_featlib_build(specs: dict[str, list[str]]) -> list[list[str]]:
    """The commands that build each requested **Arm Feature**'s system libraries, in order. Pure.

    Conda, and only conda, because these are not Python packages: `libassimp.so.5` and `libSDL2` have no
    wheel and no manifest a submitter could put them in. They used to arrive inside the submitter's own
    `environment.yml` — which is why a vlearn Spawn had to be a conda Spawn. Now that an Environment
    carries its own interpreter, the manifest can be anything, and these have to come from somewhere
    that does not care.

    One prefix per Feature, under features.FEATLIB_MOUNT. No interpreter is installed into it and none is
    wanted: it is bound for its `lib/` directory alone (on LD_LIBRARY_PATH), never run.

    Built on the Arm alongside the Environment, and for the same reason — it is CCC's own multi-hundred-MB
    conda prefix, and shipping it from the Head cost exactly what shipping the env cost.
    """
    return [[str(MICROMAMBA_BIN), "create", "--yes", "--quiet",
             "--prefix", f"{features.FEATLIB_MOUNT}/{feat}", "-c", "conda-forge", *want]
            for feat, want in specs.items()]
