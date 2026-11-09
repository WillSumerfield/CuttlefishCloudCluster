"""Per-Dive Python **Environment**s — declared by the User, built by the Arm that runs them, kept by
nobody.

**Uncalled since 2.0.** Batch Dives were the only thing that declared one, and the job-script block
that ran these commands went with them; Dives bring an optional Environment back. Kept, and tested, as
the meaning of a manifest.

A project declares its dependencies in the file its own tooling already writes. This module decides what
that declaration MEANS — which file, which shape, which commands build it — and the Arm runs those
commands at job start.

**Built on the Arm, because a built env cannot be moved cheaply.** It used to be built here and shipped
inside the uploaded project. That is several GB across the mesh on every Dive, and the mesh is not a
LAN: measured Head→remote Arm at 2.82 MB/s, so a 4.6 GB torch env is ~29 minutes of transfer before the
payload's first line. The manifest is kilobytes and every donor box has its own link to PyPI, so the
declaration travels and the download does not.

**Decided here, executed there.** `detect`, `require_tool`, `check_supplied` and `render_build` all still
run on the Head, at submit, and every refusal they can raise is still a 4xx with no Dive in existence.
Only the *running* moved: `render_build` returns argv, the job script splices it in,
and the Arm executes it inside the container. One tested place still says what a manifest means.

**What that costs, stated plainly.** A build failure is now a failed **Dive** rather than a refused
submission — it happens after the queue place and inside the Escrow, and the Escrow is not refunded (it
is not refunded for any other failed Dive either). In exchange every Dive can actually start.

**In a container, as the submitter's Guise.** A manifest is code: build backends, sdist `setup.py`, conda
post-link scripts all execute. So the build gets the same walls the payload gets — `base.sif`, entered as
that User's Guise, `--contain --pid` — rather than a second, weaker isolation invented for it. On the Arm
that is the same container flags the payload uses, one step earlier in the same job script.

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
standalone CPython, and builds against it — a version the container image neither has nor needs to have.
Inheriting was the alternative, and it cannot be frozen: `--system-site-packages` is a runtime flag, not
a set of per-package links, so an inherited torch is whatever the image holds on the day the Dive runs.

**One package is seeded into every uv env that its manifest did not ask for**: `pip`, which uv does
not install into a venv on its own and whose absence would break a **Dive**'s promise that
`pip install` at the shell works. Unpinned, because an already-satisfied requirement is a no-op: a User
who declared their own version keeps it, exactly as they used to when the image supplied one.

**Nothing is cached except downloads.** The built env lives in the Dive's node-local scratch and dies
with it, like the working directory around it — no box grows a permanent env store. The package
*download* cache on each Arm persists: node-local, size-capped, never staged, one 0700 subtree PER
Guise (never shared — a shared writable cache is a supply chain between Users) — the only reason
"rebuild every time" is affordable. The second Dive from one Guise to want a given torch hard-links it
out of that Guise's own cache instead of fetching it.

**Nothing is detected, because detection is a guess.** Every Dive NAMES the file it declares, and
`detect` only checks that the named file is there and is inside the project. There is no auto-detection
order to learn and no way to decline: a `pyproject.toml` in a vendored repo is not an instruction to the
cluster, and an Environment is too load-bearing to be inferred from a file someone happened to commit.
A **Dive** is the sole exception — it is a shell, not a run, and may decline to declare.
"""

import hashlib
import io
import os
import posixpath
import re
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import features

# Where the env is built and run, INSIDE the container, on every machine. Absolute paths baked into the env
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
# written: the Dive's own node-local scratch, NEVER inside the project. The bytes ride the job script
# (kilobytes — each is an empty wheel of the right name and version), the Arm writes them out before the
# build, and each is BOUND over whatever the declaration points at.
#
# Outside `$proj` on purpose: a bind is visible to the build and invisible to the tree, so the build
# never writes into the User's project.
STUB_ROOT = "stubs"

# uv and micromamba are single static binaries, installed on EVERY node (cuttle.setup.install_env_tools)
# and bound into the build container read-only. Deliberately not baked into BASE_DEF_TMPL: `base.sif` is built
# per node and skipped when it already exists, so a tool pinned inside the image could only be bumped by
# a full teardown of every enrolled box.
TOOLS_DIR = Path(os.environ.get("CCC_TOOLS_DIR", "/opt/ccc/tools"))
UV_BIN = TOOLS_DIR / "uv"
MICROMAMBA_BIN = TOOLS_DIR / "micromamba"

# The PARENT of the node-local package download caches — one subtree per **Guise**, never one shared
# tree (cuttle.config.NODE_ENV_CACHE_DIR says why: ownership does not propagate, and a shared writable
# cache is a supply chain between Users). The parent is 0755 root-owned; each Guise's subtree is made
# 0700 by ccc-launch (which runs as root) and mounted alone into that Guise's Dive, and swept wholesale
# by teardown. The cap is PER GUISE.
#
# Mirrors cuttle.config.NODE_ENV_CACHE_DIR — the same duplication TOOLS_DIR already carries across two
# packages that do not import each other.
CACHE_DIR = Path(os.environ.get("CCC_ENV_CACHE", "/var/lib/ccc/envcache"))
CACHE_MAX_GB = int(os.environ.get("CCC_ENV_CACHE_MAX_GB", 20))

# Where the build tools put scratch, INSIDE the container. `--contain` gives the container a /tmp carved from
# Apptainer's session directory, a RAM tmpfs capped by `sessiondir max size` in apptainer.conf (64 MB on
# this fleet). uv is pointed at the cache above, but pip (what an environment.yml's `pip:` section runs)
# writes downloads to TMPDIR, so any wheel bigger than what is left of that 64 MB dies — nvidia-cudnn-cu13
# alone is 366 MB, and pip reports the resulting ENOSPC as "This is an issue with network connectivity,
# not pip", which misdirects debugging toward the network instead of the disk.
#
# The job script binds the Dive's own node-local scratch here, so it is per-Dive and the EXIT trap
# already destroys it. Deliberately NOT inside the cache: the prune empties the whole cache at the cap,
# and one Dive's prune must not delete another's half-downloaded wheel.
POD_TMP = "/ccc-tmp"
POD_CACHE = "/ccc-cache"

# The build container's environment, identical on every node because the container's own paths are. Rendered into
# the job script; nothing here reads it at build time any more.
#
#   UV_PROJECT_ENVIRONMENT  points `uv sync` at VENV_DIR instead of the `.venv` it would otherwise create
#                           beside the manifest, inside a project directory the Guise cannot write.
#                           Harmless on the other two shapes, which never consult it.
#   only-managed            uv may not adopt an interpreter it did not install. Without it uv happily
#                           picks up whatever system python the image or the box happens to have, and the
#                           env stops being self-contained (module docstring).
#   UV_PYTHON_INSTALL_DIR   the interpreter it downloads lands INSIDE the env tree rather than under
#                           $HOME (which a Guise has not got) or on the container's 64MB /tmp.
#   TMPDIR                  every tool's scratch onto disk — see POD_TMP.
#   PIP_CACHE_DIR           with the OTHER caches, not the per-Dive scratch, or pip re-fetches every
#                           wheel on every build.
BUILD_POD_ENV = (f"UV_CACHE_DIR={POD_CACHE}/uv,UV_PROJECT_ENVIRONMENT={VENV_DIR},"
                 f"UV_PYTHON_PREFERENCE=only-managed,UV_PYTHON_INSTALL_DIR={PY_INSTALL_DIR},"
                 f"MAMBA_ROOT_PREFIX={POD_CACHE}/mamba,TMPDIR={POD_TMP},"
                 f"PIP_CACHE_DIR={POD_CACHE}/pip,LC_ALL=C.UTF-8,LANG=C.UTF-8")


class BuildError(ValueError):
    """The declared environment cannot be built, decided at SUBMIT.

    Only ever a *rejection* now — from `detect`, `require_tool` or `check_supplied`: bad input or an
    unequipped node, decided in microseconds and turned into a 4xx before any Dive exists, so nothing is
    Escrowed and nothing queues.

    A build that FAILS is a different thing entirely and does not come through here: it happens on the
    Arm, inside the job, and it ends that Dive the way every other failed Dive ends — the build's own
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
        """Where this env's executables live inside the container. Depends on the SHAPE, not on the Head: a
        conda prefix is ENV_MOUNT itself, a uv env keeps its venv one level down (see VENV_DIR)."""
        return f"{ENV_MOUNT}/bin" if self.kind == "conda" else f"{VENV_DIR}/bin"

    @property
    def interpreter(self) -> str:
        """What the payload runs, and what `uv pip` is pointed at. A per-Dive value rather than the
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

# What a User is told when they named nothing. Long, because "required" is the one thing they already
# know by the time they are reading it.
NOTHING_DECLARED = (
    "this Dive declares no Environment — name the dependency file to build it from, e.g. "
    "--manifest pyproject.toml (also uv.lock, requirements.txt, environment.yml). The container carries no "
    "Python of its own, so a Dive that declares nothing has no interpreter to run your script with "
    "at all. CCC does not guess which file is the instruction: a dependency file in a repo is not "
    "always the one describing this work. If yours needs no packages, an empty requirements.txt is a "
    "complete declaration — it builds Python 3.12, or whatever a .python-version beside it names"
)


def detect(project: Path, manifest: str | None) -> Manifest:
    """Which declaration this Dive makes. Pure — reads the tree, decides nothing else.

    **Naming one is mandatory** (CONTEXT.md, **Environment**). There is no auto-detection and no
    default: CCC infers nothing about an Environment, because a `pyproject.toml` in a repo may belong
    to something the User is vendoring, and because the Environment is what all their work runs in. The
    file must exist and must be inside the project.
    """
    rel = (manifest or "").strip()
    if not rel:
        raise BuildError(NOTHING_DECLARED)
    # Containment: a manifest path is joined onto a directory the Head owns, so `../../etc` must not be a way out of it.
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


def require_tool(m: Manifest) -> None:
    """Refuse now if this Head has not got the binary this Dive's build needs. Raises BuildError.

    Only the manifest decides it: uv for a uv/pip Dive, micromamba for a conda one. A Feature's system
    libraries used to add micromamba here too, back when CCC built them on the Arm per Dive — they are
    built once at `cuttle setup --arm` now (cuttle.features.build_libs), on the Arm, so a Feature no
    longer changes what the Head must hold.

    A REJECTION, not a degradation. It used to be the latter — a Head provisioned before Environments
    existed ran the cluster base rather than refusing Dives that had worked for months. There is no
    such Dive left: every one names a manifest, so degrading would mean running the User's work
    against an Environment they did not ask for and cannot see. Better to say the Head is misconfigured
    and name the one command that fixes it. Nothing is Escrowed — this is decided before any Dive
    exists.
    """
    need = MICROMAMBA_BIN if m.kind == "conda" else UV_BIN
    if not need.exists():
        raise BuildError(
            f"this Head cannot build Environments — there is no {need.name} at {need}. Ask the "
            f"operator to re-run `cuttle setup --head`, which installs it."
        )


def describe(m: Manifest | None) -> str:
    """The submit-time echo. Said BEFORE the Dive is accepted, because the worst case — a manifest one
    directory too deep — is not a build failure at all, so nothing else would ever mention it and the
    User discovers it as an ImportError after paying for a GPU.

    `None` only ever reaches here from a **Dive**, the one Dive that may decline to declare."""
    return f"environment: {m.summary}" if m else "environment: none declared (Dive)"


def render_build(m: Manifest, project: Path | None = None) -> list[list[str]]:
    """The commands that build `m` at ENV_MOUNT, in order. Pure — runs nothing, so it is testable.

    Each is argv as run INSIDE the build container, with the project as cwd and ENV_MOUNT bound writable.
    `project` is read for one thing only — whether a `.python-version` sits beside the manifest — and
    only the `requirements.txt` path consults it.
    """
    uv = str(UV_BIN)
    if m.kind == "conda":
        return [
            [str(MICROMAMBA_BIN), "create", "--yes", "--prefix", ENV_MOUNT, "--file", m.path],
        ]
    # The one package every uv env gets that its manifest did not ask for: `pip`, because uv does not
    # seed one and the env's own interpreter would otherwise answer "No module named pip" — which breaks
    # a **Dive**'s promise that `pip install` at the shell works. ~10MB,
    # shipped on every Dive; the alternative was putting uv itself in every Arm's image. A conda prefix
    # already has one.
    #
    # UNPINNED, and `uv pip` rather than `python -m pip` — which could not run before pip is there. An
    # already-satisfied requirement is a no-op, so a User who declared it keeps their version.
    seed = [uv, "pip", "install", "--python", m.interpreter, "pip"]
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
    # NOT --active: that only means "use $VIRTUAL_ENV", and nothing in a build container sets it, so uv falls
    # back to its default `.venv` beside the manifest — inside the Mantle-owned project the Guise cannot
    # write, which fails every pyproject.toml/uv.lock Dive with "Permission denied". UV_PROJECT_ENVIRONMENT
    # (set in `build`'s --env) names the path outright instead, same as the `uv pip` branch's --python.
    # `uv sync` still writes `uv.lock` into the project itself, unavoidably.
    # --inexact keeps the seeded pip below from being pruned away by a later sync.
    return [[uv, "sync", "--inexact", "--no-dev"], seed]


@dataclass(frozen=True)
class Supplied:
    """One dependency the submitter declared that an **Arm Feature** supplies instead.

    `declared` is the path exactly as written in `[tool.uv.sources]`. `pod_rel` is the same file
    expressed relative to the PROJECT ROOT — the only form that means anything on the Arm, since the
    project lands in node-local scratch under a name no Head path predicts. It may begin `../`, which is
    the ordinary `../my-wheels/x.whl` layout and resolves into the Dive's own scratch beside `$proj`.
    """
    package: str
    feature: str
    declared: str
    pod_rel: str
    filename: str


@dataclass(frozen=True)
class StubFile:
    """One placeholder wheel the Arm writes before the build. `rel` is the file's path relative to the
    PROJECT ROOT — exactly where uv expects the declared wheel, which the Arm overwrites in place."""
    rel: str
    data: bytes


def _sources_from_text(pyproject_text: str) -> dict:
    """`[tool.uv.sources]` parsed out of a `pyproject.toml`'s text, or `{}` for anything without one.

    Text, not a path: the Head never holds the project, only the manifest bytes `dive-prep` returns.
    A conda/pip Dive has no such table, and a parse error is uv's to report far better than this.
    """
    try:
        data = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError:
        return {}
    src = data.get("tool", {}).get("uv", {}).get("sources", {})
    return src if isinstance(src, dict) else {}


def supplied(pyproject_text: str, env_rel: str) -> list[Supplied]:
    """Which of this project's declared dependencies come from an **Arm Feature** instead.

    `env_rel` is the manifest's path relative to the project root (the `--env` the Dive carries); a
    source path is written relative to the manifest's own directory, and `pod_rel` re-expresses it
    against the project root — the only form the Arm can place.

    Reads `[tool.uv.sources]` and nothing else — a package is only claimed here if the User pointed at
    a local file for it, which is the one case CCC can honour by substitution. A bare `vlearn` in
    `dependencies` is left alone: it means the index, uv will fail to find it there, and uv's own
    message about that is clearer than anything invented here.

    Pure, text in. Raises nothing — `check_supplied` is what turns a bad one into a refusal.
    """
    manifest_dir = posixpath.dirname(env_rel)
    out = []
    for pkg, spec in _sources_from_text(pyproject_text).items():
        # PEP 503 normalisation, so `Vlearn` and `v_learn` find the same profile as `vlearn`.
        feat = features.SUPPLIED_BY.get(re.sub(r"[-_.]+", "-", pkg).lower())
        if not feat:
            continue
        for one in (spec if isinstance(spec, list) else [spec]):
            if not isinstance(one, dict) or not isinstance(one.get("path"), str):
                continue
            declared = one["path"]
            # Normalised so `sub/../wheels/x.whl` says what it means, re-expressed against the project
            # root from the manifest's directory. All string maths — no file on the Head to resolve.
            pod_rel = posixpath.normpath(posixpath.join(manifest_dir, declared))
            out.append(Supplied(
                package=pkg, feature=feat, declared=declared,
                pod_rel=pod_rel, filename=posixpath.basename(declared),
            ))
    return out


def check_supplied(pyproject_text: str, env_rel: str, requested: list[str]) -> list[Supplied]:
    """Validate every Feature-supplied dependency at SUBMIT time, and return them. Raises BuildError.

    Every check is a refusal rather than a build failure, and that is the point of doing them here:
    each one is certain before the Dive exists, and each would otherwise surface as an ImportError
    after the User has paid Karma to hold a GPU.
    """
    found = supplied(pyproject_text, env_rel)
    for s in found:
        # Asking for the package without asking for the Feature is always a mistake, never a choice:
        # CCC is about to replace the wheel with an empty stub (licensing — see cuttle.feature_hook),
        # and only the Feature puts the Arm's real one back. Without it the Dive would build cleanly,
        # place anywhere, and die at `import`.
        if s.feature not in requested:
            raise BuildError(
                f"{env_rel} declares '{s.package}', which comes from the '{s.feature}' Arm Feature, but "
                f"this Dive did not request it. Add --feature {s.feature}, or drop the dependency — "
                f"CCC replaces a Feature's package with a placeholder and only the Feature puts the "
                f"Arm's own licensed build back."
            )
        # A wheel, because a stub of one is a file CCC can write. A directory or sdist source would
        # need a whole synthesized project, and no Feature has ever wanted one.
        if not s.filename.endswith(".whl"):
            raise BuildError(
                f"{env_rel} points '{s.package}' at {s.declared}, which is not a wheel. A Feature's "
                f"package is replaced by a placeholder before the build, and CCC can only do that for "
                f"a `.whl` — point the path at one."
            )
        # The declared path is rendered into the job script's `--bind` list, inside a double-quoted
        # shell string. It is User text, so it is held to an alphabet rather than escaped: a `$`, a
        # backtick or a quote in a wheel's path would otherwise be shell the submitter wrote and the
        # Head ran. A comma would be no safer — that is the separator apptainer's own bind list uses.
        if not re.fullmatch(r"[A-Za-z0-9._+/-]+", s.declared):
            raise BuildError(
                f"{env_rel} points '{s.package}' at {s.declared}, which CCC will not use as a path. "
                f"Name it with letters, digits and `. _ + - /` only — the path is handed to the "
                f"build's own mounts, and anything else there could not be told from an instruction."
            )
        # The same containment rule `detect` applies to the manifest itself, stated against what the
        # USER WROTE rather than against a path on the Head. The stub is bound into the build container at
        # `$proj/<pod_rel>`, and `$proj`'s parent is the Dive's own node-local scratch: one `..`
        # reaches that scratch, which is what the ordinary `../my-wheels/x.whl` layout needs, and two
        # would name a directory outside the Dive altogether. Saying it this way makes it mean the
        # same thing on the Head and on the Arm.
        if s.declared.startswith("/") or s.pod_rel.startswith("../../"):
            raise BuildError(
                f"{env_rel} points '{s.package}' at {s.declared}, which reaches outside the directory "
                f"CCC runs this project in. A path source may name a file in the project or in "
                f"a sibling directory beside it, and no further up than that."
            )
    return found


def _stub_wheel(package: str, filename: str) -> bytes:
    """An empty wheel, as bytes. Enough to satisfy a resolver and nothing more: name, version, the
    compatibility tag, and a package directory with nothing in it.

    Bytes rather than a file, because there is nowhere on the Head this belongs. It is written by the
    ARM, into that Dive's own scratch, out of the job script it already received.

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
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in entries:
            # A FIXED timestamp, so the same declaration always produces the same bytes and therefore
            # the same hash. zipfile stamps the current time by default, which would give every rebuild
            # of an unchanged project a different sha256 and make `_relock` rewrite the lock every time.
            # 1980-01-01 is the zip epoch.
            z.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), body)
    return buf.getvalue()


def stub_payload(found: list[Supplied]) -> tuple[list[StubFile], dict[str, str], list[str]]:
    """The placeholder bytes + lock digests the Arm needs, computed Head-side at submit.
    `(stub files, {package: sha256}, log lines)`.

    `found` is `check_supplied`'s return. For each supplied wheel: an empty stub wheel keyed by its
    project-relative path (where the Arm writes it, in place over the declared path), and the sha256 the
    in-container relock must stamp into `uv.lock` — uv verifies a path-wheel's hash on every sync, so
    the stub's own hash has to be the one the lock records.

    The bulky `uv.lock` never travels: only the digests do, and `cuttle _feature-stub` rewrites the
    Arm-local copy of the lock from them before `uv sync`. Pure — the parsing stays here; the writing
    happens in the container.
    """
    files: list[StubFile] = []
    digests: dict[str, str] = {}
    log: list[str] = []
    for s in found:
        data = _stub_wheel(s.package, s.filename)
        files.append(StubFile(s.pod_rel, data))
        digests[s.package] = hashlib.sha256(data).hexdigest()
        log.append(f"ccc: '{s.package}' comes from the '{s.feature}' Arm Feature — {s.declared} is an "
                   f"empty placeholder for this build, and your own copy is untouched")
    return files, digests, log
