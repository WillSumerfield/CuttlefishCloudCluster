"""Runtime **profiles** for Arm Features (temp/FEATURES_BUILTIN.md) — what CCC does for a Spawn that
asked for one.

A Feature is a name in `cuttle.config.FEATURE_NAMES`. Without a profile it buys two things: placement
(a Slurm `Feature=` token) and `$<NAME>_PATH` in the Spawn's environment — all `isaaclab` gets, and a
complete, useful Feature on its own.

A profile adds the rest — package, binds, working directory, cache, env vars — so the submitter's
script contains **none** of it. Not a convenience: `LD_LIBRARY_PATH` is read by the dynamic loader once,
at `exec`, so a payload cannot set it for itself; whatever arranges it must run *outside* the process,
i.e. the job script, i.e. here.

**Everything a profile emits resolves on the Arm, not here.** The Head renders one script for the whole
fleet and cannot know where any donor installed anything, so snippets name `$VLEARN_PATH` and friends —
variables the Arm exports from `/etc/ccc/features.env`, written by `cuttle setup --arm` (see
`cuttle.features` for the Arm-side half: what it must have on disk and how it prepares it, offline).

**A profile's failures are the Spawn's, and loud.** Every check ends in `feature_fail`, which writes a
sentence naming the Arm and what was wrong into the Spawn's `err`, records exit 1, and stages out — a
mismatch reads as a mismatch, not an `ImportError` a User would blame on their own code.
"""

import textwrap
from dataclasses import dataclass

# Where a profile's Arm-side package is bound inside the Pod. Under /ccc rather than into the
# Environment's own tree: the Environment is bound read-only at one fixed path and belongs to the
# submitter, while this belongs to the Arm — two owners, two mount points, no chance of one silently
# shadowing part of the other.
FEATURE_MOUNT = "/ccc/feature"


@dataclass(frozen=True)
class Profile:
    """One Feature's runtime, and the environment CCC builds when it needs to run that Feature itself.

    `setup` is bash, spliced into the job script after the Environment has been resolved (so `$py`,
    `$envbind` and `$envmount` are set) and before the payload runs. It contributes to two accumulator
    variables, `featbind` and `featenv`, each a leading-comma fragment appended to the `apptainer exec`
    bind and env lists.

    `conda`/`pip` are the system libraries and wheels the Feature's shared objects need — the ONE
    canonical copy. `cuttle feature show <name>` emits a starter `environment.yml` from them, and the
    activation Spawn builds its Environment from the same lists, so a library added for a new release
    fixes both.

    Each `conda`/`pip` entry is `(spec, why)` — the reason travels with the requirement, since "why is
    assimp pinned to 5" is exactly what someone editing a generated `environment.yml` will ask, and
    that file is the only place they'll be looking.
    """

    name: str
    summary: str
    python: str = ""
    python_why: str = ""
    conda: tuple[tuple[str, str], ...] = ()
    pip: tuple[tuple[str, str], ...] = ()
    pip_index: str = ""
    pip_index_why: str = ""
    setup: str = ""
    # Bash appended to `setup` for an ACTIVATION Spawn only (`spawns.submit(activation=True)`), and the
    # payload that Spawn runs. This is the one Spawn allowed to see the Arm's licence *key*, as opposed
    # to the activation state the key produces — see cuttle.enroll.activate_arm.
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
  "this Spawn's Environment is Python $feature_abi, and this Arm's vlearn is built for: $(ls "$VLEARN_PKG_ROOT" 2>/dev/null | tr '\n' ' '). vlearn ships binary wheels only, so the two have to match -- pin that Python in your environment.yml. (No Environment at all means the base image's Python, which vlearn has never shipped a wheel for.)"
# vlearn resolves assets/, shaders/, cache/ and TurboActivate.dat against VL_WORKING_DIRECTORY, which
# is this Spawn's own directory -- so those four names have to exist in it. Three come from the Arm's
# install, and the install always wins: an upload that carried its own assets/ or shaders/ is replaced,
# not merged. They are ~1 GB and ~24 KB of vendor data; nobody uploads the first, and one predictable
# behaviour beats a partial-merge trap.
#
# SYMLINKS, not copies or binds, and that is load-bearing rather than tidy. $proj is what
# recover_scratch() measures against RECOVERY_MAX_BYTES and then rsyncs home for the Recovery Window;
# a real 1 GB assets/ inside it would silently disable recovery for every vlearn Spawn -- precisely the
# bug the Environment's `mv` out of $proj (above) records having already fixed once. `du` does not
# follow symlinks and `rsync -a` copies them as links, so the window still sees the upload.
rm -rf "$proj/assets" "$proj/shaders" "$proj/TurboActivate.dat" "$proj/cache"
ln -s "$VLEARN_PATH/assets" "$proj/assets"
ln -s "$VLEARN_PATH/shaders" "$proj/shaders"
ln -s "$VLEARN_PATH/TurboActivate.dat" "$proj/TurboActivate.dat"
# cache/ is the one of the four vlearn WRITES (compiled assets, content-addressed), so it cannot be a
# symlink into a read-only bind -- that is an EROFS the first time a Spawn meets an asset the Arm's
# owner never opened. Copied per Spawn instead: ~1 MB, warm from whatever they did generate, with no
# shared writers and nothing written back into their tree.
cp -r "$VLEARN_PATH/cache" "$proj/cache" 2>/dev/null || mkdir -p "$proj/cache"
# Only those three paths are bound, never the install root. The vendor's README puts the **Licence**
# key at the checkout root, and an ordinary Spawn has no business seeing it (Phase 3's activation Spawn
# is the one thing that does). Bound at their OWN host paths, so the symlinks just made resolve to the
# same files inside the Pod as outside.
featbind="$featbind,$VLEARN_PATH/assets:$VLEARN_PATH/assets:ro,$VLEARN_PATH/shaders:$VLEARN_PATH/shaders:ro,$VLEARN_PATH/TurboActivate.dat:$VLEARN_PATH/TurboActivate.dat:ro,$vl_pkg:%%FEATURE_MOUNT%%/vlearn:ro"
# The WHOLE unpacked wheel goes on PYTHONPATH, not its `vlearn/` subdirectory: a wheel unpacks to three
# top-level entries -- `vlearn/`, its `.dist-info`, and a sibling `vlearn_bindings.<abi>.so` -- and the
# bindings module is the one `import vlearn` fails without.
#
# LD_LIBRARY_PATH is why this whole block is CCC's job. libgym.so links SDL2, assimp, zeromq, czmq, GLU
# and libcudart dynamically; they resolve at load time, so they are needed even headless. They come
# from the submitter's Environment (see this profile's conda/pip lists) and from the nvidia wheels in
# it, whose lib directories the probe above asked the interpreter itself to name.
vl_ld="%%FEATURE_MOUNT%%/vlearn/vlearn/lib"
[ -n "$envbind" ] && vl_ld="$vl_ld:$envmount/lib"
[ -n "$feature_nvlibs" ] && vl_ld="$vl_ld:$feature_nvlibs"
featenv="$featenv,PYTHONPATH=%%FEATURE_MOUNT%%/vlearn,VL_WORKING_DIRECTORY=$proj,LD_LIBRARY_PATH=$vl_ld"
"""

# The extra an ACTIVATION Spawn gets, and nothing else does. An ordinary Spawn runs against activation
# state already in the Arm's **Licence** directory; the key that PRODUCED that state is the credential
# and stays out of every Pod but this one.
#
# The key never leaves the Arm: `cuttle setup --arm` mirrors the owner's file into that box's CCC state
# at 0640 (cuttle.features.mirror_licence_key — a Pod can't reach a file under someone's home), and
# this binds that copy at its own host path, so it has one name inside the Pod and out.
VLEARN_ACTIVATION = r"""
# --- Arm Feature: vlearn, ACTIVATION ---------------------------------------
[ -n "${VLEARN_LICENCE_KEY:-}" ] || feature_fail \
  "this Arm declared the 'vlearn' Feature but recorded no licence key path, so CCC cannot activate it. Its owner should re-run 'cuttle setup --arm'."
[ -f "$VLEARN_LICENCE_KEY" ] || feature_fail \
  "this Arm's recorded vlearn licence key ($VLEARN_LICENCE_KEY) is not there any more. Its owner should re-run 'cuttle setup --arm'."
# Both, deliberately. VL_LICENSE_KEY_PATH is the documented override, and License.key beside the
# working directory is where the vendor's own instructions put it -- an activation that failed because
# only one of the two was honoured would be a very expensive thing to debug once per donated box.
ln -sf "$VLEARN_LICENCE_KEY" "$proj/License.key"
featbind="$featbind,$VLEARN_LICENCE_KEY:$VLEARN_LICENCE_KEY:ro"
featenv="$featenv,VL_LICENSE_KEY_PATH=$VLEARN_LICENCE_KEY"
"""

# What `activate_vlearn.sh` does, which is the vendor's own answer to "how do I activate this": the
# library activates itself the first time a gym is created. Headless, because an Arm has no display and
# because rendering is not what is being licensed.
VLEARN_ACTIVATE_PY = """\
# Run by CCC on an Arm as it is admitted (cuttle.enroll.activate_arm). The vendor's
# activate_vlearn.sh is this line; creating a gym is what makes TurboActivate write this machine's
# activation into the Licence directory, which is bound at this Spawn's home.
import vlearn

vlearn.create_gym(with_render=False)
print("vlearn activated on this Arm")
"""

VLEARN = Profile(
    name="vlearn",
    summary="Vsim/vlearn — GPU physics + RL, licensed per machine (TurboActivate).",
    python="3.11",
    python_why="the only versions vlearn ships a Linux wheel for, with 3.10 — and base.sif is 3.12, "
               "which is why this file has to be conda (its own interpreter) and not uv (which "
               "inherits the image's)",
    # libgym.so links all of these dynamically, resolving at LOAD time — required even headless
    # (missing one is an OSError on import, not a render error). conda-forge not apt: base.sif is a
    # pinned, cluster-wide image with no per-Arm hook, so baking one Feature's deps in would change
    # the image for every Arm.
    conda=(
        ("sdl2", ""),
        ("assimp=5", "conda-forge defaults to 6 (libassimp.so.6); libgym.so wants libassimp.so.5"),
        ("zeromq", ""),
        ("czmq", ""),
        ("libglu", ""),
        ("mesalib", ""),
    ),
    # Named explicitly rather than left to torch: a Spawn that doesn't need torch shouldn't carry 3 GB
    # for one shared object, and the activation Spawn needs only libcudart. (libcuda.so.1 is the host
    # driver's, injected by `apptainer --nv`.)
    # NOT `nvidia-cuda-runtime-cu13`: NVIDIA dropped the `-cuXX` suffix at CUDA 13; that name is a
    # reserved 0.0.1 PyPI placeholder with no wheels, so pip falls back to its sdist and fails to build.
    # The unsuffixed name carries the CUDA major in its version instead, hence the pin.
    pip=(("nvidia-cuda-runtime>=13,<14",
          "libcudart.so.13, which libvsimdynamics.so needs and a bare conda env has no other source "
          "for"),),
    pip_index="https://download.pytorch.org/whl/cu130",
    pip_index_why="cu130, to match base.sif's CUDA and vlearn's own build",
    setup=VLEARN_SETUP,
    activation=VLEARN_ACTIVATION,
    activation_script=VLEARN_ACTIVATE_PY,
    notes=(
        "assets/, shaders/, cache/ and TurboActivate.dat come from the Arm — do not upload your own.",
        "Do not add vlearn itself here. The Arm supplies the build its own licence covers, and CCC "
        "puts it on this Spawn's PYTHONPATH.",
    ),
)

# --- isaaclab -----------------------------------------------------------------------------------
# No profile. An Arm declaring it gets placement and $ISAACLAB_PATH — a complete answer for software
# a submitter drives directly.
PROFILES: dict[str, Profile] = {VLEARN.name: VLEARN}


# The preamble every profiled Spawn gets, once, before the individual blocks. Rendered only when at
# least one requested Feature HAS a profile — an ordinary Spawn's script doesn't contain it at all.
#
# The probe is one Pod launch asking this Spawn's own interpreter two questions rather than assuming
# either: which ABI tag a Feature's binary package must match, and where the nvidia wheels in that
# Environment put their shared libraries. Runs on the ARM, where the Environment has landed and where
# the Feature's package lives — both halves of the comparison are here.
PREAMBLE = r"""
# Arm **Features** were requested, so CCC arranges each one's runtime here -- package, binds, working
# directory, cache and env vars -- and the submitter's script contains none of it. See
# ccc_head_api.features: LD_LIBRARY_PATH is read by the loader at exec, so nothing running INSIDE the
# Pod can set it for itself.
feature_fail() {
    echo "ccc: $1" >> "$err"
    echo "ccc: this Spawn never started." >> "$err"
    echo 1 > "$exitf"
    kill "$loop_pid" 2>/dev/null   # remote branch only; unset is a harmless no-op on the Head's
    stage_out
    exit 1
}
feature_probe=$(apptainer exec --contain $dxg --bind "$proj$envbind" "$sif" $py -c '
import glob, os, sys, sysconfig
print("cp%d%d" % sys.version_info[:2])
print(":".join(sorted(glob.glob(os.path.join(sysconfig.get_paths()["purelib"], "nvidia", "*", "lib")))))
' 2>>"$err")
feature_abi=$(printf %s "$feature_probe" | sed -n 1p)
feature_nvlibs=$(printf %s "$feature_probe" | sed -n 2p)
[ -n "$feature_abi" ] || feature_fail \
  "could not run this Spawn's interpreter ($py) to identify it, which every Feature below needs. If this Spawn declared an Environment, its build is the place to look."
"""


def setup_snippet(features: list[str], activation: bool = False) -> str:
    """The bash CCC splices into the job script for this Spawn's requested Features, or `""`.

    Empty for most Spawns, and for one that asked only for Features with no profile (`isaaclab`) —
    those need nothing beyond `$<NAME>_PATH`, which the Arm already exports, and the placement Slurm
    already did.

    Ordered rather than caller-order, so the same request renders the same script regardless of how
    the User typed it.

    `activation` appends each profile's activation block, where the Arm's licence *key* is bound. Set
    by exactly one caller (`cuttle.enroll.activate_arm`, via the Head-local pinned submit), never by
    anything a submitted Spawn could request — a User able to set this flag could read every Arm
    owner's key.
    """
    blocks = [PROFILES[n].setup + (PROFILES[n].activation if activation else "")
              for n in sorted(set(features)) if n in PROFILES]
    if not blocks:
        return ""
    return (PREAMBLE + "".join(blocks)).replace("%%FEATURE_MOUNT%%", FEATURE_MOUNT)


def activation_project(name: str) -> dict[str, str]:
    """The whole project an activation Spawn uploads for `name`: `{filename: contents}`.

    Two files, no repository. The Environment comes from the same profile every submitter's starter
    `environment.yml` does, so a release adding a system library reaches both at once. Deliberately
    excludes vlearn itself: the package a Spawn imports comes from the Arm's own unpacked wheel (the
    copy the licence covers), and installing a second from an index would activate the wrong build.

    Raises KeyError for a Feature with no activation — most don't; an Arm declaring one is simply
    admitted without this step.
    """
    p = PROFILES[name]
    if not p.activation_script:
        raise KeyError(name)
    return {"environment.yml": environment_yml(name), "activate.py": p.activation_script}


def needs_activation(features: list[str]) -> list[str]:
    """Which of an Arm's declared Features CCC has to activate before the Arm can serve them."""
    return [n for n in sorted(set(features))
            if n in PROFILES and PROFILES[n].activation_script]


def environment_yml(name: str) -> str:
    """A starter `environment.yml` for a Feature, from the profile's one canonical library list.

    Served by `GET /features/{name}/environment.yml` and printed by `cuttle feature show <name>`, so
    there is exactly one copy of "what does this software link against" in the codebase and it is this
    one. A submitter adds their own dependencies to what comes out; the point is that they never have
    to discover the system libraries by watching `import` fail.

    Raises KeyError for a Feature with no profile — the caller turns that into a 404, since "this name
    has no runtime to describe" is a real and permanent answer, not an error.
    """
    p = PROFILES[name]

    def spec(indent: str, s: str, why: str) -> list[str]:
        """One dependency line, with its reason inline if it fits and above it if it does not — a
        wrapped trailing comment is not a thing YAML has, and a 200-column line is not a thing anyone
        reads."""
        line = f"{indent}- {s}"
        if not why:
            return [line]
        if len(line) + len(why) + 5 <= 99:
            return [f"{line}   # {why}"]
        return [f"{indent}# {w}" for w in textwrap.wrap(why, 96 - len(indent))] + [line]

    out = [f"# Starter Environment for the '{p.name}' Arm Feature — `cuttle feature show {p.name}`.",
           f"# {p.summary}"]
    for n in p.notes:
        out += ["#"] + [f"# {w}" for w in textwrap.wrap(n, 96)]
    out += [f"name: {p.name}", "channels:", "  - conda-forge", "dependencies:"]
    if p.python:
        out += spec("  ", f"python={p.python}", p.python_why)
    out.append("  - pip")
    out.append("")
    out.append(f"  # What {p.name}'s shared objects link against. They resolve at LOAD time, so a")
    out.append("  # missing one is an ImportError rather than a missing-feature error at run time.")
    for s, why in p.conda:
        out += spec("  ", s, why)
    if p.pip:
        out.append("")
        out.append("  - pip:")
        if p.pip_index:
            out += spec("      ", f"--extra-index-url {p.pip_index}", p.pip_index_why)
        for s, why in p.pip:
            out += spec("      ", s, why)
    out.append("")
    out.append("  # Your own dependencies go below, in whichever of the two lists they belong to.")
    return "\n".join(out) + "\n"
