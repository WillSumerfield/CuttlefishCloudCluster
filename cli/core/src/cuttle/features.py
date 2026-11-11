"""What an Arm must have on disk for a **Feature** it declares, and what CCC prepares from it.

The Arm's half of temp/FEATURES_BUILTIN.md; `ccc_head_api.features` holds the Head half (the bash a
Dive's job script runs). Head knows what a Feature's runtime *needs*, this knows what the box *has*.

Split is forced: `cuttle setup --arm` asks these questions before the box has enrolled, i.e. before
it can reach a Head — so everything here must be answerable offline.

Validation happens at the prompt, not at the Dive — a missing path is re-asked immediately, naming
what's absent, rather than surfacing hours later as a mystery failure in someone else's payload.

Preparation happens once at setup, not per Dive: wheels + runtime data are copied into CCC-owned
state (`/var/lib/ccc/features/<name>/`), and a Dive's container binds those read-only — never the owner's
directory, since a container runs as a **Guise** with no relationship to the donor and can't traverse a
0750 home. See `mirror_install`.
"""

import json
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config, ui
from .system import run


@dataclass(frozen=True)
class ArmProfile:
    """What one Feature needs from the box that declares it.

    `requires`: relative path -> sentence describing it, so a re-ask names the missing thing in the
    vendor's own words. `wheels`: glob for binary wheels to unpack (empty if none). `env`: extra
    `export` lines this Feature contributes to /etc/ccc/features.env beyond the standard `<NAME>_PATH`.
    `licence_key`: default filename of the product key this Feature was activated with, if any —
    mirrored into CCC's own state (mirror_licence_key), never leaves the Arm, no other Dive sees it.
    """

    name: str
    requires: dict[str, str] = field(default_factory=dict)
    wheels: str = ""
    licence_key: str = ""
    licence_key_help: str = ""
    # System-library conda specs CCC builds into this Feature's state tree at setup (build_libs). SPECS
    # ONLY — a faithful mirror of ccc_head_api.features.Profile.libs/alt_libs (the "why" notes stay
    # Head-side). `libs` is the main prefix; each `alt_libs` entry `(suffix, specs)` a second prefix for
    # a spec that conflicts with it (a second CUDA major).
    libs: tuple[str, ...] = ()
    alt_libs: tuple[tuple[str, tuple[str, ...]], ...] = ()


# vlearn is distributed as a checkout, not installed: a directory holding the vendor's wheels beside the
# data their runtime resolves at load time. All five entries below are things vlearn itself opens, and a
# missing one is a failure inside libgym.so rather than anything CCC could report usefully later.
VLEARN = ArmProfile(
    name="vlearn",
    requires={
        "assets": "the asset library vlearn loads scenes from (~1 GB)",
        "shaders": "the compiled shader directory",
        "cache": "the asset cache vlearn seeds a run from",
        "TurboActivate.dat": "the licensing descriptor that pairs with this machine's activation",
    },
    # Linux only, and every ABI this checkout ships: an Arm can serve Dives on any Python it holds a
    # wheel for, and which one a given Dive needs is not known until that Dive arrives.
    wheels="vlearn-*-linux_x86_64.whl",
    # A box with vlearn installed was activated by whoever installed it, so it already has this — the
    # vendor's README says to put it at the checkout root, which is the default offered.
    licence_key="License.key",
    licence_key_help="the product key vlearn was activated with. CCC copies it into this box's own "
                     "CCC state, readable by the cluster's accounts and nobody else, and reads it "
                     "once — from the activation CCC runs as this Arm joins. It never leaves the box.",
    # libgym.so links all of these dynamically, resolving at LOAD time (missing one is an OSError on
    # import, even headless). No Python manifest can carry them and uv cannot install libassimp.so.5, so
    # CCC builds them into a conda prefix here (build_libs). Both CUDA majors ship (alt_libs): which one
    # this Arm's wheel wants is a property of the build, and distinct SONAMEs mean the loader takes
    # whichever the binary names. MUST agree with ccc_head_api.features.VLEARN.libs/alt_libs (specs
    # only) — the two never import each other (same split as FEATURE_NAMES), and a spec added there but
    # not here is an ImportError in a Dive on this Arm.
    libs=("sdl2", "assimp=5", "zeromq", "czmq", "libglu", "mesalib", "cuda-cudart=13"),
    alt_libs=(("cuda12", ("cuda-cudart=12",)),),
)

ARM_PROFILES: dict[str, ArmProfile] = {VLEARN.name: VLEARN}

# One tree per Feature, under the same /var/lib/ccc the rest of an Arm's CCC state lives in.
FEATURE_PKG_DIR = config.CCC_LIB / "features"

# The Feature's data, mirrored off the owner's install. Root-owned and world-readable: it is a vendor's
# redistributable runtime, which is not secret, and every **Guise** on this box has to be able to read
# it. See mirror_install for why it is a copy and not a bind of where the owner put it.
INSTALL_DIRNAME = "install"

# The **Licence** key, mirrored the same way but NOT world-readable. Deliberately not in the Licence
# directory, which the job script binds as `$HOME` into every Dive that asked for the Feature — the key is
# the credential that PRODUCED that directory's contents, and only the activation Dive may see it.
LICENCE_KEY_NAME = "licence.key"

# Wheel filenames encode their ABI as `...-cp311-cp311-linux_x86_64.whl`. That tag is what a Dive's
# job script matches its own interpreter against, so it is the directory name too — the comparison is
# then a `[ -d ]`, with no parsing on the Arm at run time.
_ABI_RE = re.compile(r"-(cp\d{2,3})-\1[-.]")
_CU_RE = re.compile(r"\+cu(\d+)")


def _cu_major(name: str) -> int:
    """The CUDA major a wheel's local version tags (`+cu130` → 130), or -1 if it names none."""
    m = _CU_RE.search(name)
    return int(m.group(1)) if m else -1

# The per-Feature descriptor ccc-launch reads to bind this Feature into a Dive (ccc_launch.py:
# feature_binds_env). JSON of {"binds": [{src, dst, ro}], "env": {k: v}} — src a host path under this
# Feature's state tree, dst a container path under /ccc. Root-written and world-readable: it names
# paths, holds no secret.
DESCRIPTOR_NAME = "profile.json"

# Where a Feature's binds land inside the container. The contract between the descriptor this module
# writes (bind dst), `ccc_launch` which applies it, and `feature_hook` which reads it back — all
# Arm-side, so one constant, imported where it is needed rather than duplicated.
FEATURE_MOUNT = "/ccc/feature"

# The one writable Feature directory inside the container:
# vlearn resolves assets/shaders/cache/TurboActivate.dat against VL_WORKING_DIRECTORY, and cache/ is
# written at run time, so it cannot be a read-only bind. Created + chowned to the User by DIVE_INIT, laid
# out by the in-container feature hook (feature_hook.setup_vlearn).
VLWD_MOUNT = "/ccc/vlwd"

# A Feature's SYSTEM-LIBRARY prefixes: micromamba-built into its state tree at setup (build_libs) and
# bound read-only, the loader resolving a Feature's .so against them. The main conda prefix is "libs";
# each alternate (a conflicting spec, a second CUDA major) is "libs-<suffix>".
LIBS_DIRNAME = "libs"

# Marker CCC drops once it has activated a Feature on this box. Activation is licensed per machine
# (TurboActivate) and wasting one is expensive, so it runs exactly once — `activate` skips a re-run when
# the marker is present. It lives in the **Licence** directory, NOT the per-Feature state tree, because
# `prepare` rebuilds that tree every `setup --arm`: the Licence directory survives a re-setup (and a
# teardown), which is exactly the lifetime of "this machine is activated". Basename stem; the file is
# `.<ACTIVATED_MARKER>-<feature>` so one Licence directory can mark several Features.
ACTIVATED_MARKER = "ccc-activated"

# The vendor's own activation line — creating a gym is what makes TurboActivate write this machine's
# activation into the Licence directory. Headless, because an Arm has no display and rendering is not
# what is licensed. Activation is Arm-local now (this package's `activate`), so this lives only here.
VLEARN_ACTIVATE_PY = "import vlearn\n\nvlearn.create_gym(with_render=False)\nprint('vlearn activated on this Arm')\n"
ACTIVATE_SCRIPTS = {"vlearn": VLEARN_ACTIVATE_PY}


def check_install(name: str, path: str) -> list[str]:
    """What `path` is missing before it can serve `name` — empty when it is good, or when CCC has no
    opinion about this Feature (`isaaclab`, which has no profile).

    Returned rather than raised: the caller is a prompt, and the useful thing to do with the answer is
    print it and ask again, not abort a setup the operator is halfway through.
    """
    p = ArmProfile("") if name not in ARM_PROFILES else ARM_PROFILES[name]
    if not p.name:
        return []
    root = Path(path)
    if not root.is_dir():
        return [f"{path} is not a directory"]
    missing = [f"{rel} — {what}" for rel, what in sorted(p.requires.items())
               if not (root / rel).exists()]
    if p.wheels and not list(root.glob(p.wheels)):
        missing.append(f"{p.wheels} — no Linux wheel for {name} here")
    return missing


def abi_tags(name: str, path: str) -> list[str]:
    """The Python ABI tags this box holds a `name` wheel for, e.g. `["cp310", "cp311"]`."""
    p = ARM_PROFILES.get(name)
    if not p or not p.wheels:
        return []
    tags = {m.group(1) for w in Path(path).glob(p.wheels) if (m := _ABI_RE.search(w.name))}
    return sorted(tags)


def unpack_packages(features: list[dict]) -> dict[str, list[str]]:
    """Unzip every declared Feature's binary wheels into `FEATURE_PKG_DIR/<name>/<abi>`, and report
    what landed as `{feature: [abi, ...]}`.

    Unpacks the whole wheel, not just the importable subdirectory — e.g. vlearn's `vlearn_bindings
    .<abi>.so` sits beside `vlearn/`, and `import vlearn` needs both on PYTHONPATH. Unpacked by CCC
    rather than pointed at the owner's own `.venv`, so PYTHONPATH holds only this project's artefacts,
    not everything else the donor has installed. Rebuilt from scratch each setup so an in-place
    software upgrade can't leave old-release files mixed in under the same ABI tag.
    """
    landed: dict[str, list[str]] = {}
    for f in features:
        p = ARM_PROFILES.get(f["name"])
        if not p or not p.wheels:
            continue
        dest_root = FEATURE_PKG_DIR / p.name
        run(["rm", "-rf", str(dest_root)], sudo=True, check=False)
        # One wheel per ABI: the ABI dir holds a single build (one vlearn_bindings.so on PYTHONPATH),
        # so when a box ships several CUDA variants for the same ABI (vlearn's +cu128/+cu130), take the
        # highest CUDA major — both lib prefixes ship, so either resolves, and merging them would clash.
        chosen: dict[str, Path] = {}
        for whl in sorted(Path(f["path"]).glob(p.wheels)):
            m = _ABI_RE.search(whl.name)
            if not m:
                continue
            abi = m.group(1)
            if abi not in chosen or _cu_major(whl.name) > _cu_major(chosen[abi].name):
                chosen[abi] = whl
        for abi, whl in sorted(chosen.items()):
            dest = dest_root / abi
            run(["install", "-d", "-m", "0755", str(dest)], sudo=True)
            # Extracted to a staging dir this process owns, then moved into place as root: zipfile
            # writes with THIS user's privileges, and /var/lib/ccc is not ours to write into.
            staging = Path("/tmp") / f"ccc-unpack-{p.name}-{abi}"
            run(["rm", "-rf", str(staging)], check=False)
            with zipfile.ZipFile(whl) as z:
                z.extractall(staging)
            run(["cp", "-a", f"{staging}/.", str(dest)], sudo=True)
            run(["chmod", "-R", "a+rX", str(dest)], sudo=True)
            run(["rm", "-rf", str(staging)], check=False)
        if chosen:
            landed[p.name] = sorted(chosen)
    return landed


def _lib_prefixes(p: ArmProfile) -> dict[str, list[str]]:
    """`{dirname: [conda spec, ...]}` for one Feature — the main `libs` prefix and each conflicting
    alternate (`libs-<suffix>`). Empty for a Feature with no system libraries."""
    out: dict[str, list[str]] = {}
    if p.libs:
        out[LIBS_DIRNAME] = list(p.libs)
    for suffix, extra in p.alt_libs:
        out[f"{LIBS_DIRNAME}-{suffix}"] = list(extra)
    return out


def build_libs(features: list[dict]) -> None:
    """micromamba-create each declared Feature's system-library prefix(es) into its state tree.

    Once per `setup --arm`, not per Dive: these are CCC's own conda prefixes (libassimp.so.5, libSDL2,
    libcudart, …) that a submitter's manifest cannot carry and uv cannot install. Built HERE, on the
    Arm that holds the wheel they link into, rather than on the Head per Dive — shipping a half-gigabyte
    prefix from the Head cost what shipping the Environment cost, and it is the same conda-forge solve
    whatever the manifest. Both CUDA majors always (`alt_libs`).

    Rebuilt from scratch each setup — `unpack_packages` already cleared the tree, so an in-place upgrade
    cannot leave a prior release's libraries mixed in.
    """
    mm = str(config.ENV_TOOLS_DIR / "micromamba")
    for f in features:
        p = ARM_PROFILES.get(f["name"])
        if not p:
            continue
        for dirname, specs in _lib_prefixes(p).items():
            prefix = FEATURE_PKG_DIR / p.name / dirname
            run(["rm", "-rf", str(prefix)], sudo=True, check=False)
            run([mm, "create", "--yes", "--quiet", "--prefix", str(prefix),
                 "-c", "conda-forge", *specs], sudo=True)
            run(["chmod", "-R", "a+rX", str(prefix)], sudo=True)


# patchelf, run from the pinned uv (CLAUDE: uv for packages); edits the unpacked wheel's RUNPATH.
PATCHELF = [str(config.ENV_TOOLS_DIR / "uv"), "tool", "run", "patchelf"]


def _is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def rpath_wheel_libs(name: str, abis: list[str]) -> None:
    """Add this Feature's system-library prefixes to each unpacked wheel `.so`'s RUNPATH, named at their
    fixed in-Dive mount (FEATURE_MOUNT) — the path ccc-launch binds them to.

    Then `import` resolves libcudart/libzmq/libstdc++/... from the `.so` itself, so a Dive needs no
    LD_LIBRARY_PATH for the Feature. A Dive-wide LD_LIBRARY_PATH would be read by EVERY process in the
    shell and, because the prefix ships a whole userland (libtinfo, libncurses, libstdc++, libssl, ...),
    would shadow the base system's own and break ordinary tools like tmux. RUNPATH is the `.so`'s own, so
    only what links against it is affected. `--add-rpath` appends, keeping the wheel's `$ORIGIN` entries
    (its intra-wheel deps), and the tree is rebuilt from scratch each setup so nothing is appended twice.
    """
    p = ARM_PROFILES.get(name)
    if not p:
        return
    extra = ":".join(f"{FEATURE_MOUNT}/{name}/{d}/lib" for d in _lib_prefixes(p))
    if not extra:
        return
    root = FEATURE_PKG_DIR / name
    for abi in abis:
        for so in sorted((root / abi).rglob("*.so*")):
            if so.is_file() and not so.is_symlink() and _is_elf(so):
                run([*PATCHELF, "--add-rpath", extra, str(so)], sudo=True)


def prepare(features: list[dict], licence_home: str = "") -> dict[str, list[str]]:
    """Bring everything a declared Feature needs into CCC's own state, and report the ABI tags that
    landed as `{feature: [abi, ...]}`.

    Order matters: `unpack_packages` clears the whole per-Feature tree, so the mirrors go in behind it.

    `licence_home` is this Arm's one Licence directory — recorded in each licensed Feature's descriptor
    so ccc-launch can bind its contents into a Dive's home (where TurboActivate reads the activation).

    Called on EVERY setup, including one that declares nothing — `forget_undeclared` is why. A re-run
    that drops a Feature has to erase the ~1 GB mirror describing it, and a caller that only ran this
    when `features` was non-empty would leave the box advertising a Feature it no longer serves.
    """
    forget_undeclared(features)
    landed = unpack_packages(features)
    build_libs(features)
    for f in features:
        rpath_wheel_libs(f["name"], landed.get(f["name"], []))
        mirror_install(f)
        mirror_licence_key(f)
        write_descriptor(f["name"], landed.get(f["name"], []), licence_home)
    return landed


def forget_undeclared(features: list[dict]) -> None:
    """Drop the mirrored tree of every Feature this node no longer declares (mirror + licence key).

    Shrinking a declaration must be as real as growing one: the registry and Slurm's `Feature=` line
    already follow it, so without this the box keeps a gigabyte of vendor runtime, and a copy of the
    Licence key that produced it, describing something it stopped offering.
    """
    keep = {f["name"] for f in features}
    try:
        stale = [d for d in FEATURE_PKG_DIR.iterdir() if d.name not in keep]
    except OSError:
        return  # nothing was ever mirrored here
    for d in stale:
        run(["rm", "-rf", str(d)], sudo=True, check=False)


def mirror_install(f: dict) -> None:
    """Copy the paths a Feature's runtime opens out of the owner's install and into CCC's own state.

    A container runs as a **Guise** with no relationship to the donor, and a home dir is 0750 on every distro
    — one un-traversable ancestor makes apptainer fail the whole bind as "permission denied", reading
    as a CCC bug. ACLing the ancestors was rejected (would make the owner's whole home traversable by
    every Guise on the cluster); refusing installs under a home was rejected too (donor would have to
    move a gigabyte first). So: ~1 GB copied once per `setup --arm`, rebuilt each time (not synced) so
    an in-place upgrade can't leave old release files mixed into the new one's (see unpack_packages).
    """
    p = ARM_PROFILES.get(f["name"])
    if not p or not p.requires:
        return
    dest = FEATURE_PKG_DIR / p.name / INSTALL_DIRNAME
    run(["rm", "-rf", str(dest)], sudo=True, check=False)
    run(["install", "-d", "-m", "0755", str(dest)], sudo=True)
    src = Path(f["path"])
    for rel in sorted(p.requires):
        run(["cp", "-a", str(src / rel), str(dest / rel)], sudo=True)
    run(["chmod", "-R", "a+rX", str(dest)], sudo=True)


def mirror_licence_key(f: dict) -> None:
    """Copy this Feature's **Licence** key into CCC's state, readable by the cluster's accounts only.

    Same reason as `mirror_install`: the activation Dive is a container, can't reach a key under the owner's
    home. Mode 0640 root:ccc-guises, not the a+rX everything else here gets — real gate is that no
    ordinary Dive ever binds it, but the mode is cheap insurance against a bind mistake. Not stored in
    the Licence directory (the job script binds that as `$HOME` into every Dive that asked for the
    Feature, which would expose the key to exactly the Dives that must never see it).
    """
    p = ARM_PROFILES.get(f["name"])
    if not p or not p.licence_key or not f.get("key"):
        return
    dest = FEATURE_PKG_DIR / p.name / LICENCE_KEY_NAME
    run(["install", "-D", "-m", "0640", "-o", "root", "-g", config.GUISE_GROUP,
         f["key"], str(dest)], sudo=True)


def descriptor_binds(name: str, abis: list[str]) -> list[dict]:
    """The read-only binds a Dive gets for Feature `name` — the unpacked wheel (one per ABI the box
    holds), the mirrored runtime data, and each system-library prefix. Each entry is
    `{src, dst, ro}`, `src` under this Feature's own state tree, `dst` under FEATURE_MOUNT. Never the
    **Licence** key (ccc-launch refuses it too).

    The ABI is picked INSIDE the container, after the Environment's interpreter is known (the feature
    hook, phase 8c) — so every held ABI's wheel dir is bound and the hook chooses; here we only lay
    out what it may choose from. The lib prefixes are both bound for the same reason both CUDA majors
    ship: the loader takes whichever SONAME the binary names.
    """
    p = ARM_PROFILES.get(name)
    if not p:
        return []
    root = FEATURE_PKG_DIR / name
    binds = [{"src": str(root / abi), "dst": f"{FEATURE_MOUNT}/{name}/{abi}", "ro": True}
             for abi in abis]
    if p.requires:   # the mirrored assets/shaders/cache/… the Feature resolves its data against
        binds.append({"src": str(root / INSTALL_DIRNAME),
                      "dst": f"{FEATURE_MOUNT}/{name}/{INSTALL_DIRNAME}", "ro": True})
    for dirname in _lib_prefixes(p):   # the whole prefix, not just lib/: conda's CUDA libs live under
        binds.append({"src": str(root / dirname),   # <prefix>/targets/.../lib and lib/ only symlinks in,
                      "dst": f"{FEATURE_MOUNT}/{name}/{dirname}", "ro": True})   # so lib/ alone dangles
    return binds


def write_descriptor(name: str, abis: list[str], licence_home: str = "") -> None:
    """Write `/var/lib/ccc/features/<name>/profile.json` — what ccc-launch binds for this Feature.

    Skipped for a Feature CCC only places (`isaaclab`: no ArmProfile, no binds). Staged by this
    process then `install`ed as root, like every other write into /var/lib/ccc here.

    `licence_home` is recorded only for a LICENSED Feature with a Licence directory: ccc-launch merges
    that directory's contents into the Dive's home, where TurboActivate (getpwuid->pw_dir) reads the
    per-machine activation. It is the activation STATE, never the key — the key lives in this tree, not
    the Licence directory.
    """
    binds = descriptor_binds(name, abis)
    if not binds:
        return
    p = ARM_PROFILES.get(name)
    desc = {"binds": binds, "env": {}}
    if licence_home and p and p.licence_key:
        desc["licence_home"] = licence_home
    dest = FEATURE_PKG_DIR / name / DESCRIPTOR_NAME
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        json.dump(desc, tmp)
        staging = tmp.name
    run(["install", "-D", "-m", "0644", staging, str(dest)], sudo=True)
    run(["rm", "-f", staging], check=False)


# --- activation (Arm-local, once per machine) -----------------------------------

# The container entry the activation runs. Lays out VL_WORKING_DIRECTORY exactly as the in-container
# Dive hook does (feature_hook.setup_vlearn), makes an ABI-matched interpreter with uv, and runs the
# vendor's activate.py against the unpacked wheel on PYTHONPATH. CPU-only — no GPU is bound and rendering
# is off; TurboActivate fingerprints CPU/MAC/disk, not the card.
_ACTIVATE_ENTRY = """\
set -e
install={feat}/{install}
mkdir -p {vlwd}
ln -sfn "$install/assets" {vlwd}/assets
ln -sfn "$install/shaders" {vlwd}/shaders
ln -sfn "$install/TurboActivate.dat" {vlwd}/TurboActivate.dat
rm -rf {vlwd}/cache
cp -a "$install/cache" {vlwd}/cache 2>/dev/null || mkdir -p {vlwd}/cache
export PYTHONPATH={feat}/{abi}
export LD_LIBRARY_PATH={feat}/{abi}/vlearn/lib:{feat}/{libs}/lib:{feat}/{libs}-cuda12/lib
export VL_WORKING_DIRECTORY={vlwd}
export VL_LICENSE_KEY_PATH={key}
uv venv --python {pyver} /ccc/venv
/ccc/venv/bin/python /ccc/activate.py
"""


def held_abis(name: str) -> list[str]:
    """The Python ABI tags this box has already unpacked a `name` wheel for (the `cp\\d` dirs
    `unpack_packages` made), e.g. `["cp310", "cp311"]`. Read off disk, after `prepare`."""
    root = FEATURE_PKG_DIR / name
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and re.fullmatch(r"cp\d{2,3}", d.name))


def activation_argv(image: str, name: str, abi: str, licence_home: str, activate_py: str,
                    cdi: list[str]) -> list[str]:
    """The `podman run` argv that activates `name` on this box. Pure — builds nothing, runs nothing.

    Like `enroll.bench_locally`: a one-shot container on the base image, with the Feature's own
    read-only state bound under FEATURE_MOUNT (the ABI wheel, the mirrored install, both
    system-library prefixes), plus the mirrored Licence key read-only and `activate_py` to run.

    Gets the box's GPU(s) the same way a Dive does — `--device nvidia.com/gpu=<cdi>` — NOT CPU-only:
    importing vlearn dlopens libgym.so, which links libcuda.so.1 (the driver), so the NVIDIA runtime
    has to inject the driver stack even though activation only fingerprints CPU/MAC/disk.

    The Licence directory is bound at `/root` and the container runs as root: TurboActivate chooses its
    state directory with `getpwuid(getuid())->pw_dir` (ignoring $HOME), so the activation lands in root's
    home — which IS the Licence directory here. `activate` chowns it back to the Licence account
    afterwards, the identity a real Dive activates against (setup.provision_licence_home).
    """
    p = ARM_PROFILES[name]
    root = FEATURE_PKG_DIR / name
    feat = f"{FEATURE_MOUNT}/{name}"
    key = f"/ccc/key/{p.licence_key}"
    pyver = f"{abi[2]}.{abi[3:]}"   # cp311 -> 3.11, cp310 -> 3.10, cp39 -> 3.9
    binds = [
        (root / abi, f"{feat}/{abi}"),
        (root / INSTALL_DIRNAME, f"{feat}/{INSTALL_DIRNAME}"),
        (root / LIBS_DIRNAME, f"{feat}/{LIBS_DIRNAME}"),                     # whole prefix, not lib/:
        (root / f"{LIBS_DIRNAME}-cuda12", f"{feat}/{LIBS_DIRNAME}-cuda12"),   # CUDA libs live in targets/
    ]
    # Networked, unlike bench_locally's --network none: TurboActivate phones the licence server to
    # activate the key, and uv fetches the matching CPython for the venv -- both need egress.
    argv = ["podman", "run", "--rm", "--authfile", str(config.REGISTRY_AUTH)]
    for g in cdi:
        argv += ["--device", f"nvidia.com/gpu={g}"]
    for src, dst in binds:
        argv += ["-v", f"{src}:{dst}:ro"]
    argv += ["-v", f"{root / LICENCE_KEY_NAME}:{key}:ro",
             "-v", f"{activate_py}:/ccc/activate.py:ro",
             "-v", f"{licence_home}:/root", "-e", "HOME=/root"]
    entry = _ACTIVATE_ENTRY.format(feat=feat, install=INSTALL_DIRNAME, vlwd=VLWD_MOUNT,
                                   abi=abi, libs=LIBS_DIRNAME, key=key, pyver=pyver)
    argv += [image, "bash", "-lc", entry]
    return argv


def _activation_cdi() -> list[str]:
    """CDI device names for ALL this box's GPUs, refreshed so Podman can resolve them — the GPU the
    activation container needs (see activation_argv). Reuses the Dive launcher's enumeration so the two
    agree. Empty on a box with no GPU, where activation can't work anyway."""
    from . import ccc_launch as cl
    wsl = cl.WSL_DXG.exists() and not cl._device_minors()
    gpus = cl.gpu_ids([0] if wsl else list(range(len(cl._device_minors()))))
    cdi = cl.cdi_names(gpus, wsl)
    cl.ensure_cdi(cdi)
    return cdi


def activate(features: list[dict], image: str, licence_home: str) -> None:
    """Activate each declared Feature that needs it, Arm-local in Podman, once per machine.

    Run from `setup --arm` after the Licence directory exists. The marker in that directory makes it
    exactly-once (ACTIVATED_MARKER): a re-setup must not spend another TurboActivate seat on hardware
    already activated. Skipped when there is no Licence directory, no key, or no activation script, and
    when the box holds no wheel to import the Feature from.
    """
    if not licence_home:
        return
    for f in features:
        name = f["name"]
        p = ARM_PROFILES.get(name)
        script = ACTIVATE_SCRIPTS.get(name)
        if not p or not script or not p.licence_key or not f.get("key"):
            continue
        marker = Path(licence_home) / f".{ACTIVATED_MARKER}-{name}"
        # Via sudo, not marker.exists(): the Licence directory is 2770 ccc-licence:ccc-guises and the
        # setup user cannot traverse it, so a plain stat raises EACCES instead of answering the question.
        if run(["test", "-e", str(marker)], sudo=True, check=False).returncode == 0:
            ui.result(name, "already activated (delete the marker in the Licence directory to re-run)")
            continue
        abis = held_abis(name)
        if not abis:
            continue
        cdi = _activation_cdi()
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
            tmp.write(script)
            activate_py = tmp.name
        Path(activate_py).chmod(0o644)
        try:
            run(activation_argv(image, name, abis[0], licence_home, activate_py, cdi), sudo=True)
        finally:
            run(["rm", "-f", activate_py], check=False)
        # The activation ran as root, so its files are root-owned; hand them to the Licence account and
        # make them group-writable, as provision_licence_home set the directory itself — a Dive runs as a
        # Guise (a different uid, in GUISE_GROUP) and rewrites them on periodic online re-validation.
        run(["chown", "-R", f"{config.LICENCE_USER}:{config.GUISE_GROUP}", licence_home], sudo=True)
        run(["chmod", "-R", "g+rwX", licence_home], sudo=True)
        run(["touch", str(marker)], sudo=True)
        ui.result(name, "activated")


def install_path(f: dict) -> str:
    """What `$<NAME>_PATH` should say for this Feature — CCC's mirror where there is one, and the path
    the owner declared where there is not (a Feature with no profile, which CCC only places).

    One function so `features.env` and `mirror_install` can never disagree about where the data is.
    """
    p = ARM_PROFILES.get(f["name"])
    return str(FEATURE_PKG_DIR / p.name / INSTALL_DIRNAME) if p and p.requires else f["path"]


def features_env_extras(features: list[dict]) -> list[str]:
    """The extra `export` lines a declared Feature contributes to /etc/ccc/features.env, beyond the
    `<NAME>_PATH` every Feature gets.

    This file is the ONE channel from an Arm's setup to a Dive running on it. The Head renders a
    single job script for the whole fleet and cannot know where a donor put anything, so anything
    machine-specific has to arrive this way — `ccc_head_api.features`' snippets read exactly these
    names.
    """
    lines = []
    for f in features:
        p = ARM_PROFILES.get(f["name"])
        if not p:
            continue
        stem = re.sub(r"[^A-Za-z0-9]", "_", p.name).upper()
        if p.wheels:
            lines.append(f"export {stem}_PKG_ROOT={FEATURE_PKG_DIR / p.name}")
        # The PATH of the key, never the key — and CCC's copy of it, not the owner's original, which a
        # container cannot reach (mirror_licence_key). This file is world-readable; the file it names is not,
        # and it is bound read-only into the activation Dive and nothing else.
        if p.licence_key and f.get("key"):
            lines.append(f"export {stem}_LICENCE_KEY={FEATURE_PKG_DIR / p.name / LICENCE_KEY_NAME}")
            # Source path (no Dive reads it) — features.env is the only durable record of Arm-local
            # state, so a re-setup (setup._head_arm_features) recovers it from here rather than
            # offering CCC's own mirror back as the source and pinning a since-rotated key forever.
            lines.append(f"export {stem}_LICENCE_KEY_SRC={f['key']}")
    return lines


def runtime_summary(name: str) -> str:
    """The prose description of what CCC arranges for `name`, **from the Head**
    (`GET /features/{name}/runtime`) — the one thing here that is not offline.

    The library *specs* are mirrored Arm-side now (`ARM_PROFILES[...].libs`, which `build_libs` turns
    into a prefix at setup), the same must-agree duplication as `FEATURE_NAMES`. But the annotated
    *summary* — the "why is assimp pinned to 5" prose `cuttle feature show` prints — stays Head-side,
    next to the code that builds against these libraries, rather than a second block of prose to keep in
    step.
    """
    from . import api, auth
    client = auth._client()
    client.token = auth._load_token()
    try:
        return (client.raw(f"/features/{name}/runtime") or b"").decode()
    except api.ApiError as e:
        if ui.DEBUG:
            raise
        raise ui.abort(e.user_message()) from None


def report(name: str, path: str) -> None:
    """Say what this box will actually be able to do with `name`, at the moment it is declared.

    The ABI tags are the interesting part and the least discoverable: they decide which Environments
    can ever run here, and a donor whose checkout ships only cp310 wheels should learn that now rather
    than from a submitter's confused bug report.
    """
    tags = abi_tags(name, path)
    if tags:
        ui.say(f"  [muted]{name}: wheels for {', '.join(tags)} — Dives must declare a matching "
               f"Python[/muted]")
