"""What an Arm must have on disk for a **Feature** it declares, and what CCC prepares from it.

The Arm's half of temp/FEATURES_BUILTIN.md. `ccc_head_api.features` holds the other half — the bash a
Spawn's job script runs — and the two never overlap, because they answer different questions about the
same name: the Head knows what a Feature's runtime *needs*, this knows what this box *has*.

The split is forced rather than stylistic. `cuttle setup --arm` asks these questions BEFORE the box has
enrolled, which is before it can reach a Head at all (see setup.setup_arm — the declaration rides the
same POST as the hardware report). So everything below has to be answerable offline, and everything the
Head contributes has to arrive later, in the rendered job script.

**Validation happens at the prompt, not at the Spawn.** An install path that is missing what the Feature
needs is re-asked immediately, naming what was absent. Left to run time it would surface hours later as
a Spawn dying in someone else's payload, on a box its owner is not watching, with an error that reads
like a CCC bug.

**Preparation happens once, at setup, not per Spawn.** Binary wheels are unpacked into a CCC-owned
directory, and the data the Feature's runtime opens is mirrored into one beside it. A Spawn's Pod binds
those read-only; the copy that runs is unambiguously the one this machine's licence covers.

**Everything a Spawn touches is CCC's copy, not the owner's directory.** A Pod runs as a **Guise**,
which has no relationship to the donor and cannot traverse a 0750 home — so wherever the owner keeps
their install, what a Spawn is handed is `/var/lib/ccc/features/<name>/`. That makes every Arm's bind
paths identical and keeps CCC out of anyone's home directory permissions. See `mirror_install`.
"""

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config, ui
from .system import run


@dataclass(frozen=True)
class ArmProfile:
    """What one Feature needs from the box that declares it.

    `requires` is checked against the install path the owner gives; each entry is a relative path and
    a sentence saying what it is, so a re-ask can name the missing thing in the vendor's own words
    rather than as a bare path.

    `wheels` is a glob for binary wheels to unpack (empty for a Feature that ships none). `env` names
    the extra `export` lines this Feature contributes to /etc/ccc/features.env beyond the standard
    `<NAME>_PATH` — the channel by which everything Arm-local reaches a Spawn.

    `licence_key` is the default filename of the product key this Feature's software was activated
    with, if it has one. `setup --arm` asks where it is; CCC mirrors it into this box's own state
    (mirror_licence_key) and the activation Spawn binds that copy read-only. It never leaves the Arm
    and no other Spawn ever sees it.
    """

    name: str
    requires: dict[str, str] = field(default_factory=dict)
    wheels: str = ""
    licence_key: str = ""
    licence_key_help: str = ""


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
    # Linux only, and every ABI this checkout ships: an Arm can serve Spawns on any Python it holds a
    # wheel for, and which one a given Spawn needs is not known until that Spawn arrives.
    wheels="vlearn-*-linux_x86_64.whl",
    # A box with vlearn installed was activated by whoever installed it, so it already has this — the
    # vendor's README says to put it at the checkout root, which is the default offered.
    licence_key="License.key",
    licence_key_help="the product key vlearn was activated with. CCC copies it into this box's own "
                     "CCC state, readable by the cluster's accounts and nobody else, and reads it "
                     "once — from the activation CCC runs as this Arm joins. It never leaves the box.",
)

ARM_PROFILES: dict[str, ArmProfile] = {VLEARN.name: VLEARN}

# One tree per Feature, under the same /var/lib/ccc the rest of an Arm's CCC state lives in.
FEATURE_PKG_DIR = config.CCC_LIB / "features"

# The Feature's data, mirrored off the owner's install. Root-owned and world-readable: it is a vendor's
# redistributable runtime, which is not secret, and every **Guise** on this box has to be able to read
# it. See mirror_install for why it is a copy and not a bind of where the owner put it.
INSTALL_DIRNAME = "install"

# The **Licence** key, mirrored the same way but NOT world-readable. Deliberately not in the Licence
# directory, which JOB_SCRIPT binds as `$HOME` into every Spawn that asked for the Feature — the key is
# the credential that PRODUCED that directory's contents, and only the activation Spawn may see it.
LICENCE_KEY_NAME = "licence.key"

# Wheel filenames encode their ABI as `...-cp311-cp311-linux_x86_64.whl`. That tag is what a Spawn's
# job script matches its own interpreter against, so it is the directory name too — the comparison is
# then a `[ -d ]`, with no parsing on the Arm at run time.
_ABI_RE = re.compile(r"-(cp\d{2,3})-\1[-.]")


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

    **The whole wheel, not its importable subdirectory.** A wheel unpacks to several top-level entries
    — for vlearn that is `vlearn/`, its `.dist-info` and a sibling `vlearn_bindings.<abi>.so` — and the
    extension module beside the package is the one `import vlearn` fails without. So the unpack
    directory as a whole is what goes on a Spawn's PYTHONPATH.

    **Unpacked by CCC rather than pointed at whatever the owner installed.** PYTHONPATH aimed at a
    donor's own `.venv` would put every package they happen to have in front of the submitter's
    Environment; a directory built from one wheel holds one project's artefacts.

    Rebuilt from scratch each setup: a donor who upgraded the software in place would otherwise leave
    the previous release's files sitting under the same ABI tag, mixed with the new one's, and every
    Spawn would import the result.
    """
    landed: dict[str, list[str]] = {}
    for f in features:
        p = ARM_PROFILES.get(f["name"])
        if not p or not p.wheels:
            continue
        dest_root = FEATURE_PKG_DIR / p.name
        run(["rm", "-rf", str(dest_root)], sudo=True, check=False)
        tags = []
        for whl in sorted(Path(f["path"]).glob(p.wheels)):
            m = _ABI_RE.search(whl.name)
            if not m:
                continue
            dest = dest_root / m.group(1)
            run(["install", "-d", "-m", "0755", str(dest)], sudo=True)
            # Extracted to a staging dir this process owns, then moved into place as root: zipfile
            # writes with THIS user's privileges, and /var/lib/ccc is not ours to write into.
            staging = Path("/tmp") / f"ccc-unpack-{p.name}-{m.group(1)}"
            run(["rm", "-rf", str(staging)], check=False)
            with zipfile.ZipFile(whl) as z:
                z.extractall(staging)
            run(["cp", "-a", f"{staging}/.", str(dest)], sudo=True)
            run(["chmod", "-R", "a+rX", str(dest)], sudo=True)
            run(["rm", "-rf", str(staging)], check=False)
            tags.append(m.group(1))
        if tags:
            landed[p.name] = tags
    return landed


def prepare(features: list[dict]) -> dict[str, list[str]]:
    """Bring everything a declared Feature needs into CCC's own state, and report the ABI tags that
    landed as `{feature: [abi, ...]}`.

    Order matters: `unpack_packages` clears the whole per-Feature tree, so the mirrors go in behind it.
    """
    landed = unpack_packages(features)
    for f in features:
        mirror_install(f)
        mirror_licence_key(f)
    return landed


def mirror_install(f: dict) -> None:
    """Copy the paths a Feature's runtime opens out of the owner's install and into CCC's own state.

    **Because a Spawn cannot read the owner's home.** The Pod runs as a **Guise**, an account with no
    relationship to the donor, and a home directory is 0750 on every distro that ships one. Everything
    inside the checkout is usually world-readable; the home above it is not, and one un-traversable
    ancestor is enough — apptainer fails the whole container as "permission denied" on the bind, which
    reads as a CCC bug and is really a fact about where someone keeps their files.

    The alternatives were worse. ACLing the ancestors would make the owner's entire home traversable by
    every Guise on the cluster — on the Head, that is the box holding the Mantle's key and every User's
    work. Refusing an install path under a home would make a donor move a gigabyte before they can join.

    So: ~1 GB copied once per `setup --arm`, and every Arm's bind paths become the same paths. Rebuilt
    each setup rather than synced, for the same reason the wheels are (see unpack_packages) — a donor
    who upgraded in place must not leave the old release's assets mixed into the new one's.
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

    Same reason as `mirror_install` — the activation Spawn is a Pod, and it cannot reach a key sitting
    under the owner's home. Mode 0640 root:ccc-guises rather than the a+rX everything else here gets:
    the key is a credential, and while `--contain` means the real gate is that no ordinary Spawn ever
    BINDS it, a file mode costs nothing and is the one protection that survives a mistake in the bind.

    Not in the Licence directory, which would have been the obvious home for it: JOB_SCRIPT binds that
    as `$HOME` into every Spawn that asked for the Feature, so the key would be visible to exactly the
    Spawns it must never be visible to.
    """
    p = ARM_PROFILES.get(f["name"])
    if not p or not p.licence_key or not f.get("key"):
        return
    dest = FEATURE_PKG_DIR / p.name / LICENCE_KEY_NAME
    run(["install", "-D", "-m", "0640", "-o", "root", "-g", config.GUISE_GROUP,
         f["key"], str(dest)], sudo=True)


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

    This file is the ONE channel from an Arm's setup to a Spawn running on it. The Head renders a
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
        # Pod cannot reach (mirror_licence_key). This file is world-readable; the file it names is not,
        # and it is bound read-only into the activation Spawn and nothing else.
        if p.licence_key and f.get("key"):
            lines.append(f"export {stem}_LICENCE_KEY={FEATURE_PKG_DIR / p.name / LICENCE_KEY_NAME}")
            # Where the copy CAME from, which no Spawn reads. It is here because features.env is this
            # box's only durable record of anything Arm-local, and the Head's own Arm recovers its
            # answers from it on a re-setup (setup._head_arm_features). Without it, a re-setup would
            # offer CCC's own copy back as the source and re-copy the mirror onto itself — pinning a
            # key the owner may since have rotated, and never seeing the new one.
            lines.append(f"export {stem}_LICENCE_KEY_SRC={f['key']}")
    return lines


def environment_yml(name: str) -> str:
    """A starter `environment.yml` for `name`, **from the Head** (`GET /features/{name}/environment.yml`).

    The one thing here that is not offline, and deliberately not local: the list of system libraries a
    Feature's software links against belongs next to the code that builds against it, which is
    `ccc_head_api.features`. A second copy in the CLI would be a second copy to forget when a release
    adds a library, and the symptom of forgetting is an `ImportError` in someone else's Spawn.
    """
    from . import api, auth
    client = auth._client()
    client.token = auth._load_token()
    try:
        return (client.raw(f"/features/{name}/environment.yml") or b"").decode()
    except api.ApiError as e:
        if ui.DEBUG:
            raise
        raise ui.abort(e.user_message()) from None


def activation_project(name: str) -> dict[str, str]:
    """The project CCC submits to activate `name` on an Arm, `{file: body}`.

    Read out of the Head's own API venv rather than over HTTP, because both callers stand on the Head
    (`cuttle arm admit`, and `cuttle setup --head` for its own co-located Arm) and the second of them
    runs before any account has ever logged in — there is no session to authenticate with, and being
    the operator on this box is already the stronger claim.

    Raises LookupError for a Feature that needs no activation. Most do not, and an Arm declaring one is
    simply admitted without the step (see cuttle.enroll.activate_arm).
    """
    from . import auth
    r = run(auth._admin_argv(["feature-activation", name]), sudo=True,
            capture_output=True, text=True, check=False)
    # 1 is the admin tool's "this Feature needs no activation". Anything else is a broken invocation —
    # a missing venv, a sudo refusal — and must not be mistaken for it: silently skipping activation
    # would admit an Arm whose Licence was never created, which is the exact failure this whole path
    # exists to prevent.
    if r.returncode == 1:
        raise LookupError(name)
    if r.returncode != 0:
        raise ui.abort(f"could not ask the Head what activating {name!r} takes: "
                       f"{(r.stderr or '').strip().splitlines()[-1:] or ['no output']}")
    return json.loads(r.stdout)


def report(name: str, path: str) -> None:
    """Say what this box will actually be able to do with `name`, at the moment it is declared.

    The ABI tags are the interesting part and the least discoverable: they decide which Environments
    can ever run here, and a donor whose checkout ships only cp310 wheels should learn that now rather
    than from a submitter's confused bug report.
    """
    tags = abi_tags(name, path)
    if tags:
        ui.say(f"  [muted]{name}: wheels for {', '.join(tags)} — Spawns must declare a matching "
               f"Python[/muted]")
