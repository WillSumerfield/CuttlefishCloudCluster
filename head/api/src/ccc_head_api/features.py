"""Runtime **profiles** for Arm Features (temp/FEATURES_BUILTIN.md) — what CCC does for a Dive that
asked for one.

A Feature is a name in `cuttle.config.FEATURE_NAMES`. Without a profile it buys two things: placement
(a Slurm `Feature=` token) and `$<NAME>_PATH` in the Dive's environment — all `isaaclab` gets, and a
complete, useful Feature on its own.

A profile adds the rest the Head needs to KNOW about a Feature: which packages the submitter must not
install (they come from the Arm), the system libraries its binaries link against, and the prose
`cuttle feature show` prints. Everything a profile *does* for a Dive now happens on the Arm — the
per-Dive arrangement in-container after the build (`cuttle.feature_hook`), the library prefixes and the
mirrored package at `cuttle setup --arm` (`cuttle.features`). The Head renders one script for the whole
fleet and cannot know where any donor installed anything, so the doing is the Arm's.

The two packages never import each other; their only couplings are `/etc/ccc/features.env` and the
spec lists duplicated into `cuttle.features` (a drift test proves those agree).
"""

import textwrap
from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    """One Feature's runtime description, held on the Head.

    `libs` are the system libraries the Feature's shared objects link against — the ONE canonical copy,
    CCC's own responsibility rather than the submitter's (`cuttle.features.build_libs` builds exactly
    this list into a conda prefix at `setup --arm`, from a must-agree mirror of it). Each entry is
    `(spec, why)` — the reason travels with the requirement, since "why is assimp pinned to 5" is the
    question a future reader of this list will actually have.

    `alt_libs` are specs that CONFLICT with `libs` and so cannot share its prefix — a second major
    version of the same package, each becoming its own prefix. The loader resolves by SONAME, so two
    majors on one path do not compete: a binary naming `libcudart.so.12` finds the prefix carrying that
    file and never sees the other.

    `supplies` names the packages that come from the ARM and must therefore never be installed into a
    submitter's Environment — see `SUPPLIED_BY` and `environments.check_supplied`.
    """

    name: str
    summary: str
    python: str = ""
    python_why: str = ""
    libs: tuple[tuple[str, str], ...] = ()
    alt_libs: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    supplies: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


# --- vlearn -------------------------------------------------------------------------------------
# The package comes from the ARM, not the submitter's Environment — vlearn is licensed per machine
# through TurboActivate, whose activation state is tied to a specific release, so the copy that runs
# has to be the copy the Arm's owner activated. `cuttle setup --arm` unpacks the Arm's Linux wheel and
# the in-container hook (`cuttle.feature_hook`) puts it on PYTHONPATH; the Head's part is only to refuse
# a submitter who declares vlearn without the Feature (`environments.check_supplied`) and to describe it.
VLEARN = Profile(
    name="vlearn",
    summary="Vsim/vlearn — GPU physics + RL, licensed per machine (TurboActivate).",
    python="3.11",
    python_why="the only versions vlearn ships a Linux wheel for, with 3.10. An Environment now "
               "carries its own interpreter, so any manifest can ask for it — but it must be asked "
               "for, since a binary wheel cannot be built for a version it was not compiled against",
    # vlearn itself, which is why a submitter must never install it: the Arm holds the build its own
    # licence covers, and CCC puts that on PYTHONPATH. A declared vlearn is replaced by an empty stub
    # before the Environment is built and deleted again on the Arm (cuttle.feature_hook).
    supplies=("vlearn",),
    # libgym.so links all of these dynamically, resolving at LOAD time — required even headless
    # (missing one is an OSError on import, not a render error). conda-forge not apt: base.sif is a
    # pinned, cluster-wide image with no per-Arm hook, so baking one Feature's deps in would change
    # the image for every Arm.
    libs=(
        ("sdl2", ""),
        ("assimp=5", "conda-forge defaults to 6 (libassimp.so.6); libgym.so wants libassimp.so.5"),
        ("zeromq", ""),
        ("czmq", ""),
        ("libglu", ""),
        ("mesalib", "software GL, so a Dive is not relying on --nv having injected a libGL.so.1"),
        # From conda-forge rather than the `nvidia-cuda-runtime` PyPI wheel it used to be. That wheel
        # needed a pip, which needed a Python, in a prefix whose whole point is that it holds neither —
        # this prefix is libraries and nothing else. (libcuda.so.1 is the host driver's, injected by
        # `--nv`; only the runtime comes from here.)
        ("cuda-cudart=13", "libcudart.so.13, which libvsimdynamics.so needs and neither a uv "
                           "Environment nor a torch-free conda one has any other source for"),
    ),
    # The CUDA major is a property of the ARM's vlearn build, not of the cluster — and it is pinned
    # here, on the Head, for every Arm at once. An Arm whose wheel was built against CUDA 12 failed on
    # `OSError: libcudart.so.12: cannot open shared object file` with everything else on its
    # LD_LIBRARY_PATH supplying 13, and nothing in CCC could tell it apart from an Arm needing 13.
    #
    # Both are shipped rather than detected: they cost one extra conda solve at setup and cannot
    # collide (see `alt_libs`), where detection would need the major recorded per Arm at enroll, a
    # Head-side render that varies by placement, and a migration for every Arm already enrolled.
    alt_libs=(
        ("cuda12", (("cuda-cudart=12", "libcudart.so.12, for an Arm whose vlearn was built against "
                                       "CUDA 12 — the majors are not compatible and the SONAME is "
                                       "what the loader matches"),)),
    ),
    notes=(
        "assets/, shaders/, cache/ and TurboActivate.dat come from the Arm — do not upload your own.",
        "You may declare vlearn as a path to your own wheel, and should if that is what makes your "
        "project resolve. CCC replaces it with an empty placeholder before building, and removes "
        "that again on the Arm: what you import is the Arm's own build, which is the one its licence "
        "covers. A Dive that declares vlearn must also ask for this Feature.",
    ),
)

# --- isaaclab -----------------------------------------------------------------------------------
# No profile. An Arm declaring it gets placement and $ISAACLAB_PATH — a complete answer for software
# a submitter drives directly.
PROFILES: dict[str, Profile] = {VLEARN.name: VLEARN}


def profiled(features: list[str]) -> list[str]:
    """Which of `features` carry a **runtime profile** — the ones whose setup needs an interpreter.

    A profiled Feature attaches its software to the Dive's **Environment** (the in-container hook picks
    the ABI, deletes the stub, and sets PYTHONPATH). So one requested by a Dive that declared no
    Environment has nothing to attach to, and cannot work — `dive.request` refuses it.

    Sorted, so a refusal names them the same way however the User typed the request.
    """
    return sorted({f for f in features if f in PROFILES})


# Which Feature supplies a package the submitter must therefore not install: `{package: feature}`.
# Built from the profiles rather than written out, so adding a package to a Profile's `supplies` is the
# only edit needed. Names are lowercased — a dependency may be spelled any way PEP 503 allows.
SUPPLIED_BY: dict[str, str] = {
    pkg.lower(): p.name for p in PROFILES.values() for pkg in p.supplies
}


def runtime_summary(name: str) -> str:
    """What CCC arranges for a Feature, in prose — served by `GET /features/{name}/runtime` and printed
    by `cuttle feature show <name>`.

    This used to emit a starter `environment.yml` for the submitter to copy into their project, because
    the system libraries below had to be in THEIR manifest or nothing would load them. CCC builds them
    itself now (`cuttle.features.build_libs`), so copying this list into a project would only install a
    second copy of it — hence a description rather than a file.

    The list is still here, and still the one canonical copy, because "why is my Environment 500 MB
    bigger when I ask for vlearn" and "which Python may I pin" are questions someone will have.

    Raises KeyError for a Feature with no profile — the caller turns that into a 404, since "this name
    has no runtime to describe" is a real and permanent answer, not an error.
    """
    p = PROFILES[name]
    out = [f"{p.name} — {p.summary}", ""]
    for n in p.notes:
        out += textwrap.wrap(n, 96, initial_indent="  ", subsequent_indent="  ") + [""]
    if p.libs:
        out.append("CCC builds these into every Dive that asks for this Feature. Do not declare them")
        out.append("yourself — they are system libraries, and no Python manifest can install them:")
        for s, why in [*p.libs, *(e for _suffix, extra in p.alt_libs for e in extra)]:
            out.append(f"  {s}" + (f"   # {why}" if why and len(s) + len(why) < 88 else ""))
            if why and len(s) + len(why) >= 88:
                out += textwrap.wrap(why, 92, initial_indent="      # ", subsequent_indent="      # ")
        out.append("")
    if p.python:
        out.append(f"Your Environment must be Python {p.python}.")
        out += textwrap.wrap(p.python_why, 96, initial_indent="  ", subsequent_indent="  ")
        out.append("")
        out.append("Any manifest can ask for it — an Environment carries its own interpreter:")
        out.append(f'  pyproject.toml   requires-python = "=={p.python}.*"')
        out.append(f"  environment.yml  - python={p.python}")
        out.append(f"  requirements.txt put {p.python} in a .python-version file beside it")
    return "\n".join(out).rstrip() + "\n"
