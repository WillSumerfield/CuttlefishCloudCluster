"""Runtime **profiles** for Arm Features (temp/FEATURES_BUILTIN.md) — what CCC does for a Dive that
asked for one.

A Feature is a name in `cuttle.config.FEATURE_NAMES`. Without a profile it buys two things: placement
(a Slurm `Feature=` token) and `$<NAME>_PATH` in the Dive's environment — all `isaaclab` gets, and a
complete, useful Feature on its own.

A profile adds the rest — package, binds, working directory, cache, env vars — so the submitter's
script contains **none** of it. Not a convenience: `LD_LIBRARY_PATH` is read by the dynamic loader once,
at `exec`, so a payload cannot set it for itself; whatever arranges it must run *outside* the process,
i.e. the job script, i.e. here.

**Everything a profile emits resolves on the Arm, not here.** The Head renders one script for the whole
fleet and cannot know where any donor installed anything, so snippets name `$VLEARN_PATH` and friends —
variables the Arm exports from `/etc/ccc/features.env`, written by `cuttle setup --arm` (see
`cuttle.features` for the Arm-side half: what it must have on disk and how it prepares it, offline).

**A profile's failures are the Dive's, and loud.** Every check ends in `feature_fail`, which writes a
sentence naming the Arm and what was wrong into the Dive's `err`, records exit 1, and stages out — a
mismatch reads as a mismatch, not an `ImportError` a User would blame on their own code.
"""

import textwrap
from dataclasses import dataclass

# Where a profile's Arm-side package is bound inside the container. Under /ccc rather than into the
# Environment's own tree: the Environment is bound read-only at one fixed path and belongs to the
# submitter, while this belongs to the Arm — two owners, two mount points, no chance of one silently
# shadowing part of the other.
FEATURE_MOUNT = "/ccc/feature"

# Where the SYSTEM LIBRARIES a Feature's shared objects link against are bound — a small conda prefix
# CCC builds on the Head per Dive (environments.build_feature_libs) and ships inside the project, the
# same way the Environment travels.
#
# A third mount for a third owner, by the same rule as FEATURE_MOUNT above. These are not the
# submitter's dependencies and must not be in their Environment: they used to be, back when a vlearn
# Dive's manifest HAD to be an `environment.yml` (conda was the only way to get an interpreter vlearn
# ships a wheel for). Now that an Environment carries its own interpreter, a submitter can declare a
# `pyproject.toml` — and uv has no way to install libassimp.so.5. So CCC builds these itself, and a
# submitter never has to discover by watching `import` fail that `libgym.so` wanted SDL2.
FEATLIB_MOUNT = "/ccc/featlibs"

# Where a profile's own WORKING DIRECTORY is bound — the directory a Feature's software resolves its
# vendor data against, built per Dive on the Arm and writable, since that is the one of these three
# mounts something inside the container writes to.
#
# A fourth mount for a fourth owner, and the reason is the same rule that separated the first three: it
# is CCC's, not the submitter's. It used to be `$proj` itself, which put a real cache/ inside the
# User's project. Sited here, a Feature's runtime touches no part of anyone's project.
VLWD_MOUNT = "/ccc/vlwd"


@dataclass(frozen=True)
class Profile:
    """One Feature's runtime, and the environment CCC builds when it needs to run that Feature itself.

    `setup` is bash, spliced into the job script after the Environment has been resolved (so `$py`,
    `$envbind` and `$envmount` are set) and before the payload runs. It contributes to two accumulator
    variables, `featbind` and `featenv`, each a leading-comma fragment appended to the `apptainer exec`
    bind and env lists.

    `libs` are the system libraries the Feature's shared objects link against — the ONE canonical copy,
    and CCC's own responsibility rather than the submitter's. `environments.build_feature_libs` builds
    exactly this list into a conda prefix at FEATLIB_MOUNT for every Dive that asks for the Feature, so
    a library added for a new release reaches every Dive without anyone editing a manifest.

    Each entry is `(spec, why)` — the reason travels with the requirement, since "why is assimp pinned
    to 5" is the question a future reader of this list will actually have.

    `alt_libs` are specs that CONFLICT with `libs` and so cannot share its prefix — a second major
    version of the same package. Each entry becomes its own prefix at `FEATLIB_MOUNT/<name>-<suffix>`,
    and the profile's `setup` puts it on LD_LIBRARY_PATH alongside the first. The loader resolves by
    SONAME, so two majors on one path do not compete: a binary naming `libcudart.so.12` finds the one
    prefix carrying that file and never sees the other.

    `supplies` names the packages that come from the ARM and must therefore never be installed into a
    submitter's Environment — see `SUPPLIED_BY` and `environments.substitute_supplied`.
    """

    name: str
    summary: str
    python: str = ""
    python_why: str = ""
    libs: tuple[tuple[str, str], ...] = ()
    alt_libs: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    supplies: tuple[str, ...] = ()
    setup: str = ""
    # Bash appended to `setup` for an ACTIVATION job only (`setup_snippet(activation=True)`), and the
    # payload that job runs. This is the one job allowed to see the Arm's licence *key*, as opposed to
    # the activation state the key produces. Uncalled since 2.0 dropped the activation call; rebuilt as
    # a Head-local job in phase 2.
    activation: str = ""
    activation_script: str = ""
    notes: tuple[str, ...] = ()


# --- vlearn -------------------------------------------------------------------------------------
# The package comes from the ARM, not the submitter's Environment — vlearn is licensed per machine
# through TurboActivate, whose activation state is tied to a specific release, so the copy that runs
# has to be the copy the Arm's owner activated.
#
# `cuttle setup --arm` unzips the Arm's Linux wheel once into an ABI-labelled directory under
# $VLEARN_PKG_ROOT; unpacking it OURSELVES (rather than pointing PYTHONPATH at a donor's own .venv)
# keeps the directory to one project's artefacts instead of dragging in everything else installed.
VLEARN_SETUP = r"""
# --- Arm Feature: vlearn ---------------------------------------------------
[ -n "${VLEARN_PATH:-}" ] || feature_fail \
  "this Arm advertises the 'vlearn' Feature but its /etc/ccc/features.env exports no VLEARN_PATH. Its owner should re-run 'cuttle setup --arm'."
[ -n "${VLEARN_PKG_ROOT:-}" ] || feature_fail \
  "this Arm advertises the 'vlearn' Feature but has no unpacked vlearn package (no VLEARN_PKG_ROOT). Its owner should re-run 'cuttle setup --arm'."
# vlearn ships binary wheels only (cp310/cp311), so the Environment's Python has to be one the Arm
# actually holds a build for. Checked HERE, on the Arm, because only the Arm knows what it unpacked --
# and checked before anything else, since every path below is inside the answer.
vl_pkg="$VLEARN_PKG_ROOT/$feature_abi"
[ -d "$vl_pkg" ] || feature_fail \
  "this Dive's Environment is Python $feature_abi, and this Arm's vlearn is built for: $(ls "$VLEARN_PKG_ROOT" 2>/dev/null | tr '\n' ' '). vlearn ships binary wheels only, so the two have to match -- pin that Python in your environment.yml. (No Environment at all means the base image's Python, which vlearn has never shipped a wheel for.)"
# The Environment must not contain a WORKING vlearn, and this is where that is made true rather than
# merely likely. The Head replaced the submitter's declared wheel with an empty stub before the build
# (environments.substitute_supplied), because vlearn is licensed per machine and the copy that runs has
# to be the copy this Arm's owner activated -- so what `uv sync` installed is a package directory with
# nothing in it.
#
# sys.path would already resolve `import vlearn` to the PYTHONPATH entry below, which precedes
# site-packages. That ordering is not a thing to rest a LICENSING invariant on: it is subtle, it is
# Python's to change, and a `.pth` file in site-packages can reorder it. Deleting the stub outright
# means there is only ever one vlearn on the box to find.
if [ -n "$envmount" ] && [ -n "$feature_purelib" ]; then
    # $feature_purelib is the container-side path the probe asked the interpreter for; the deletion happens
    # out here on the ARM's filesystem, where the env is still writable -- it is bound read-only into
    # every container, so this could not be done from inside one.
    vl_site="$scratch/env${feature_purelib#$envmount}"
    rm -rf "$vl_site/vlearn" "$vl_site"/vlearn-*.dist-info "$vl_site"/vlearn_bindings*.so \
           "$vl_site"/*vlearn*.pth
fi
# vlearn resolves assets/, shaders/, cache/ and TurboActivate.dat against VL_WORKING_DIRECTORY -- so
# those four names have to exist in whatever that points at. Three come from the Arm's install.
#
# A directory of CCC's OWN (VLWD_MOUNT), never `$proj` -- see VLWD_MOUNT. Nothing here is the
# submitter's, so nothing here belongs in the directory that is.
#
# That also settles what "the install always wins" used to have to argue: an upload carrying its own
# assets/ is no longer replaced, because the two are not in the same directory any more.
vlwd="$scratch/vlwd"
rm -rf "$vlwd"
mkdir -p "$vlwd"
# SYMLINKS, not copies or binds, and still load-bearing rather than tidy: ~1 GB of assets/ COPIED per
# Dive would be a minute of disk per run for vendor data that has not changed since setup.
ln -s "$VLEARN_PATH/assets" "$vlwd/assets"
ln -s "$VLEARN_PATH/shaders" "$vlwd/shaders"
ln -s "$VLEARN_PATH/TurboActivate.dat" "$vlwd/TurboActivate.dat"
# cache/ is the one of the four vlearn WRITES (compiled assets, content-addressed), so it cannot be a
# symlink into a read-only bind -- that is an EROFS the first time a Dive meets an asset the Arm's
# owner never opened. Copied per Dive instead: ~1 MB, warm from whatever they did generate, with no
# shared writers and nothing written back into their tree.
cp -r "$VLEARN_PATH/cache" "$vlwd/cache" 2>/dev/null || mkdir -p "$vlwd/cache"
# Only those three paths are bound, never the install root. The vendor's README puts the **Licence**
# key at the checkout root, and an ordinary Dive has no business seeing it (Phase 3's activation Dive
# is the one thing that does). Bound at their OWN host paths, so the symlinks just made resolve to the
# same files inside the container as outside.
#
# $vlwd itself is bound WRITABLE -- it is the only one of CCC's four mounts something inside the container
# writes to, and cache/ is what writes to it.
featbind="$featbind,$VLEARN_PATH/assets:$VLEARN_PATH/assets:ro,$VLEARN_PATH/shaders:$VLEARN_PATH/shaders:ro,$VLEARN_PATH/TurboActivate.dat:$VLEARN_PATH/TurboActivate.dat:ro,$vl_pkg:%%FEATURE_MOUNT%%/vlearn:ro,$vlwd:%%VLWD_MOUNT%%"
# The WHOLE unpacked wheel goes on PYTHONPATH, not its `vlearn/` subdirectory: a wheel unpacks to three
# top-level entries -- `vlearn/`, its `.dist-info`, and a sibling `vlearn_bindings.<abi>.so` -- and the
# bindings module is the one `import vlearn` fails without.
#
# LD_LIBRARY_PATH is why this whole block is CCC's job. libgym.so links SDL2, assimp, zeromq, czmq, GLU
# and libcudart dynamically; they resolve at load time, so they are needed even headless.
#
# They come from the prefix CCC built for this Feature (this profile's `libs`), NOT from the
# submitter's Environment as they used to -- an Environment can now be a `pyproject.toml`, and uv has
# no way to install libassimp.so.5. The Environment's own lib dir stays on the path behind it, since a
# conda manifest may still carry its own copies, and the nvidia wheel dirs the probe found go last:
# a torch install brings its own libcudart, and if it did, that is the one its extensions were built
# against.
# Both CUDA majors, because which one this Arm's build wants is not knowable here (see
# VLEARN.alt_libs). Distinct SONAMEs, so the loader takes whichever the binary names.
vl_ld="%%FEATURE_MOUNT%%/vlearn/vlearn/lib:%%FEATLIB_MOUNT%%/vlearn/lib:%%FEATLIB_MOUNT%%/vlearn-cuda12/lib"
[ -n "$envbind" ] && vl_ld="$vl_ld:$envmount/lib"
[ -n "$feature_nvlibs" ] && vl_ld="$vl_ld:$feature_nvlibs"
featenv="$featenv,PYTHONPATH=%%FEATURE_MOUNT%%/vlearn,VL_WORKING_DIRECTORY=%%VLWD_MOUNT%%,LD_LIBRARY_PATH=$vl_ld"
"""

# The extra an ACTIVATION Dive gets, and nothing else does. An ordinary Dive runs against activation
# state already in the Arm's **Licence** directory; the key that PRODUCED that state is the credential
# and stays out of every container but this one.
#
# The key never leaves the Arm: `cuttle setup --arm` mirrors the owner's file into that box's CCC state
# at 0640 (cuttle.features.mirror_licence_key — a container can't reach a file under someone's home), and
# this binds that copy at its own host path, so it has one name inside the container and out.
VLEARN_ACTIVATION = r"""
# --- Arm Feature: vlearn, ACTIVATION ---------------------------------------
# No Licence directory bound means `$HOME` is the container's throwaway tmpfs: TurboActivate would activate
# INTO it, exit 0, and the Arm would be marked activated with nothing kept -- one vendor activation
# burnt and every later Dive unlicensed. `$licbind` is the job script's answer to "is it bound".
[ -n "$licbind" ] || feature_fail \
  "this Arm has no Licence directory (CCC_LICENCE_HOME in /etc/ccc/features.env is unset or missing), so an activation would be written nowhere that lasts. Its owner should re-run 'cuttle setup --arm' and name one."
[ -n "${VLEARN_LICENCE_KEY:-}" ] || feature_fail \
  "this Arm declared the 'vlearn' Feature but recorded no licence key path, so CCC cannot activate it. Its owner should re-run 'cuttle setup --arm'."
[ -f "$VLEARN_LICENCE_KEY" ] || feature_fail \
  "this Arm's recorded vlearn licence key ($VLEARN_LICENCE_KEY) is not there any more. Its owner should re-run 'cuttle setup --arm'."
# Both, deliberately. VL_LICENSE_KEY_PATH is the documented override, and License.key beside the
# working directory is where the vendor's own instructions put it -- an activation that failed because
# only one of the two was honoured would be a very expensive thing to debug once per donated box.
#
# `$vlwd`, set by VLEARN_SETUP above (this block is only ever appended to it): "beside the working
# directory" has to mean the directory vlearn is actually told to resolve against, and that is no
# longer `$proj`.
ln -sf "$VLEARN_LICENCE_KEY" "$vlwd/License.key"
featbind="$featbind,$VLEARN_LICENCE_KEY:$VLEARN_LICENCE_KEY:ro"
featenv="$featenv,VL_LICENSE_KEY_PATH=$VLEARN_LICENCE_KEY"
"""

# What `activate_vlearn.sh` does, which is the vendor's own answer to "how do I activate this": the
# library activates itself the first time a gym is created. Headless, because an Arm has no display and
# because rendering is not what is being licensed.
VLEARN_ACTIVATE_PY = """\
# Run by CCC on an Arm as it is admitted (the activation job). The vendor's
# activate_vlearn.sh is this line; creating a gym is what makes TurboActivate write this machine's
# activation into the Licence directory, which is bound at this Dive's home.
import vlearn

vlearn.create_gym(with_render=False)
print("vlearn activated on this Arm")
"""

VLEARN = Profile(
    name="vlearn",
    summary="Vsim/vlearn — GPU physics + RL, licensed per machine (TurboActivate).",
    python="3.11",
    python_why="the only versions vlearn ships a Linux wheel for, with 3.10. An Environment now "
               "carries its own interpreter, so any manifest can ask for it — but it must be asked "
               "for, since a binary wheel cannot be built for a version it was not compiled against",
    # vlearn itself, which is why a submitter must never install it: the Arm holds the build its own
    # licence covers, and CCC puts that on PYTHONPATH. A declared vlearn is replaced by an empty stub
    # before the Environment is built (environments.substitute_supplied) and deleted again on the Arm
    # (VLEARN_SETUP above).
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
        # `apptainer --nv`; only the runtime comes from here.)
        ("cuda-cudart=13", "libcudart.so.13, which libvsimdynamics.so needs and neither a uv "
                           "Environment nor a torch-free conda one has any other source for"),
    ),
    # The CUDA major is a property of the ARM's vlearn build, not of the cluster — and it is pinned
    # here, on the Head, for every Arm at once. An Arm whose wheel was built against CUDA 12 failed on
    # `OSError: libcudart.so.12: cannot open shared object file` with everything else on its
    # LD_LIBRARY_PATH supplying 13, and nothing in CCC could tell it apart from an Arm needing 13.
    #
    # Both are shipped rather than detected: they cost one extra conda solve per Dive and cannot
    # collide (see `alt_libs`), where detection would need the major recorded per Arm at enroll, a
    # Head-side render that varies by placement, and a migration for every Arm already enrolled.
    alt_libs=(
        ("cuda12", (("cuda-cudart=12", "libcudart.so.12, for an Arm whose vlearn was built against "
                                       "CUDA 12 — the majors are not compatible and the SONAME is "
                                       "what the loader matches"),)),
    ),
    setup=VLEARN_SETUP,
    activation=VLEARN_ACTIVATION,
    activation_script=VLEARN_ACTIVATE_PY,
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

    A profiled Feature attaches its software to the Dive's **Environment**: PREAMBLE probes `$py` for
    the ABI to match, the nvidia lib dirs to put on LD_LIBRARY_PATH, and where site-packages is. So one
    requested by a Dive that declared no Environment has nothing to attach to, and cannot work.

    Sorted, so a refusal names them the same way however the User typed the request.
    """
    return sorted({f for f in features if f in PROFILES})


# The preamble every profiled Dive gets, once, before the individual blocks. Rendered only when at
# least one requested Feature HAS a profile — an ordinary Dive's script doesn't contain it at all.
#
# The probe is one container launch asking this Dive's own interpreter three questions rather than assuming
# any of them: which ABI tag a Feature's binary package must match, where the nvidia wheels in that
# Environment put their shared libraries, and where its site-packages is. Runs on the ARM, where the
# Environment has landed and where the Feature's package lives — both halves of the comparison are here.
PREAMBLE = r"""
# Arm **Features** were requested, so CCC arranges each one's runtime here -- package, binds, working
# directory, cache and env vars -- and the submitter's script contains none of it. See
# ccc_head_api.features: LD_LIBRARY_PATH is read by the loader at exec, so nothing running INSIDE the
# container can set it for itself.
feature_fail() {
    echo "ccc: $1" >> "$err"
    echo "ccc: this Dive never started." >> "$err"
    echo 1 > "$exitf"
    kill "$loop_pid" 2>/dev/null   # remote branch only; unset is a harmless no-op on the Head's
    stage_out
    exit 1
}
# Checked BEFORE the probe, because `$py` is unquoted below (it has to be — an empty interpreter must
# vanish rather than become an empty argument). With no Environment declared it expands to NOTHING, so
# apptainer takes the next word as the command and answers `FATAL: "-c": executable file not found in
# $PATH` — an error about a flag, logged above the one line that explains what actually went wrong.
# `profiled()` is the submit-time gate meant to refuse this (a profiled Feature needs an Environment to
# attach to), so reaching here means that gate was bypassed rather than that a User did something ordinary.
[ -n "$py" ] || feature_fail \
  "could not run this Dive's interpreter () to identify it, which every Feature below needs. The container image supplies none, so this is a Dive that declared no Environment -- a Dive opened without --manifest is the one way to get here. Declare one and the Features below have an interpreter to attach to."
feature_probe=$(apptainer exec --contain --bind "$proj$envbind" "$sif" $py -c '
import glob, os, sys, sysconfig
print("cp%d%d" % sys.version_info[:2])
print(":".join(sorted(glob.glob(os.path.join(sysconfig.get_paths()["purelib"], "nvidia", "*", "lib")))))
print(sysconfig.get_paths()["purelib"])
' 2>>"$err")
feature_abi=$(printf %s "$feature_probe" | sed -n 1p)
feature_nvlibs=$(printf %s "$feature_probe" | sed -n 2p)
# container-side, so it starts with $envmount; a profile that needs the ARM-side path rewrites the prefix.
feature_purelib=$(printf %s "$feature_probe" | sed -n 3p)
# A DIFFERENT failure from the one above, now that an absent interpreter is caught before the probe:
# `$py` exists and ran, but said nothing useful. That is the declared Environment's build to look at.
[ -n "$feature_abi" ] || feature_fail \
  "this Dive's interpreter ($py) ran but could not be identified, which every Feature below needs. Its Environment build is the place to look -- the output above is the whole story."
"""


def setup_snippet(features: list[str], activation: bool = False) -> str:
    """The bash CCC splices into the job script for this Dive's requested Features, or `""`.

    Empty for most Dives, and for one that asked only for Features with no profile (`isaaclab`) —
    those need nothing beyond `$<NAME>_PATH`, which the Arm already exports, and the placement Slurm
    already did.

    Ordered rather than caller-order, so the same request renders the same script regardless of how
    the User typed it.

    `activation` appends each profile's activation block, where the Arm's licence *key* is bound. No
    caller since 2.0 dropped the activation call (rebuilt in phase 2), and never anything a submitted
    Dive could request — a User able to set this flag could read every Arm owner's key.
    """
    blocks = [PROFILES[n].setup + (PROFILES[n].activation if activation else "")
              for n in sorted(set(features)) if n in PROFILES]
    if not blocks:
        return ""
    return (PREAMBLE + "".join(blocks)).replace("%%FEATURE_MOUNT%%", FEATURE_MOUNT) \
                                       .replace("%%FEATLIB_MOUNT%%", FEATLIB_MOUNT) \
                                       .replace("%%VLWD_MOUNT%%", VLWD_MOUNT)


# Which Feature supplies a package the submitter must therefore not install: `{package: feature}`.
# Built from the profiles rather than written out, so adding a package to a Profile's `supplies` is the
# only edit needed. Names are lowercased — a dependency may be spelled any way PEP 503 allows.
SUPPLIED_BY: dict[str, str] = {
    pkg.lower(): p.name for p in PROFILES.values() for pkg in p.supplies
}


def lib_specs(features: list[str]) -> dict[str, list[str]]:
    """The conda specs each requested Feature needs built, `{feature: [spec, ...]}`, skipping those
    with none. What `environments.build_feature_libs` turns into a prefix at FEATLIB_MOUNT.

    Ordered by Feature name so the same request builds the same prefix — a solve that varies with
    argument order would make two identical Dives ship different bytes.
    """
    out: dict[str, list[str]] = {}
    for n in sorted(set(features)):
        p = PROFILES.get(n)
        if not p:
            continue
        if p.libs:
            out[n] = [s for s, _why in p.libs]
        # After its Feature's own prefix, so the build order a Dive watches reads as one Feature's
        # libraries then its alternates, rather than interleaving two Features.
        for suffix, extra in p.alt_libs:
            out[f"{n}-{suffix}"] = [s for s, _why in extra]
    return out


def lib_prefix_label(key: str) -> str:
    """What one `lib_specs` key means in prose, for the line a Dive's log shows while it builds.

    A key is usually a Feature name, but an `alt_libs` prefix is not a Feature and saying so would
    invent one — the Dashboard would then show a Dive building "the 'vlearn-cuda12' Arm Feature".
    """
    if key in PROFILES:
        return f"the '{key}' Arm Feature's system libraries"
    for n, p in PROFILES.items():
        for suffix, _extra in p.alt_libs:
            if key == f"{n}-{suffix}":
                return f"the '{n}' Arm Feature's alternate system libraries ({suffix})"
    return f"system libraries for {key}"


# The activation Dive's whole manifest: an interpreter of the right version and nothing else.
#
# It used to be the same starter `environment.yml` a submitter was handed, carrying the full system
# library list — because back then that file was the ONLY way to get a Python vlearn ships a wheel for,
# and the libraries had nowhere else to live. Both halves of that have gone: an Environment carries its
# own interpreter whatever its manifest, and the libraries are CCC's own prefix now (`lib_specs`),
# built for this Dive because it asks for the Feature like any other. So what is left to declare is
# the version, which is a real requirement — a binary wheel cannot be built for an ABI it was not
# compiled against, and this is the one Dive CCC writes the manifest for.
#
# `package = false` because there is no package here to install: two loose files, no build backend.
ACTIVATION_PYPROJECT = """\
# Written by CCC to activate the '{name}' Arm Feature — not a file anyone edits.
[project]
name = "ccc-{name}-activation"
version = "0"
# {why}
requires-python = "=={python}.*"
dependencies = []

[tool.uv]
package = false
"""

# Only reached by a profile with an activation script but no pinned Python — none today, and a Feature
# whose package is not a binary wheel would legitimately not care which interpreter it activates under.
DEFAULT_ACTIVATION_PYTHON = "3.12"


def activation_project(name: str) -> dict[str, str]:
    """The whole project an activation Dive uploads for `name`: `{filename: contents}`.

    Two files, no repository. Deliberately excludes the Feature's own package: what a Dive imports
    comes from the Arm's unpacked wheel (the copy the licence covers), and installing a second from an
    index would activate the wrong build.

    Raises KeyError for a Feature with no activation — most don't; an Arm declaring one is simply
    admitted without this step.
    """
    p = PROFILES[name]
    if not p.activation_script:
        raise KeyError(name)
    return {
        "pyproject.toml": ACTIVATION_PYPROJECT.format(
            name=p.name, python=p.python or DEFAULT_ACTIVATION_PYTHON, why=p.python_why),
        "activate.py": p.activation_script,
    }


def needs_activation(features: list[str]) -> list[str]:
    """Which of an Arm's declared Features CCC has to activate before the Arm can serve them."""
    return [n for n in sorted(set(features))
            if n in PROFILES and PROFILES[n].activation_script]


def runtime_summary(name: str) -> str:
    """What CCC arranges for a Feature, in prose — served by `GET /features/{name}/runtime` and printed
    by `cuttle feature show <name>`.

    This used to emit a starter `environment.yml` for the submitter to copy into their project, because
    the system libraries below had to be in THEIR manifest or nothing would load them. CCC builds them
    itself now (`lib_specs`, `environments.build_feature_libs`), so copying this list into a project
    would only install a second copy of it — hence a description rather than a file.

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
