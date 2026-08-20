"""`cuttle setup` — provision a node in one of two roles, on a native-Linux box.

`--head`: self-hosts the Headscale coordinator + the Head API, runs slurmctld, and carries a
co-located Arm as a *static* Slurm node. `--local` is an alias (a single box is just a Head).

`--arm`: joins an existing cluster with a join token, pulls the munge key from the Head API over the
mesh, and registers as a *dynamic* Slurm node (`slurmd -Z`) fetching its config from slurmctld.

Drives apt+sudo directly. Intended path only — no fallbacks (test box; failures decided live).
"""

import getpass
import hashlib
import ipaddress
import json
import os
import platform
import re
import secrets
import socket
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from . import (api, auth, backup, config, enroll, headapi, menu, mesh, monitor, settings, system,
               templates, tls, ui)
from . import features as feature_profiles   # `features` is a local name throughout this module
from .system import run, sh


def _write_root_file(path: Path, content: str, mode: str = "644") -> None:
    """Write `content` to a root-owned path via a temp file + sudo install.

    Kept as a wrapper rather than replaced by `system.install_file`: it is passed as a CALLBACK into
    mesh.up_arm and headapi.install_enroll, so its (path, content, mode) shape is a contract.
    """
    system.install_file(content, path, mode)


def preflight(role: str) -> None:
    """`role` is "head" or "arm". WSL2 is a proven Arm platform (mesh, slurmd, and CUDA inside a Pod
    all verified on an RTX 4090) but an unproven Head: headscale and slurmctld have never run there,
    and a Head is expected to stay up."""
    rel = system.os_release()
    # ui.say, not print: inside a section this is a row of the box. print goes to stdout, which
    # `ui.fullscreen` holds back until the whole flow is over — so the box would sit empty while the
    # facts about this machine waited to be replayed at the end.
    ui.say(f"  [muted]host={system.hostname()} os={rel.get('PRETTY_NAME')} "
           f"cpus={system.cpu_count()} mem={system.real_memory_mb()}MB[/muted]")
    # Gating a supported platform, not an internal invariant — so ui.abort (a clean ✗ + a next step),
    # not `assert` (which prints a bare one-liner and vanishes entirely under `python -O`).
    if not system.is_native_linux_x86():
        raise ui.abort("this MVP supports Linux x86_64 only",
                       next_step="run setup on a native x86_64 Ubuntu 24.04 box")
    if rel.get("VERSION_ID") != "24.04":
        raise ui.abort(f"this MVP is pinned to Ubuntu 24.04 (this box is {rel.get('VERSION_ID')!r})",
                       next_step="use Ubuntu 24.04")
    if system.is_wsl():
        if role != "arm":
            raise ui.abort("WSL2 can host an Arm, but not the Head (headscale/slurmctld unproven here)",
                           next_step="run `cuttle setup --arm` on this box, and host the Head elsewhere")
        ui.say("  [muted]WSL2 — supported as an Arm, with one permanent limit: /dev/dxg is a single "
               "device node for ALL GPUs, so Slurm can never isolate GPUs per-Spawn here — this box "
               "registers one Device however many cards it has[/muted]")
    run([system.nvidia_smi(), "--query-gpu=name,driver_version", "--format=csv,noheader"])
    # Before anything is installed, joined, or written: a box whose GPUs are not all the same cannot be
    # described by ONE `vramN` Feature, and both roles hit this — the Head's own co-located Arm is an
    # Arm like any other. Aborts (a clean ✗ naming the cards it found), never a traceback.
    model, vram_mb = system.gpu_profile()
    # Physical cards vs schedulable Devices: equal natively, and deliberately NOT on WSL, where every
    # card sits behind the one /dev/dxg. Printed together so a WSL donor sees why their second card
    # bought them nothing.
    cards, devices = len(system.gpu_inventory()), len(system.gpu_devices())
    ui.say(f"  [muted]gpu={cards}x {model} ({vram_mb} MiB each) → "
           f"Gres=gpu:{devices} Feature={config.vram_feature(vram_mb)}[/muted]")


def configure_apptainer_wsl() -> None:
    """Make `apptainer exec --nv` work on a WSL Arm, so nothing above the node ever learns what WSL
    is. Contains the platform quirk at the platform boundary: `cuttle run`, Tide Pools, and any
    future API-launched job all keep emitting one portable exec line.

    Two additive lines (see config.WSL_LIB_ROOT for the measurements). Idempotent: `setup --arm`
    re-runs, and a duplicate `bind path` would be applied twice.
    """
    sh(f"grep -qxF 'bind path = {config.WSL_LIB_ROOT}' {config.APPTAINER_CONF} || "
       f"echo 'bind path = {config.WSL_LIB_ROOT}' >> {config.APPTAINER_CONF}", sudo=True)
    sh(f"grep -qxF '{config.WSL_EXTRA_NVLIB}' {config.APPTAINER_NVLIBLIST} || "
       f"echo '{config.WSL_EXTRA_NVLIB}' >> {config.APPTAINER_NVLIBLIST}", sudo=True)
    ui.say(f"  [muted]WSL Arm — Apptainer binds {config.WSL_LIB_ROOT} and injects "
           f"{config.WSL_EXTRA_NVLIB}; --nv now behaves as on native Linux[/muted]")


def add_repos(head: bool = False) -> None:
    """Add the apt sources CCC's pins need. Every one of them is an EXTRA file under
    sources.list.d — this box's own sources are never rewritten (see the snapshot template).

    The apptainer PPA and NodeSource are added on the **Head only**. An Arm installs the Head's pinned
    .deb by path (install_apptainer) and never builds the Dashboard, so a donor's machine gains no
    third-party repo at all — which is both the hygiene win and the reason two Arms set up months
    apart now run the same apptainer.
    """
    _write_root_file(config.UBUNTU_SNAPSHOT_LIST, templates.render_ubuntu_snapshot_list())
    if head:
        run(["add-apt-repository", "-y", config.APPTAINER_PPA], sudo=True)
        # Written ourselves rather than by piping NodeSource's setup script to a root shell: the
        # script also apt-get updates, installs, and rewrites sources of its own choosing. Two lines
        # is the whole repo. --yes: clobber on re-run instead of prompting, as above.
        sh(f"curl -fsSL {config.NODESOURCE_KEY_URL} "
           f"| gpg --yes --dearmor -o {config.NODESOURCE_KEYRING}", sudo=True)
        _write_root_file(config.NODESOURCE_LIST,
                         f"deb [signed-by={config.NODESOURCE_KEYRING}] "
                         f"{config.NODESOURCE_REPO} nodistro main\n")
    sh(
        # --yes: clobber the keyring on re-run instead of prompting (public repo key; setup is idempotent).
        "curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey "
        "| gpg --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg && "
        "curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/"
        "nvidia-container-toolkit.list "
        "| sed 's#deb https://#deb [signed-by=/usr/share/keyrings/"
        "nvidia-container-toolkit-keyring.gpg] https://#g' "
        "> /etc/apt/sources.list.d/nvidia-container-toolkit.list",
        sudo=True,
    )


def _version_available(spec: str) -> bool:
    """Can any configured apt source still supply `pkg=version`? Asked only after an install failed."""
    name, _, version = spec.partition("=")
    out = run(["apt-cache", "madison", name], capture_output=True, text=True, check=False).stdout
    return version in {line.split("|")[1].strip() for line in out.splitlines() if "|" in line}


def _apt_install(pkgs: list[str]) -> None:
    """`apt-get install`, with a *rotted pin* turned into something the operator can act on.

    apt reports an unavailable pin as `E: Version '23.11.4-1.2ubuntu5' for 'slurm-wlm' was not found` —
    accurate, and useless unless you already know CCC pinned it and where. Raw tool errors must not
    reach the user, so the one failure we can name precisely gets named: which package, which pin,
    that the repo dropped it, and the single file to edit.

    Anything else re-raises untouched — `run` has already printed apt's own output.
    """
    try:
        run(["apt-get", *system.APT_LOCK_WAIT, "install", "-y", *pkgs], sudo=True)
    except subprocess.CalledProcessError:
        for spec in pkgs:
            if "=" not in spec or _version_available(spec):
                continue
            name, _, version = spec.partition("=")
            raise ui.abort(
                f"{name} {version} is gone from every apt source this box has — CCC pins that exact "
                f"version, and the repository no longer carries it",
                next_step=f"`apt-cache madison {name}` lists what IS available; bump the pin (and "
                          f"UBUNTU_SNAPSHOT with it) in cli/core/src/cuttle/config.py, then re-run "
                          f"setup and re-test the cluster on the new version")
        raise


def install_packages(slurm_pkg: str = config.SLURM_PKG, head: bool = False) -> set[str]:
    """Install the pinned system stack, EXCEPT apptainer — that arrives as the Head's pinned .deb
    (install_apptainer), which on the Head does not exist yet when this runs.

    `slurm_pkg` is the full suite on a Head, just the node daemon on an Arm. Returns the set of
    packages installed *before* this ran, so the caller can close the manifest window with
    `record_installed` once apptainer is in too — teardown purges from that manifest, and apptainer
    plus its deps must be in it.

    `head` adds `acl`, which only the Head can use: it is what lets the Mantle hand ONE Guise read
    access to ONE uploaded project (see temp/spawn-isolation.md), and the uploads only ever exist
    here. A donor's box gains nothing it cannot use.

    `head` also adds Node, and for the same reason: `build_spa` runs `npm` some twenty minutes later,
    and a Head with no Node used to get that far and then fail on `No such file or directory: 'npm'`
    with the Dashboard — the primary surface — never built.

    `libnss3-tools` is on every role, this one included: it carries `certutil`, and without it the
    Chromium-family browsers on this box go on warning about the Dashboard however well the OS trust
    store is set up (see system.trust_ccc_ca_browsers).

    `uidmap` likewise, and for the same shape of reason: newuidmap/newgidmap are what an unprivileged
    `apptainer build` uses to map its fakeroot namespace (see ensure_subid_range), and every node
    builds the Pod image.
    """
    run(["apt-get", *system.APT_LOCK_WAIT, "update"], sudo=True)
    before = system.installed_packages()
    _apt_install([slurm_pkg, config.NVIDIA_CTK_PKG, config.MUNGE_PKG, config.NSS_TOOLS_PKG,
                  config.UIDMAP_PKG,
                  *([config.ACL_PKG, config.NODE_PKG] if head else [])])
    return before


def install_apptainer(deb: Path) -> None:
    """Install the Head's pinned apptainer from the .deb itself — the same bytes on the Head and on
    every Arm, whatever the PPA holds by then.

    By PATH, not by name: apt treats an argument containing `/` as a file, installs that exact file,
    and resolves its dependencies from the configured sources (all of apptainer's are plain Ubuntu
    archive packages — measured; the PPA carries nothing else apptainer needs).
    """
    _apt_install([str(deb)])


def record_installed(before: set[str]) -> None:
    """Record exactly what setup added (incl. pulled deps) so teardown removes only these."""
    added = sorted(system.installed_packages() - before)
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.APT_MANIFEST.write_text("\n".join(added) + "\n" if added else "")
    ui.say(f"  [muted]recorded {len(added)} newly-installed packages → {config.APT_MANIFEST}[/muted]")


def pin_apptainer_deb() -> Path:
    """**Head only.** Fetch the apptainer .deb once and keep it forever, as the fleet's one build.

    Launchpad's PPA keeps only its newest build, so an Arm enrolled today and one enrolled in three
    months would otherwise get different apptainer versions — and the earlier one would by then be
    unfetchable. Serving the file removes the PPA from the equation entirely: the Head hands the same
    bytes to every Arm, and to itself.

    **A re-run keeps the recorded .deb.** That is the whole point — re-fetching would silently give
    later Arms a build the rest of the fleet is not running. Bumping it is a deliberate act: delete
    config.APPTAINER_RECORD and re-run.
    """
    if config.APPTAINER_RECORD.exists():
        rec = json.loads(config.APPTAINER_RECORD.read_text())
        ui.say(f"  [muted]apptainer {rec['version']} already pinned for this cluster — keeping it "
               f"(delete {config.APPTAINER_RECORD} to re-pin)[/muted]")
        return config.HEAD_DEBS_DIR / rec["filename"]

    run(["install", "-d", "-m", "0755", str(config.HEAD_DEBS_DIR)], sudo=True)
    # `apt-get download` needs no root, and run as root it warns about the unsandboxed _apt user —
    # so it runs as the operator, into a temp dir, and the result is installed root-side.
    with tempfile.TemporaryDirectory() as tmp:
        run(["apt-get", "download", config.APPTAINER_PKG], cwd=tmp)
        deb = next(Path(tmp).glob("apptainer_*.deb"))
        rec = {
            "version": run(["dpkg-deb", "-f", str(deb), "Version"],
                           capture_output=True, text=True).stdout.strip(),
            "filename": deb.name,
            "sha256": hashlib.sha256(deb.read_bytes()).hexdigest(),
        }
        run(["install", "-m", "0644", str(deb), str(config.HEAD_DEBS_DIR / deb.name)], sudo=True)
    _write_root_file(config.APPTAINER_RECORD, json.dumps(rec, indent=2, sort_keys=True) + "\n")
    ui.say(f"  [muted]pinned apptainer {rec['version']} for the whole fleet → "
           f"{config.HEAD_DEBS_DIR / rec['filename']}[/muted]")
    return config.HEAD_DEBS_DIR / rec["filename"]


def write_slurm_config(mesh_ip: str, features: list[dict] | None = None) -> None:
    for d, owner in ((config.SLURM_STATE_DIR, "slurm"),
                     (config.SLURM_LOG_DIR, "slurm"),
                     (config.SLURMD_SPOOL_DIR, "root")):
        run(["install", "-d", "-o", owner, "-g", owner, str(d)], sudo=True)
    # The Head plus any Arms already admitted (a re-run must not un-admit them). The Head's own row is
    # skipped: head_gres_node() already supplies its lines (see enroll.render_gres).
    reg = enroll.load_registry()
    # The Head's Weight goes in the file, not just `scontrol update`, or the next reconfigure wipes it.
    # Unknown on a first setup (the benchmark runs later, and re-renders this) -> node_weight's floor.
    head_row = reg.get(system.hostname()) or {}
    powers = head_row.get("powers")
    # The Head's own Arm Features. `features` only when the caller has them and the registry does not
    # yet — a first `setup --head`, where register_head_arm has not run. Otherwise the registry row is
    # the source: `scontrol reconfigure` re-reads this file and resets any node attribute the file does
    # not state, exactly as it does Weight above, so every later caller (arm authorize/admit/remove)
    # must re-state them or silently drop the Head out of Eligibility for every Feature it offers.
    head_features = sorted(f["name"] for f in (features or head_row.get("features") or []))
    # Guises (temp/spawn-isolation.md): `Prolog=` goes in only once every admitted Arm has reported
    # having the script, named out loud since the fix is re-running `setup --arm` on those boxes — a
    # silently-absent Prolog would mean every Spawn quietly keeps running as the Mantle.
    missing = enroll.arms_missing(config.ARM_CAP_PROLOG)
    if missing:
        # ui.warn, not ui.say: lands in the end-of-run trouble summary, or an operator misses it and
        # believes Guises are live when every Spawn still runs as the Mantle.
        ui.warn(f"Guises are held back — these Arms have no prolog yet: {', '.join(missing)}. "
                f"Re-run `cuttle setup --arm` on each, then re-run this.")
    # Same gate for job_container/tmpfs, asked separately: an Arm may report only the prolog, and
    # holding both lines back over that would take working Guises from a box that has them.
    missing_tmpfs = enroll.arms_missing(config.ARM_CAP_TMPFS)
    if missing_tmpfs:
        ui.warn(f"Private per-Spawn /tmp is held back — these Arms have no job_container.conf yet: "
                f"{', '.join(missing_tmpfs)}. Re-run `cuttle setup --arm` on each, then re-run this.")
    _write_root_file(
        config.SLURM_CONF,
        templates.render_slurm_conf(mesh_ip, config.node_weight(powers) if powers else 1,
                                    features=head_features,
                                    missing_prolog=missing, missing_tmpfs=missing_tmpfs))
    nodes = [templates.head_gres_node()]
    nodes += [(n, e["gpu_devices"]) for n, e in sorted(reg.items())
              if e["admitted"] and not e.get("head")]
    _write_root_file(config.GRES_CONF, templates.render_gres_conf(nodes))
    # What task/cgroup enforces. Configless serves it to every Arm with slurm.conf, so it is written
    # here only — an Arm never authors one.
    _write_root_file(config.CGROUP_CONF, templates.render_cgroup_conf())
    # The Head's own slurmd (its co-located Arm) runs Spawns too, so the per-node files its slurm.conf
    # references must exist here as well as on every Arm (setup_arm writes them there). The Head is
    # never in `missing` — it has no /enroll POST to report in, and this run is what writes its files.
    _write_root_file(config.SLURM_EPILOG, templates.render_epilog(), mode="755")
    _write_root_file(config.SLURM_PROLOG, templates.render_prolog(), mode="755")
    _write_root_file(config.JOB_CONTAINER_CONF, templates.render_job_container_conf())


def make_registry_dir() -> None:
    """The Enroll API's only writable location — it runs as munge and owns nothing else."""
    run(["install", "-d", "-m", "0755", "-o", config.ENROLL_API_USER, "-g", config.ENROLL_API_USER,
         str(config.CCC_LIB)], sudo=True)


def ensure_guise_group() -> None:
    """Create the group every **Guise** shares, if it is not there yet.

    Called from more than one place because more than one thing needs it before `ensure_guise_accounts`
    (which is where a Guise, and therefore this group, is normally born) runs: the Licence directory is
    group-owned by it, and so is the mirrored Feature licence key. Both happen several steps earlier —
    `setup --head` records this box's Features long before it provisions anyone's Unix identity.

    The gid is pinned (config.GUISE_GROUP_GID) so the Head and every Arm agree about it; `check=False`
    because "it already exists" is the normal case on every run after the first.

    Asserted afterwards, like the Mantle's uid, because check=False also swallows a *collision* — the
    pinned gid already belonging to some unrelated group on this box. Without the check that failure
    surfaces far away, as every later `install -g` and `useradd --gid` reporting no such group.
    """
    run(["groupadd", "--gid", str(config.GUISE_GROUP_GID), config.GUISE_GROUP], sudo=True, check=False)
    r = run(["getent", "group", config.GUISE_GROUP], capture_output=True, text=True, check=False)
    gid = (r.stdout or "").strip().split(":")[2] if (r.stdout or "").strip() else None
    assert gid == str(config.GUISE_GROUP_GID), (
        f"{config.GUISE_GROUP} is gid {gid}, not the pinned {config.GUISE_GROUP_GID} — "
        f"another group already holds that gid on this box")


def provision_licence_home(path: str) -> None:
    """Create the **Licence** account (config.LICENCE_USER) with `path` as its home, so the vendor's
    activation can be run AS it and land its state there.

    This is the whole mechanism. TurboActivate picks its state directory with getpwuid(getuid())->pw_dir
    and ignores $HOME, so the identity that activates decides where the licence lives. Give an account
    this directory as its home, activate as that account, and the files appear here at whatever names
    the installed release uses — nobody ever has to find, name or copy them.
    `ccc_head_api.spawns.JOB_SCRIPT` then binds this directory at the Guise's home inside the Pod, which
    is where the same getpwuid call looks from in there.

    Group-owned by GUISE_GROUP and 2770, because a Spawn runs as a **Guise** — a different uid — and
    still has to WRITE here: the activation record is rewritten on periodic online re-validation, and a
    read-only licence would eventually go stale. setgid so files the re-validation creates inherit the
    group rather than the Guise's own private one, which no other Guise could then read.

    Idempotent. `--create-home` on an account that already exists is a no-op, and the mode is re-applied
    every run — a re-setup that repaired nothing would be the surprising behaviour.
    """
    ensure_guise_group()
    # No --uid: locally allocated on purpose (see config.LICENCE_USER). --create-home makes `path`
    # itself, including parents, which is why the operator is never asked to mkdir anything.
    run(["useradd", "--system", "--home-dir", path, "--create-home", "--shell", "/usr/sbin/nologin",
         "--comment", config.LICENCE_GECOS, config.LICENCE_USER], sudo=True, check=False)
    # Re-pointed explicitly: useradd above is non-fatal, so an account left over from a setup that
    # named a DIFFERENT directory would otherwise keep its old home and activate into the wrong place.
    run(["usermod", "--home", path, config.LICENCE_USER], sudo=True, check=False)
    run(["install", "-d", "-m", "2770", "-o", config.LICENCE_USER, "-g", config.GUISE_GROUP, path],
        sudo=True)
    # Empty is normal here: CCC fills this directory itself via an activation Spawn run as this Arm
    # is admitted (cuttle.enroll.activate_arm, minutes away at this point in setup) — a **Guise**
    # does the activating, inside a Pod, with this bound at its home.


def provision_mantle() -> None:
    """Create the **Mantle** — the one identity every Spawn executes as — on THIS node, Head or Arm.

    The uid is pinned (config.CCC_UID), not distro-allocated, and that is the whole point: Slurm runs a
    job as the *submitting* uid, so slurmctld records the Head API's uid at sbatch and every slurmd
    setuid()s to it. A dynamically-allocated `ccc` would get a different uid on each box and a Spawn
    scheduled onto a remote Arm would setuid() to a user that doesn't exist there. One pinned uid,
    cluster-wide, is what makes a Spawn the same Spawn on any node.

    Home-less on purpose: the Mantle owns no per-node state anywhere. It reads base.sif from the
    world-readable CCC_IMAGES_DIR and scratches in /tmp (see the API's JOB_SCRIPT), so a donor box is
    left with nothing. `nologin` is fine — slurmd execs the batch script by its shebang, not a shell.

    Idempotent (re-runs are non-fatal), but a uid/gid COLLISION is fatal by design: useradd fails,
    setup halts, and we decide live rather than silently landing on a mismatched uid.
    """
    run(["groupadd", "--gid", str(config.CCC_GID), config.HEAD_API_USER], sudo=True, check=False)
    run(["useradd", "--uid", str(config.CCC_UID), "--gid", str(config.CCC_GID), "--system",
         "--no-create-home", "--shell", "/usr/sbin/nologin", config.HEAD_API_USER],
        sudo=True, check=False)  # non-fatal if the account already exists
    # useradd's failure is non-fatal above (re-run), so verify the uid we actually ended up with —
    # a pre-existing `ccc` at some OTHER uid would silently break remote Spawns.
    uid = run(["id", "-u", config.HEAD_API_USER], capture_output=True, text=True).stdout.strip()
    assert uid == str(config.CCC_UID), (
        f"the `{config.HEAD_API_USER}` account on this box has uid {uid}, but CCC pins it to "
        f"{config.CCC_UID}. Every node must agree or Slurm cannot run a Spawn here. Remove the "
        f"conflicting account (`sudo userdel {config.HEAD_API_USER}`) and re-run setup.")


def provision_head_auth() -> None:
    """Head-side identity spine: the Mantle's Head state + auth store (head.db). On the Head the Mantle
    also runs the Head API and owns head.db plus the submission dirs (uploaded projects, Spawn results,
    scratch). Called after the API venv is built (seeding the admin runs from it) and before the Head
    API unit and rsyncd start (both need User=ccc + these dirs). Idempotent on re-run."""
    provision_mantle()
    # 0711 — traverse, never list (see temp/spawn-isolation.md). A **Guise** has to reach its own
    # `spawns/<jid>`, `scratch/<jid>` and `projects/<uuid>`, and a 0700 parent stops it at the door. It
    # is `--x` on purpose: reaching a directory whose exact name you already have is all a Spawn needs,
    # and it is the whole difference from `r-x`, which would let any Spawn enumerate every other User's
    # uploads and results. head.db stays 0600 ccc, so what these parents contain is still unreadable.
    for d in (config.HEAD_STATE_DIR, config.HEAD_PROJECTS_DIR, config.HEAD_SPAWNS_DIR,
              config.HEAD_SCRATCH_DIR):
        run(["install", "-d", "-m", "0711", "-o", config.HEAD_API_USER, "-g", config.HEAD_API_USER,
             str(d)], sudo=True)
    admin_user = config.FIRST_ADMIN_USER
    auth.seed_admin(admin_user)  # registers the one-time temp password as a result


def ensure_munge_key() -> None:
    """Mint the cluster's munge key if this Head has none. **Head only** — an Arm is *given* this key
    over /enroll (enroll.write_munge_key); a second one would be a second cluster.

    Can't rely on the munge package's postinst (mints a key on a FRESH install only): `cuttle teardown`
    deletes the key but leaves munge installed if the package predates CCC, since the apt manifest only
    records what setup itself added — so the next `apt-get install munge` is a no-op, no postinst runs,
    and munge dies on a missing keyfile, taking slurmctld and slurmd with it.

    `test -s` under sudo rather than Path.exists(): /etc/munge is `drwx------ munge munge`, and pathlib
    raises PermissionError instead of answering False when it cannot traverse (see monitor.py).
    """
    if run(["test", "-s", str(config.MUNGE_KEY)], sudo=True, check=False).returncode == 0:
        return
    run([config.MUNGEKEY_BIN, "--create", "--keyfile", str(config.MUNGE_KEY)], sudo=True)
    run(["chown", "munge:munge", str(config.MUNGE_KEY)], sudo=True)
    run(["chmod", "0400", str(config.MUNGE_KEY)], sudo=True)
    ui.say(f"  [muted]minted a new cluster munge key → {config.MUNGE_KEY}[/muted]")


def start_daemons() -> None:
    ensure_munge_key()  # munge cannot start without it, and slurmctld/slurmd cannot start without munge
    # enable = start on boot; restart = start if down, else reload new config (idempotent re-run).
    run(["systemctl", "enable", "munge", "slurmctld", "slurmd"], sudo=True)
    run(["systemctl", "restart", "munge", "slurmctld", "slurmd"], sudo=True)
    # What actually delivers the slurm.conf just rendered to a REMOTE Arm. Restarting slurmctld does
    # not: a configless slurmd caches its fetched copy and re-fetches only on its own startup or on a
    # reconfigure, so without this a donor box keeps serving the previous config until something else
    # happens to reconfigure (an `arm admit`, say — which is the only reason `Prolog=` ever reached
    # willpersonal). Every fleet-wide line this project renders depends on it landing.
    #
    # Unconditional and safe on 23.11+, where reconfigure "operates by creating new processes for the
    # daemons, then passing control to the new processes when or if they start up successfully" — so
    # plugin types (JobContainerType) reload without a hand-rolled restart pass over the fleet, and a
    # config that fails to load leaves the old process serving rather than killing slurmd.
    #
    # check=False: slurmctld was restarted a moment ago and may not have finished coming up. Failing
    # setup over that would be wrong — the files are written, and the next reconfigure lands them.
    #
    # Via enroll.reconfigure, never `scontrol` directly: a reconfigure resets every dynamic node's
    # Weight, so every admitted donor Arm needs its Weight re-pushed behind it. See there.
    enroll.reconfigure(check=False)


def ensure_bench_secret() -> None:
    """Mint the [bench] module's rsync secret if this Head has none.

    Cluster-lifetime rather than per-run: the enrollment benchmark never goes through the Head API, so
    there is no **Siphon** to hand it (see temp/spawn-isolation.md). Root-owned 0600 — `enroll`
    reads it with sudo when it renders the benchmark's sbatch, and rsyncd (running as root) reads it to
    check the login, which is what lets [bench] keep `strict modes = true` where a Siphon cannot.

    Kept if it already exists, so a `setup --head` re-run does not invalidate a benchmark in flight."""
    if run(["test", "-s", str(config.RSYNCD_BENCH_SECRETS)], sudo=True, check=False).returncode == 0:
        return
    _write_root_file(config.RSYNCD_BENCH_SECRETS,
                     f"{config.RSYNC_BENCH_USER}:{secrets.token_urlsafe(32)}\n", mode="600")


def install_rsyncd(mesh_ip: str) -> None:
    """Stage-in/out daemon, bound to the Head's mesh address only. Idle until a remote Arm uses it;
    started on every Head so admitting the first Arm needs no reconfigure. Runs as root (873 is
    privileged) but serves each module as `ccc`, so files stay ccc-owned.

    The conf itself defines no per-Spawn module — it `&include`s RSYNCD_DIR, which the Head API mints
    **Siphon**s into as `ccc`. That directory is 0700 ccc: rsyncd reads it as root, and
    nothing else on the box has any business seeing one Spawn's stage-out credential. The module roots
    a Siphon points at (projects, spawns) are created ccc-owned by provision_head_auth, which must run
    first."""
    run(["install", "-d", "-m", "0700", "-o", config.HEAD_API_USER, "-g", config.HEAD_API_USER,
         str(config.RSYNCD_DIR)], sudo=True)
    ensure_bench_secret()
    _write_root_file(config.RSYNCD_CONF, templates.render_rsyncd_conf(mesh_ip))
    _write_root_file(config.RSYNCD_UNIT, templates.render_rsyncd_unit())
    run(["systemctl", "daemon-reload"], sudo=True)
    run(["systemctl", "enable", config.RSYNCD_SERVICE], sudo=True)
    run(["systemctl", "restart", config.RSYNCD_SERVICE], sudo=True)


def _next_subid_base() -> int:
    """The first subid past every range already allocated on this box, floored at SUBID_BASE.

    Overlapping ranges are not rejected by anything — shadow writes what it is told — and two users
    sharing subids means one can act as the other inside a namespace. So this reads BOTH files and
    starts after the highest end in either, keeping the uid and gid ranges identical as well.
    """
    end = config.SUBID_BASE
    for path in (config.SUBUID_FILE, config.SUBGID_FILE):
        for line in path.read_text().splitlines() if path.exists() else []:
            parts = line.split(":")
            if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                end = max(end, int(parts[1]) + int(parts[2]))
    return end


def ensure_subid_range() -> None:
    """Give the operator subordinate uids/gids if they have none. **Every node** — an Arm builds the
    Pod image too.

    `apptainer build` runs unprivileged, and its fakeroot namespace is exactly a mapping of the
    operator's subuid range. `adduser` writes that range for a local account; an LDAP or AD account
    never passed through adduser, so it has none, and the build fails 20 minutes in with
    `Could not write info to setgroups: Permission denied` — an error that names neither subuid nor
    the operator.

    Append-only, and only when the operator is absent from the file: these files are the box's own,
    may already describe other users, and a range once handed out must not move under anyone. For the
    same reason teardown leaves what this wrote — two lines naming a user, harmless where they sit,
    and rewriting a shared system file on the way out is the worse trade.
    """
    user = getpass.getuser()
    have = {p: any(line.startswith(f"{user}:") for line in p.read_text().splitlines())
            for p in (config.SUBUID_FILE, config.SUBGID_FILE) if p.exists()}
    if all(have.get(p) for p in (config.SUBUID_FILE, config.SUBGID_FILE)):
        return
    base = _next_subid_base()
    for path in (config.SUBUID_FILE, config.SUBGID_FILE):
        if have.get(path):
            continue
        sh(f"echo '{user}:{base}:{config.SUBID_COUNT}' >> {path}", sudo=True)
    ui.say(f"  [muted]{user} had no subordinate ids — granted {base}+{config.SUBID_COUNT} so the "
           f"unprivileged image build can map a fakeroot namespace[/muted]")


def build_base_sif() -> None:
    """Build the base Pod image at CCC_SIF — the SAME path on every node, Head or Arm.

    It lands in a world-readable /opt tree rather than the operator's ~/.ccc because Spawns run as the
    home-less **Mantle**, which cannot traverse a 0750 home. One path cluster-wide means the rendered
    job script names the sif identically wherever Slurm places the Spawn.

    Built locally on each node (apptainer build) — deliberately NOT shipped from the Head, which would
    push the image over the mesh at every enrollment. Skipped if already present, and teardown keeps it
    unless --all.

    **That skip is why a changed image needs `cuttle teardown --all`.** The definition here is rendered
    fresh on every run, but an existing base.sif is never compared against it: a node that already has
    one keeps whatever it built, however old. So changing BASE_DEF_TMPL or its pinned digest is a
    fleet-wide teardown --all + setup, not a re-run.
    """
    ensure_subid_range()   # the build maps a fakeroot namespace out of these; before anything else
    # Root-owned tree; make it operator-writable to build in place, then world-readable to run.
    # The group is the operator's real primary gid, NOT their username: a user-private group is an
    # Ubuntu default, not a rule, and a directory-managed operator (LDAP/AD) has no group of their own
    # name — `-g <username>` there is `install: invalid group`, and setup dies before the Pod image.
    run(["install", "-d", "-o", getpass.getuser(), "-g", str(os.getgid()),
         str(config.CCC_IMAGES_DIR)], sudo=True)
    if config.CCC_SIF.exists():
        ui.say(f"  [muted]base.sif exists ({config.CCC_SIF}) — skipping build. `cuttle teardown "
               f"--all` is what rebuilds it if the image definition has changed.[/muted]")
        return
    def_file = config.CCC_IMAGES_DIR / "base.def"
    def_file.write_text(templates.render_base_def())
    # Cache into CCC's own tree, not the default ~/.apptainer/cache. This build runs unprivileged, so
    # apptainer caches every pulled layer under the invoking operator's home — multi-GB, outside every
    # path teardown knows about, and reclaimed by nothing. Pointed at ~/.ccc instead, which teardown
    # already removes. Not `apptainer cache clean`, which would take the operator's unrelated layers.
    config.APPTAINER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    run(["apptainer", "build", str(config.CCC_SIF), str(def_file)],
        env={**os.environ, "APPTAINER_CACHEDIR": str(config.APPTAINER_CACHE_DIR)})
    run(["chmod", "-R", "a+rX", str(config.CCC_IMAGES_DIR)], sudo=True)


def install_env_tools() -> None:
    """Install the pinned uv + micromamba that build a Spawn's declared **Environment**
    (ccc_head_api.environments), into config.ENV_TOOLS_DIR. **Every node**, Head and Arm alike.

    On an Arm because an Arm now builds every Environment it runs, at job start, out of its own
    bandwidth — see config.ENV_TOOLS_DIR for the measurement that forced that. On the Head because its
    co-located Arm is an Arm like any other, and because **Arm Feature** libraries are still built here.

    Not baked into base.sif: the image is built once per node and skipped when it already exists
    (`build_base_sif`), so a tool pinned inside it could only be bumped by a full teardown of every
    enrolled box. As a separate tree it is re-installed by an ordinary re-setup.

    Verified against the sha256 pinned in config, then installed 0755 into a root-owned tree: the build
    runs as a **Guise** inside a Pod with this directory bound read-only, so the binaries must be
    readable and executable by a uid that is not the Mantle and must not be writable by one either.

    Re-runs re-verify and re-install: bumping the pin in config and re-running `setup` is the whole
    upgrade path, and it is per-node — a fleet on a new uv is a fleet that re-ran setup.
    """
    arch = platform.machine()
    if arch not in config.UV_SHA256:
        ui.say(f"  [warn]no pinned uv/micromamba for {arch} — declared Environments will fall back to "
               f"the cluster base image[/warn]")
        return
    run(["install", "-d", "-m", "0755", str(config.ENV_TOOLS_DIR)], sudo=True)
    with tempfile.TemporaryDirectory() as tmp:
        tar = Path(tmp) / "uv.tar.gz"
        _fetch(config.UV_URL.format(v=config.UV_VERSION, arch=arch), tar, config.UV_SHA256[arch])
        with tarfile.open(tar) as tf:
            tf.extractall(tmp, filter="data")
        uv = next(Path(tmp).rglob("uv"))
        run(["install", "-m", "0755", str(uv), str(config.ENV_TOOLS_DIR / "uv")], sudo=True)

        mm = Path(tmp) / "micromamba"
        _fetch(config.MICROMAMBA_URL.format(v=config.MICROMAMBA_VERSION,
                                            arch=config.MICROMAMBA_ARCH[arch]),
               mm, config.MICROMAMBA_SHA256[arch])
        run(["install", "-m", "0755", str(mm), str(config.ENV_TOOLS_DIR / "micromamba")], sudo=True)
    ui.say(f"  [muted]uv {config.UV_VERSION} + micromamba {config.MICROMAMBA_VERSION} → "
           f"{config.ENV_TOOLS_DIR}[/muted]")


def provision_env_cache() -> None:
    """The parent of every per-**Guise** package download cache on this box
    (config.NODE_ENV_CACHE_DIR). **Every node**, for the same reason as `install_env_tools`.

    Those caches are what make building per-Spawn affordable rather than merely possible: uv and
    micromamba hard-link out of them, so the second Spawn from one User to declare a given torch pays
    no download. Without them an Arm re-fetches several GB every time.

    Only the PARENT is made here, and only so a Guise can mkdir inside it — one shared cache does not
    work and should not be wanted; see config.NODE_ENV_CACHE_DIR for both reasons.

    0777 and not sticky. A Guise creates its own subtree here on its first build and owns it outright,
    so nothing in this directory is ever written by a uid that does not own it.

    CCC_LIB is created without -o/-g so an existing munge-owned one on the Head keeps its owner.
    """
    run(["install", "-d", "-m", "0755", str(config.CCC_LIB)], sudo=True)
    run(["install", "-d", "-m", "0777", str(config.NODE_ENV_CACHE_DIR)], sudo=True)
    ui.say(f"  [muted]environment build caches → {config.NODE_ENV_CACHE_DIR}/<guise> "
           f"(each pruned past {config.NODE_ENV_CACHE_MAX_GB}GB)[/muted]")


def build_bench_env() -> None:
    """Build THIS node's copy of the enrollment benchmark's torch **Environment**. Every node.

    base.sif carries no Python and no torch any more (templates.BASE_DEF_TMPL), and the benchmark is the
    one thing that ran on the image's own — so it needs an env of its own. It is not a Spawn's: nobody
    submitted it, it has no manifest and no Guise, and it is the same declaration on every box forever.

    **Per node, from PyPI, pinned** (config.BENCH_TORCH_PKG). It used to be built once on the Head and
    pulled by each Arm over a read-only [benchenv] rsync module — but that is 4.6 GB across the mesh,
    measured at 29 minutes to a remote Arm against the benchmark job's 10-minute wall. The pin is what
    now carries the comparability that one shared tree used to; see config.BENCH_TORCH_PKG.

    Here at setup rather than inside the benchmark job, for two reasons: the job has a wall and this
    does not, and a box whose network cannot fetch torch is a box that cannot run a Spawn either — which
    is worth failing on before it enrolls rather than as an unexplained missing Power.

    Built INSIDE a Pod, with the destination bound at the one path it will ever be used at: a venv bakes
    its prefix into pyvenv.cfg, its own `bin/python` symlink and every console-script shebang, so the
    tree does not survive being used from a different path. The host directory underneath is free to
    move, which is why this can build in the operator's temp dir and be installed afterwards.

    Skipped if it already exists — like base.sif, a re-run must not re-fetch 4.6 GB. `cuttle teardown
    --all` is what discards it (and the image) when the pin here changes.
    """
    dest = config.BENCH_ENV_DIR
    if (dest / "venv" / "bin" / "python").exists():
        ui.say(f"  [muted]benchmark environment exists ({dest}) — skipping build[/muted]")
        return
    uv = str(config.ENV_TOOLS_DIR / "uv")
    venv = f"{config.BENCH_ENV_MOUNT}/venv"
    with tempfile.TemporaryDirectory() as tmp:
        build, cache = Path(tmp) / "env", Path(tmp) / "cache"
        build.mkdir(), cache.mkdir()
        for argv in ([uv, "venv", "--python", config.BENCH_ENV_PYTHON, venv],
                     [uv, "pip", "install", "--python", f"{venv}/bin/python",
                      config.BENCH_TORCH_PKG]):
            # The same two settings that make a Spawn's Environment self-contained (see
            # ccc_head_api.environments): only-managed forbids uv from adopting an interpreter it did
            # not install — a system python this box happens to have and the Pod does not — and
            # INSTALL_DIR puts the one it downloads inside the tree that is kept.
            #
            # A THROWAWAY cache, deliberately not the node's (config.NODE_ENV_CACHE_DIR). This runs as
            # the operator, and every Spawn runs as a **Guise** — different uids, so nothing written
            # here could ever be reused, and writing it into the shared parent would leave an
            # operator-owned `uv/` tree that locks every Guise out of its own cache. That is exactly
            # what shipped and broke on hardware 2026-08-08.
            run(["apptainer", "exec", "--contain", "--pwd", "/",
                 "--bind", f"{build}:{config.BENCH_ENV_MOUNT},{cache}:/ccc-cache,"
                           f"{config.ENV_TOOLS_DIR}:{config.ENV_TOOLS_DIR}:ro",
                 "--env", f"UV_CACHE_DIR=/ccc-cache/uv,UV_PYTHON_PREFERENCE=only-managed,"
                          f"UV_PYTHON_INSTALL_DIR={config.BENCH_ENV_MOUNT}/py,"
                          f"TMPDIR=/ccc-cache/tmp,LC_ALL=C.UTF-8,LANG=C.UTF-8",
                 str(config.CCC_SIF), *argv], cwd="/")
        # World-readable in /opt, like base.sif and for the same reason: the benchmark runs as the
        # home-less cluster **Guise**, which can read neither the operator's home nor a 0700 Mantle
        # tree. `cp -a` keeps the symlinks as symlinks — `bin/python` points at BENCH_ENV_MOUNT, a path
        # that exists only inside a Pod, so resolving them here would copy nothing.
        run(["install", "-d", "-m", "0755", str(dest)], sudo=True)
        run(["cp", "-a", *[str(p) for p in build.iterdir()], str(dest)], sudo=True)
        run(["chmod", "-R", "a+rX", str(dest)], sudo=True)
    ui.say(f"  [muted]{config.BENCH_TORCH_PKG} → {dest}[/muted]")


def _fetch(url: str, dest: Path, sha256: str) -> None:
    """Download `url` to `dest` and refuse anything that is not exactly `sha256`.

    A mismatch is fatal rather than a warning: these two binaries execute a User's build backends and
    conda post-link scripts on the Head, as a Guise, and a Head that installed something else is not a
    Head anyone should be submitting to.
    """
    run(["curl", "-fsSL", "-o", str(dest), url])
    got = hashlib.sha256(dest.read_bytes()).hexdigest()
    if got != sha256:
        raise RuntimeError(f"checksum mismatch for {url}\n  expected {sha256}\n  got      {got}")


def required_sans(mesh_ip: str) -> list[str]:
    """Every address a client might dial the Head at. All of them must be in the serving cert's SANs,
    or TLS rejects the connection with a name mismatch:
      - the **Cluster name** — what everything canonically uses now (config.DOMAIN),
      - the LAN IP + hostname  — a browser or CLI on the local network, or a device with no hosts entry,
      - the mesh IP            — a remote Arm's owner browsing over the mesh,
      - localhost/127.0.0.1    — a WSL Arm's dashboard forward, which surfaces the Head at
                                 https://localhost in the Windows browser.
    """
    return [f"DNS:{config.DOMAIN}", f"DNS:{system.hostname()}", "DNS:localhost",
            f"IP:{system.primary_ip()}", f"IP:{mesh_ip}", "IP:127.0.0.1"]


def _cert_sans(cert: Path) -> set[str]:
    """The SANs actually present in `cert`. openssl PRINTS an IP SAN as `IP Address:1.2.3.4` but ACCEPTS
    it as `IP:1.2.3.4`, so normalise to the input spelling — otherwise every IP reads as missing and the
    cert is reissued on every single run."""
    r = run(["openssl", "x509", "-noout", "-ext", "subjectAltName", "-in", str(cert)],
            sudo=True, capture_output=True, text=True, check=False)
    parts = re.split(r"[,\n]", r.stdout.replace("IP Address:", "IP:"))
    return {p.strip() for p in parts if p.strip().startswith(("DNS:", "IP:"))}


def install_tls(mesh_ip: str) -> None:
    """The Head's TLS identity: a CCC root CA, and a serving cert signed by it.

    The CA is minted ONCE and kept — it is the thing every box trusts and every client pins, so a new
    one un-trusts the whole cluster at a stroke. `teardown.remove_tls` is `--all` only for exactly that
    reason, so a routine teardown+setup does not churn it either.

    The LEAF, by contrast, is cheap to replace, and this reissues it whenever it stops covering
    `required_sans` — or stops being signed by the current CA. That is what upgrades an existing Head
    onto the Cluster name, and it also self-heals a changed LAN IP, which previously left TLS quietly
    broken with no path back short of a `--all` teardown. Clients notice nothing: what they pinned is
    the CA, and the new leaf still chains to it.

    Keys are readable only by the Mantle, the user the Head API runs as — re-asserted every run, see
    `_secure_tls`.
    """
    run(["install", "-d", "-m", "0755", str(config.CCC_TLS_DIR)], sudo=True)
    if not config.HEAD_CA_CERT.exists():
        # pathlen:0 — this CA signs the Head's serving cert and nothing that can sign in turn. It is
        # about to be installed into the OS trust store of every box in the cluster; a CA that could
        # mint intermediates would be a far larger thing to have handed them.
        run(["openssl", "req", "-x509", "-newkey", "rsa:4096", "-nodes",
             "-days", str(config.CERT_DAYS),
             "-subj", f"/CN=CCC Head CA ({system.hostname()})",
             "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign",
             "-keyout", str(config.HEAD_CA_KEY), "-out", str(config.HEAD_CA_CERT)], sudo=True)
        print(f"cuttle: minted the CCC certificate authority ({config.HEAD_CA_CERT})")
    want = required_sans(mesh_ip)
    if config.HEAD_CERT.exists():
        missing = [s for s in want if s not in _cert_sans(config.HEAD_CERT)]
        chains = run(["openssl", "verify", "-CAfile", str(config.HEAD_CA_CERT),
                      str(config.HEAD_CERT)], sudo=True, check=False,
                     capture_output=True, text=True).returncode == 0
        if not missing and chains:
            print(f"cuttle: TLS cert exists ({config.HEAD_CERT}) and covers every address — keeping it")
            _secure_tls()
            return
        why = (f"it does not cover {', '.join(missing)}" if missing
               else "it is not signed by this Head's CA")
        print(f"cuttle: reissuing the Head's TLS cert — {why}.\n"
              f"        Clients pin the CA, not this cert, so none of them has to do anything.")
    _mint_leaf(want)
    _secure_tls()
    print(f"cuttle: TLS cert for {','.join(want)}")


def _mint_leaf(sans: list[str]) -> None:
    """Issue the serving cert under the CA. A CSR + `x509 -req` rather than `req -x509`, because the
    latter can only self-sign. The CSR and the extension file are scratch and are removed here: they
    live in the same root-owned 0755 dir as the keys, and a stale head.ext would be a confusing thing
    to find next to a cert whose SANs no longer match it."""
    d, san = config.CCC_TLS_DIR, ",".join(sans)
    sh(f"""
umask 077
cat > {d}/head.ext <<'EOF'
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName={san}
EOF
openssl req -newkey rsa:4096 -nodes -subj '/CN={config.DOMAIN}' \
    -keyout {config.HEAD_KEY} -out {d}/head.csr
openssl x509 -req -in {d}/head.csr -CA {config.HEAD_CA_CERT} -CAkey {config.HEAD_CA_KEY} \
    -CAcreateserial -days {config.CERT_DAYS} -extfile {d}/head.ext -out {config.HEAD_CERT}
rm -f {d}/head.csr {d}/head.ext
""", sudo=True)


def trust_own_ca() -> None:
    """The Head trusting the CA it just minted. Not a TOFU moment — setup wrote these exact bytes to
    disk a few lines ago — so there is nothing to confirm, only to announce, exactly as
    mesh.trust_own_headscale_cert argues for the coordinator's cert.

    Worth doing even though nothing on the Head strictly needs it: the Head box is where the Admin
    actually opens the Dashboard, so it is the first browser that would otherwise warn.
    """
    system.trust_ccc_ca(config.HEAD_CA_CERT.read_text())
    system.trust_ccc_ca_browsers(config.HEAD_CA_CERT.read_text())


def _secure_tls() -> None:
    """Owner and modes on both keypairs, re-asserted on EVERY `setup --head` — not only the run that
    minted them.

    The CA is minted once in a Head's life and then never again, and `teardown.remove_tls` is `--all`
    only, so a routine teardown does not clear it either. That makes anything which widens a key
    afterwards permanent, with no path back short of un-trusting the cluster everywhere.

    openssl writes keys 0600 itself, so a widened key means something outside setup touched it —
    unattributable after the fact, which is the point of re-asserting rather than just checking. A
    **Guise** could otherwise read the private half of the cert its own Head authenticates with, or
    worse, the CA key that would let it mint a cert for any name in the cluster.
    """
    for key in (config.HEAD_KEY, config.HEAD_CA_KEY):
        run(["chown", f"{config.HEAD_API_USER}:{config.HEAD_API_USER}", str(key)], sudo=True)
        run(["chmod", "0600", str(key)], sudo=True)   # the private halves: Mantle-only
    for crt in (config.HEAD_CERT, config.HEAD_CA_CERT):
        run(["chmod", "0644", str(crt)], sudo=True)   # the public ID cards: anyone may read


def build_spa() -> None:
    """Build the Dashboard SPA and stage it where the Head API service reads it. Node is needed only
    on the Head — install_packages(head=True) put it there; a donor Arm never builds this. Staged
    OUTSIDE the user's 0750 home — the service user can't traverse there — into a world-readable
    /opt path."""
    web_src = headapi.REPO_ROOT / "head" / "web"
    run(["npm", "ci"], cwd=str(web_src))
    run(["npm", "run", "build"], cwd=str(web_src))
    run(["rm", "-rf", str(config.HEAD_API_WEB_DIR)], sudo=True)
    run(["install", "-d", str(config.HEAD_API_WEB_DIR.parent)], sudo=True)
    run(["cp", "-r", str(web_src / "dist"), str(config.HEAD_API_WEB_DIR)], sudo=True)
    run(["chmod", "-R", "a+rX", str(config.HEAD_API_WEB_DIR.parent)], sudo=True)
    system.allow_tcp_port(config.HEAD_API_PORT, "ccc head api (LAN+mesh)")
    # Tide Pool's (docs/TODO.md) per-Spawn relay ports — one whole range, opened once, not one ufw rule
    # per Spawn. Same LAN+mesh reachability as the Head API itself: a Tide Pool's `ssh` connects here.
    system.allow_tcp_port_range(config.TIDEPOOL_PORT_BASE, config.TIDEPOOL_PORT_RANGE,
                                "ccc tide pool relay (LAN+mesh)")
    print(f"cuttle: Dashboard SPA built → {config.HEAD_API_WEB_DIR}")


def install_wsl_dashboard_forward(head_mesh_ip: str) -> None:
    """WSL-only: forward a local port to the Head API so the Arm's owner can open the Dashboard from
    their Windows browser at https://localhost/. The CCC mesh lives inside WSL and
    Windows can't reach the Head's mesh IP, but WSL2 forwards Windows localhost to WSL listeners —
    so a listener here bridges the gap. Socket-activated proxy (systemd-socket-proxyd, no package).
    Idempotent: `enable --now` on re-run is a no-op.

    HTTPS, not HTTP: the proxy is raw TCP so it carries TLS untouched, and the Head speaks nothing but
    TLS now. `localhost`/127.0.0.1 are in the cert's SANs for exactly this path (see provision_tls)."""
    socket_unit = f"""\
[Unit]
Description=CCC dashboard forward (WSL->Windows localhost bridge)

[Socket]
ListenStream={config.HEAD_API_PORT}

[Install]
WantedBy=sockets.target
"""
    # No [Install]: this is started on demand by the .socket (names match), never enabled directly.
    service_unit = f"""\
[Unit]
Description=CCC dashboard forward: proxy to the Head API over the mesh
Requires={config.DASH_FORWARD_SERVICE}.socket
After={config.DASH_FORWARD_SERVICE}.socket {config.CCC_TS_SERVICE}.service

[Service]
ExecStart={config.SOCKET_PROXYD} {head_mesh_ip}:{config.HEAD_API_PORT}
"""
    _write_root_file(config.DASH_FORWARD_SOCKET_UNIT, socket_unit)
    _write_root_file(config.DASH_FORWARD_SERVICE_UNIT, service_unit)
    run(["systemctl", "daemon-reload"], sudo=True)
    run(["systemctl", "enable", "--now", f"{config.DASH_FORWARD_SERVICE}.socket"], sudo=True)
    ui.result("dashboard (Windows)", headapi.head_url("localhost") + "/")
    ui.say("  [muted]self-signed cert — click through the browser warning once[/muted]")


def make_user_dirs() -> None:
    """The operator's own ~/.ccc — settings + credentials only. No images/ or spawns/ here: Pod images
    live in the world-readable CCC_IMAGES_DIR (the home-less Mantle must read them) and Spawn results
    live on the Head, fetched over the API.

    0700 explicitly: everything under ~/.ccc is this operator's alone (credentials, pinned certs, run
    logs), and the ambient umask is not a decision anyone made about it."""
    config.ensure_ccc_home()
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)


def verify() -> None:
    run(["sinfo", "-N", "-o", "%N %t %C %G"])


def _pick_coordinator_address() -> str:
    """First run, no --address: which of this box's addresses will Arms dial the coordinator at?

    `primary_ip()` answers a different question — where traffic leaves — and on a Head that is also on
    a tailnet it silently picks the LAN address, which excludes every remote Arm. Ask only when the
    choice is real: one candidate is not a decision, and a non-interactive run has no one to ask.

    The mesh's own addresses are never offered: an Arm dials this BEFORE it is on the mesh, so a mesh
    address is the one answer that cannot work.
    """
    default = system.primary_ip()
    mesh_net = ipaddress.ip_network(config.MESH_V4_PREFIX)
    others = [ip for ip in system.local_ipv4s()
              if ip != default and ipaddress.ip_address(ip) not in mesh_net]
    if not others or not ui.interactive():
        return default
    ui.say("  [muted]An Arm dials this before it is on the mesh, so it must be an address the "
           "donor's box can already reach.[/muted]")
    return ui.ask_select("Which address will Arms reach this Head's coordinator at?",
                         [default, *others], default=default)


def resolve_coordinator_address(address: str | None) -> str:
    """Settle the coordinator address, and confirm anything that would change it. No side effects.

    Called BEFORE the mesh step so its prompts are not buried inside a step frame, and so the operator
    answers before any daemon has been touched.

    The stored address outranks `primary_ip()`: this is a durable property of the Head, not a per-run
    argument. Without that fallback a bare `cuttle setup --head` — a routine re-run — silently reverts
    the coordinator to the LAN IP, invalidating every enrollment token and stranding every remote Arm.
    """
    stored = settings.load().get("mesh", {})
    addr = address or stored.get("head_address") or _pick_coordinator_address()
    old = stored.get("coordinator")
    new = f"https://{addr}:{config.HEADSCALE_PORT}"

    # Re-pointing the coordinator is a cluster-wide event, not a config tweak, so it is confirmed
    # rather than announced. Unreachable unless --address names something new: an unflagged re-run now
    # resolves to the stored address and compares equal.
    if old and old != new and ui.interactive():
        ui.warn(f"this re-points the mesh: {old} -> {new}")
        ui.hint("Every Arm re-registers against the new address, and every enrollment token already "
                "handed out names the old one and will stop working.")
        if not ui.ask_confirm("Re-point the coordinator?", False):
            raise ui.abort("coordinator left unchanged")
    # Warned where the address is CHOSEN, not where a token later carries it — this is the last moment
    # it costs nothing to change. Never a refusal: a fleet of LAN donors is the ordinary case.
    if mesh.lan_only(addr):
        ui.warn(f"{addr} is an address only this LAN can reach.")
        ui.hint("Arms on other networks will hang at `setup --arm` until it times out. If any donor "
                "is remote, re-run with --address=<addr> — this box's Tailscale IP, say.")
    return addr


def mesh_up_head(addr: str) -> str:
    """Self-host Headscale + join our own tailnet. `addr` = the coordinator's advertised host, already
    settled by resolve_coordinator_address. Returns the Head's mesh IP.

    Two addresses, two jobs: `addr` is where Arms reach the coordinator *before* they're on the
    mesh (public/DDNS); the returned mesh IP is where they reach slurmctld *after*.

    HTTPS: node keys and preauth keys cross this address before the mesh exists to
    protect them, so it carries a self-signed cert like the Head API's dashboard does — except
    `tailscaled`, not cuttle's own client, is what verifies it (see mesh.trust_own_headscale_cert /
    mesh.ensure_headscale_trusted).
    """
    server_url = f"https://{addr}:{config.HEADSCALE_PORT}"

    old = settings.load().get("mesh", {}).get("coordinator")
    if old and old != server_url:
        print(f"cuttle: coordinator changed ({old} -> {server_url}); re-registering on the mesh")

    # NB: forgetting stale mesh state is mesh.up_head's job, not ours — a changed address is only one
    # of the ways the daemon's state goes stale, and it was never the dangerous one. See its docstring.
    hs_ver, mesh_ip = mesh.up_head(server_url, addr, _write_root_file)
    # Persist for later Arm enrollment (join token) + reproducibility.
    data = settings.load()
    data["mesh"] = {"head_address": addr, "coordinator": server_url,
                    "headscale_version": hs_ver, "mesh_ip": mesh_ip}
    settings.save(data)
    print(f"cuttle: Head on mesh — mesh IP {mesh_ip}, coordinator {server_url}")
    return mesh_ip


def setup_head(address: str | None = None, update: bool = False) -> None:
    with ui.section("Updating the Head" if update else "Setting up the Head",
                    lambda: "control plane + co-located Arm"):
        system.prime_sudo()   # sudo as the box's first row, before any step rows grow the frame
        _setup_head(address, update=update)


def _setup_head(address: str | None = None, update: bool = False) -> None:
    """`update` is the **Update** duty: lay this Head down again from what it already recorded. Every
    question below is skipped in favour of the stored answer — see `update_this_box`."""
    make_user_dirs()
    if not update:
        settings.prompt_wandb()  # up front, so the rest runs unattended
    # Arm Features, also up front and for the same reason. Nothing is defined here — the catalog is
    # config.FEATURE_NAMES, built in — so the Head answers exactly the question a donor's box answers:
    # which of them does THIS box have. Its co-located Arm is an Arm like any other. Asked before
    # write_slurm_config below, which bakes this box's own Features into its NodeName line.
    head_features = _head_arm_features()
    head_licence = _head_licence_home()
    if not update:
        head_features = _prompt_arm_features(head_features)
        head_licence = _prompt_licence_home(head_licence) if head_features else ""
    preflight("head")
    address = resolve_coordinator_address(address)  # may prompt — keep it out of the step frames
    # Mesh first, before any apt work: (a) slurm.conf can't be rendered without the Head's mesh IP,
    # (b) if a mesh change ever breaks host DNS, a re-run still reaches the code that repairs it
    # rather than dying at `add-apt-repository`.
    with ui.step("Joining the mesh"):
        mesh_ip = mesh_up_head(address)
        system.allow_mesh_iface(config.CCC_TS_TUN)
    with ui.step("Adding package repositories"):
        add_repos(head=True)  # the apptainer PPA is added HERE and nowhere else in the fleet
    with ui.step("Installing Slurm, munge, Apptainer and Node"):
        before = install_packages(head=True)
        # /var/lib/ccc must exist before the .deb can be stored under it, and it is munge-owned — so
        # this waits until apt has created the `munge` user, i.e. until just now.
        make_registry_dir()
        install_apptainer(pin_apptainer_deb())  # the Head runs exactly what it serves its Arms
        record_installed(before)
    # Before write_slurm_config, which reads this box's Features back out of the registry to render its
    # NodeName line.
    with ui.step("Recording Arm Features"):
        # Unconditional, even for an empty declaration: both writers below are also what ERASE a
        # Feature this box just stopped declaring, so skipping them on `[]` would leave the mirror and
        # the env file describing what the operator removed.
        #
        # Prepared BEFORE the env file is rendered: render_features_env exports where the packages
        # and the mirrored install landed, and a file promising a directory that does not exist yet
        # is a Spawn that fails on an Arm whose setup said it succeeded.
        if head_features:
            ensure_guise_group()   # the mirrored licence key is group-owned by it, and this is early
        feature_profiles.prepare(head_features)
        _write_root_file(config.ARM_FEATURES_ENV,
                         enroll.render_features_env(head_features, head_licence))
        if head_licence:
            provision_licence_home(head_licence)
    with ui.step("Configuring Slurm"):
        # `features` explicitly, not left to the registry: on a FIRST setup this box has no registry row
        # yet (register_head_arm runs below, after the admin exists), so there would be nothing to read
        # and the Head would come up with no Feature= line until something re-rendered it.
        write_slurm_config(mesh_ip, head_features)
        start_daemons()
    with ui.step("Installing the Enroll API"):
        headapi.install_enroll_sudoers(_write_root_file)   # one grant: read-only `headscale nodes list`
        headapi.install_enroll(_write_root_file, mesh_ip)  # after munge: it serves munge.key
    with ui.step("Provisioning the Mantle and the auth store"):
        provision_head_auth()                    # ccc account + head.db + submission dirs (needs venv)
        # our co-located Arm is admin-owned (needs the admin), and carries the Features declared above
        enroll.register_head_arm(mesh_ip, head_features)
    with ui.step("Provisioning Guises"):
        # Every User's Unix identity on this box, including the reserved cluster one the enrollment
        # benchmark runs as. Back-fills on an upgrade, and needs the auth store above to exist first.
        auth.ensure_guise_accounts()
        headapi.install_slurm_sudoers(_write_root_file)  # act on a Spawn as its Guise — never as root
    with ui.step("Installing stage-in/out"):
        install_rsyncd(mesh_ip)                  # mesh-bound; needs ccc + its dirs first
    with ui.step("Building the Dashboard"):
        build_spa()                              # build + stage the Dashboard SPA (Node, Head-only)
    with ui.step("Issuing the Head certificate"):
        install_tls(mesh_ip)                     # must exist before uvicorn starts
        trust_own_ca()                           # so this box's own browser trusts the Dashboard
        # The Head answers on itself. Written after the cert, so the name and the SAN that covers it
        # arrive together and the Dashboard is never briefly resolvable but untrusted.
        system.write_hosts_entry("127.0.0.1")
    with ui.step("Starting the Head API and the Karma tick"):
        headapi.install_head(_write_root_file, mesh_ip)  # board + SPA + auth + submit, LAN+mesh
        headapi.install_karma(_write_root_file)  # ranks the queue by standing (same venv)
    with ui.step("Installing the nightly backup timer"):
        backup.install_timer(_write_root_file)   # head.db + arms.json + headscale db, see backup.py
    with ui.step("Installing the Head-box monitor"):
        monitor.install(_write_root_file)        # disk/services/rogue-usage, see monitor.py
        if not update:
            monitor.prompt_smtp(_write_root_file)  # SMTP creds for alerting; skippable
    with ui.step("Building the Pod image"):
        build_base_sif()                         # Mantle-readable, at the cluster-wide path
    with ui.step("Installing the Environment build tools"):
        install_env_tools()                      # uv + micromamba — every node builds (environments.py)
        provision_env_cache()                    # what every build on this box downloads into
    with ui.step("Building the benchmark environment"):
        build_bench_env()                        # the torch base.sif no longer carries; needs both above
    # The Head's own Arm is an Arm like any other, so it is priced like one — and its declared Arm
    # Features are proved like any other's. Last, because both are real Spawns: they need slurmd up,
    # the Pod image built, and rsyncd listening.
    #
    # Both are skipped outright by an Update, and skipped by `setup` too when the registry row already
    # carries the answer: an activation burns one of a finite number of vendor activations, and a
    # benchmark costs ten minutes and a card. Neither is something a re-run should silently spend.
    enroll.ensure_priced(system.hostname())
    enroll.activate_arm(system.hostname(), head_features)
    verify()
    lan = system.primary_ip()
    # Results, not `ui.kv` — kv writes straight to stdout, which inside a box is both uncounted (so
    # the erase lands in the wrong place) and unreadable by the time eleven phases have scrolled by.
    ui.result("dashboard", headapi.head_url(config.DOMAIN))
    ui.result("dashboard (by IP)", f"{headapi.head_url(lan)} · {headapi.head_url(mesh_ip)}")
    ui.result("coordinator", mesh_ip)
    ui.result("add an Arm", "cuttle arm authorize <name> --user <owner>")
    print(f"\ncuttle: this cluster signs its own certificates (no public domain = no CA can sign for\n"
          f"        it), and every box CCC installs on is told to trust the one authority that does.\n"
          f"        So {config.DOMAIN} loads clean on this box, on every Arm, and on any client set up\n"
          f"        with `cuttle setup --client` — no 'your connection is not private'.\n"
          f"        A phone or a borrowed laptop gets neither the name nor the trust: reach the\n"
          f"        Dashboard at the IP above and expect the warning there.")


def point_at_head() -> str:
    """Ask which Head this box talks to, and record it — what makes the box **pointed**.

    A **baked** bundle already wrote this (build-installer bakes config.yaml + the matching cert pin),
    so the prompt arrives pre-filled and is one Enter. A **generic** bundle leaves the box knowing
    nothing, and this is the only place that asks: `auth.login` reads the stored URL and, finding
    none, can only abort telling you to pass `--head` — which is a usable message for a typed `cuttle
    login` but a dead end for the wizard that is supposed to be doing the asking.

    Accepts a bare host as well as a full URL: `--head` takes a URL because it is a flag, but a person
    answering "which Head?" types an address.

    What is ASKED for is an address; what is STORED is always the **Cluster name**. The answer becomes
    this box's /etc/hosts entry, and from then on every surface — settings, the cert pin's filename,
    what `cuttle dashboard` opens — says `ccc.glados` on every box in the cluster. Answering with the
    name itself is fine on a box that already has the entry; the hosts write is then a no-op.
    """
    data = settings.load()
    # Re-ask on blank rather than storing it: the prompt is pre-filled with the baked URL, so an empty
    # answer means the field was cleared — and every branch below would turn "" into a syntactically
    # valid, permanently broken URL (`https://`) that only surfaces at the next login.
    while True:
        answer = ui.ask_text("Head address?", default=data.get("head") or "").strip().rstrip("/")
        if answer:
            break
        ui.warn("a Head address is required — the host this cluster's Head API answers on")
    # A scheme means they gave a URL and we take it as given (including a deliberate http://). A bare
    # `host:port` only needs the scheme; a bare host gets the Head API's default form.
    if "://" in answer:
        url = answer
    elif ":" in answer:
        url = f"https://{answer}"
    else:
        url = headapi.head_url(answer)
    u = urlparse(url)
    # Point the name at whatever they gave us, unless they gave us the name — in which case this box
    # already has an entry, and it is the only thing that could have resolved the answer they typed.
    # A hostname is resolved first: /etc/hosts maps an ADDRESS to a name, so `myhead` would be a line
    # that parses and never works.
    if u.hostname and u.hostname != config.DOMAIN:
        system.write_hosts_entry(socket.gethostbyname(u.hostname))
    # Swap the host for the name, keeping their scheme and port: a deliberate `http://` or a
    # non-default port is a choice this has no business overriding.
    data["head"] = url = u._replace(
        netloc=config.DOMAIN + (f":{u.port}" if u.port else "")).geturl()
    settings.save(data)
    return url


def setup_client(update: bool = False) -> None:
    """A submitter's box: point it at a Head, trust the cluster, then log in. Nothing else.

    No mesh and no Slurm — a client only ever talks to the Head API over HTTPS. It does now need sudo
    and one package, which it did not before: the **Cluster name** goes in /etc/hosts, and trusting the
    cluster's CA (so the Dashboard loads without a warning) means writing the OS trust store and, for
    Chromium-family browsers, importing with `certutil`. That package is not installed here: the trust
    itself installs it (system.ensure_certutil), which is the only way the client that never ran this
    command at all — `cuttle login` against an https Head trusts the CA too — gets it. Still kept
    separate from `setup --head/--arm`, because the gap between "one hosts line and a trusted CA" and
    "provision this machine" is the whole point of the role.

    No sudo on the box means this fails, and is meant to: a client that could not be given the name is
    a client on a different configuration from every other, and finding that out here beats finding it
    out at the first `cuttle run`.
    """
    if update:
        return _update_client()
    pointed: dict[str, str] = {}
    with ui.section("Set up this client", lambda: pointed.get("head", "")):
        make_user_dirs()
        pointed["head"] = point_at_head()
        # Before login, which is what would otherwise ask: `auth.login` calls tls.ensure_ca itself, so
        # this is only the difference between confirming a fingerprint inside the wizard and outside it.
        tls.ensure_ca(pointed["head"])
    who: dict[str, str] = {}
    with ui.section("Log in", lambda: who.get("as", "")):
        who["as"] = auth.login(None)
    ui.ok("Client ready. `cuttle run <script> --time <min> --vram <GB>` to submit.")


def _update_client() -> None:
    """The **Update** duty on a client. Almost nothing is installed on one, so almost nothing can go
    out of date — what CAN is the trust, and that is the whole of this.

    Two things are checked. The **Cluster name** must still resolve: a client's Head address lives
    only in that /etc/hosts line (`point_at_head` stores the URL under the name, never the address),
    so if the line is gone there is nothing here to restore it from and re-pointing is a `setup`. Then
    the pinned CA must still sign what the Head presents — the case a client actually hits, when the
    Head was rebuilt behind the same name and every request started failing verification.

    No login: whether you hold a **Session** is not a property of the box, and re-asking for a password
    is exactly the kind of question an Update does not get to ask. It reports, and leaves it to you.
    """
    url = settings.load()["head"]
    with ui.section("Updating this client", lambda: url):
        make_user_dirs()
        try:
            socket.gethostbyname(config.DOMAIN)
        except OSError:
            raise ui.abort(f"{config.DOMAIN} does not resolve on this box — its /etc/hosts line is "
                           f"gone, and a client keeps its Head's address nowhere else",
                           next_step="re-point it with `cuttle setup --client`")
        tls.refresh_pin(url)
    ui.result("head", url)
    ui.result("session", _session_line(url))


def _session_line(url: str) -> str:
    """Whether this box currently holds a **Session**, said in one line. Validated against the Head
    rather than read off disk: a token file proves only that you logged in once."""
    if not config.CREDENTIALS_FILE.exists():
        return "not logged in (`cuttle login`)"
    try:
        me = api.for_head().json("GET", "/me", token=config.CREDENTIALS_FILE.read_text().strip(),
                                 timeout=5)
    except Exception:
        return f"could not reach {url} to check (`cuttle whoami`)"
    return f"logged in as {me.get('username', '?')}"


def update_this_box() -> None:
    """The **Update** duty: lay this box's role down again from what it already recorded.

    Re-running `setup` after a code change works, but it re-asks every question setup ever asked — ten
    of them on a Head — and every answer is already on this box. This asks none of them. What it is
    for is the *installed* half: the API venv is rebuilt, the Dashboard re-bundled, every unit file and
    slurm.conf re-rendered, the certificate re-issued. What it is not for is the *decided* half: it
    never enrolls, never re-points, and never changes what this box is.

    The role is detected, never given — a flag choosing it would be a way to tell an Update to make
    this box something else, which is the one thing it does not do. A box that has never been set up
    has nothing to lay down again, and says so.

    Benchmarking and activation are not special-cased here: both already skip when the registry says
    they are done (`enroll.ensure_priced`, `enroll.activate_arm`), so an Update spends neither — and
    an Update of a box whose Feature was never proved still proves it, which is the state you would
    want fixed rather than carried forward.
    """
    # Wrapped, not merely trusted: every prompt in the two flows below is guarded by `update`, and
    # `ui.no_prompting` is what turns "we guarded them all" from a claim into a fact — including for
    # the prompt somebody adds to a shared step next year. sudo still asks; it is not a `ui` prompt,
    # and it is asking for permission rather than for an answer.
    if menu.is_head():
        with ui.no_prompting("`cuttle update`"):
            return setup_head(update=True)
    if menu.is_arm():
        # An Update is never an enrollment, so there is no token and no --re-enroll: the identity is
        # read back off this box (enroll.enrollment_from_state, called inside). When that read fails it
        # aborts there naming a fresh token, which is the right message and the only fix — so it is
        # deliberately NOT pre-checked and re-worded here.
        with ui.no_prompting("`cuttle update`"):
            return setup_arm(None, update=True)
    # NOT wrapped. A client's update has exactly one question in it — re-trusting a Head that was
    # rebuilt behind the same name — and that one is a trust decision no default can stand in for.
    if menu.is_pointed():
        return setup_client(update=True)
    raise ui.abort("nothing is set up on this box, so there is nothing to update",
                   next_step="run `cuttle setup` to decide what this box is")


def _prompt_arm_features(existing: list[dict] | None = None) -> list[dict]:
    """Ask which of CCC's built-in Arm Features (config.FEATURE_NAMES) this box has, and where each is
    installed — `[{"name", "path"}, ...]`, /enroll's shape. A y/n per Feature rather than a typed name:
    the catalog IS the only valid answer set (Head-side validation is the real gate, but there is no
    reason to let a typo reach it). The path is likewise re-asked until non-empty, mirroring the rule
    /enroll already enforces, so the failure lands next to the question.

    The same prompt on the Head and on a donor's box. Nothing here is Head-only any more: the Head does
    not own a catalog to define, it just has an Arm with Features like everyone else.

    On a re-setup, what this box already declared is offered as-is first. A full teardown+setup is the
    normal redeploy here, so re-walking an unchanged list is the common case, not the rare one — and a
    mistyped path on the way past is a Feature that silently resolves to nothing.

    Off-terminal (`--no-input`, a scripted setup) keeps `existing` rather than declaring none: a re-run
    that silently dropped this box's Features would take it out of Eligibility for every Spawn that
    needs them, which is not a safe default. A first setup still has nothing to keep.
    """
    existing = existing or []
    if not ui.interactive():
        return existing
    if existing:
        shown = ", ".join(f"{f['name']} ({f['path']})" for f in existing)
        ui.say(f"  [muted]already declared: {shown}[/muted]")
        if ui.ask_confirm("Keep these Arm Features?", True):
            return existing
    ui.say(f"  [muted]Which of these does THIS box have installed? "
           f"({', '.join(config.FEATURE_NAMES)})[/muted]")
    ui.hint("Yes only for what is really here — a Spawn asking for it can be sent nowhere else.")
    prior = {f["name"]: f["path"] for f in existing}
    prior_keys = {f["name"]: f.get("key") for f in existing}
    features = []
    for name in config.FEATURE_NAMES:
        if not ui.ask_confirm(f"Does this Arm have {name!r}?", name in prior):
            continue
        # Absolute, because the answer is read back on an Arm from /etc/ccc/features.env by a job
        # script whose working directory is the Spawn's staging dir — a relative path resolves
        # against something the operator answering here cannot see.
        var = re.sub(r"[^A-Za-z0-9]", "_", name).upper() + "_PATH"
        ui.hint(f"Where {name} lives on this box — absolute, e.g. /opt/{name}. Spawns see it as "
                f"${var}. Brought by the submitter instead? Point it at what this box does supply, "
                f"such as the licence directory below; the Head will not take an empty path.")
        # Re-asked here rather than left to the Head: /enroll rejects an empty path (enroll.py, "needs
        # a non-empty install path"), and a donor discovering that as a 422 mid-enrollment has already
        # sat through the whole wizard.
        while True:
            path = ui.ask_text(f"Install path for {name!r}:", prior.get(name, "")).strip()
            if not path:
                ui.warn(f"  {name!r} needs an install path — the Head refuses a declaration without one")
                continue
            # Checked against what the Feature's runtime actually opens (cuttle.features), not just for
            # non-emptiness. A path missing the vendor's assets or wheels is a Feature this box
            # advertises and cannot serve, and left to run time it surfaces as a stranger's Spawn dying
            # inside libgym.so — hours later, on a box its owner is not watching.
            missing = feature_profiles.check_install(name, path)
            if not missing:
                break
            ui.warn(f"  {path} does not look like a {name} install — nothing there for:")
            for m in missing:
                ui.say(f"    [muted]{m}[/muted]")
        feature_profiles.report(name, path)
        entry = {"name": name, "path": path}
        # The Feature's product key, for Features that have one. Asked here rather than assumed at the
        # default location: a box with this software installed was activated by whoever installed it,
        # so the key is already somewhere on it, and CCC is not going to guess. Never sent to the Head
        # — it is mirrored into this box's own CCC state, at 0640, and only this box ever reads it.
        p = feature_profiles.ARM_PROFILES.get(name)
        if p and p.licence_key:
            ui.hint(p.licence_key_help)
            while True:
                key = ui.ask_text(f"Path to {name}'s {p.licence_key}:",
                                  prior_keys.get(name) or str(Path(path) / p.licence_key)).strip()
                if key and Path(key).is_file():
                    break
                # Re-asked rather than accepted: a wrong path here fails the ACTIVATION, minutes later,
                # after a whole Environment build — and reads like a CCC fault rather than a typo.
                ui.warn(f"  no file at {key or '(nothing)'} — CCC cannot activate {name} without it")
            entry["key"] = key
        features.append(entry)
    return features


def _head_arm_features() -> list[dict]:
    """What the Head's own co-located Arm declared last time, for a re-setup to offer back.

    From the registry rather than settings (where a donor Arm keeps its copy): the Head's box has a
    registry row and no `arm` settings block — it never enrolls, so nothing ever wrote one.

    The **Licence** key paths come back from the rendered features.env instead, because the registry
    deliberately does not carry them (it is served to the whole cluster, and where a box keeps its key
    is that box's business). Same reason `_head_licence_home` reads that file.
    """
    features = (enroll.load_registry().get(system.hostname()) or {}).get("features") or []
    for f in features:
        # _SRC: where the OWNER's key is, not CCC's mirror of it — the prompt below re-copies from
        # whatever comes back, and a mirror offered as its own source would freeze at that copy.
        key = _features_env_value(
            f"{re.sub(r'[^A-Za-z0-9]', '_', f['name']).upper()}_LICENCE_KEY_SRC")
        if key:
            f["key"] = key
    return features


def _features_env_value(var: str) -> str:
    """One `export <var>=...` line's value out of the rendered features.env, or `""`.

    That file is the only durable record of anything Arm-local on this box — see
    `enroll.render_features_env`.
    """
    if not config.ARM_FEATURES_ENV.exists():
        return ""
    prefix = f"export {var}="
    for line in config.ARM_FEATURES_ENV.read_text().splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _head_licence_home() -> str:
    """The Head's own Licence directory as last recorded, read back out of the rendered features.env.

    That file is the only durable record on this box — a Licence path is Arm-local by design, so it is
    deliberately not in the registry the Head serves to the rest of the cluster.
    """
    return _features_env_value(config.LICENCE_HOME_VAR)


def _prompt_licence_home(existing: str = "") -> str:
    """Where this box keeps the machine-locked **Licence** state for its Feature software.

    ONE directory per Arm, not one per Feature: a licence is locked to hardware, and an Arm is one
    machine. Bound at a Spawn's Guise home inside the Pod (ccc_head_api.spawns.JOB_SCRIPT) because that
    is where a licence manager looks: TurboActivate resolves its state directory through
    getpwuid()->pw_dir and cannot be redirected by any env var.

    The operator names a directory and CCC creates it, as the home of config.LICENCE_USER. They then
    run the vendor's activation once as that account, which writes the state in. CCC still never reads,
    copies or parses what lands there — it only owns the empty directory and the identity.

    Hand-copying the files out of the operator's own home isn't viable: they're named from an
    obfuscated per-release token absent from both TurboActivate.dat and the vlearn wheels, so it can't
    be looked up, and a version bump renames every one of them — a stale copy would go unnoticed
    because the bind guard only tests that the directory exists.

    Must NOT be the operator's real home — it is bound into a Pod wholesale, so anything else living
    there is handed to the Spawn along with it. Giving it to a CCC-owned account makes that structural
    rather than advisory.
    """
    if not ui.interactive():
        return existing
    ui.say("  [muted]Some of that software is licensed to the machine, and looks for its activation "
           "files in the home directory of whoever runs it — never at $HOME, always at the home in "
           "/etc/passwd. So CCC gives the licence its own account, and mounts that account's home "
           "into a Spawn.[/muted]")
    if not ui.ask_confirm("Does this box hold a machine licence for any of that software?",
                          bool(existing)):
        return ""
    ui.hint(f"Name a directory that does not exist yet — CCC creates it as {config.LICENCE_USER}'s "
            f"home. You then activate the software ONCE as that account and the files land there; you "
            f"never have to find or copy them. Not your own home: all of it is mounted into every "
            f"Spawn that asks for a Feature.")
    return ui.ask_text("Licence directory for CCC to create (absolute, e.g. /opt/ccc/licence)",
                       existing).strip()


def setup_arm(token: str | None, re_enroll: bool = False, update: bool = False) -> None:
    """Enroll this box as an Arm, redeeming the **enrollment token** an Admin minted for it.

    The donor declares nothing. The token names the Arm and carries the one-time preauth key that
    admits it to the mesh — so there is no node-name prompt and no waiting for a human to approve a
    relayed auth-id. The Admin already decided, when they ran `cuttle arm authorize`.

    A None `token` is the re-setup of a box that already enrolled: everything the token carried is
    read back off this machine instead (enroll.enrollment_from_state), because a second setup is not
    a second enrollment and should not need a second secret.

    `re_enroll` forces a fresh mesh identity even if this box looks already-joined.
    """
    with ui.section("Updating this Arm" if update else "Setting up this Arm", lambda: "enrolled"):
        system.prime_sudo()   # sudo as the box's first row, before any step rows grow the frame
        _setup_arm(token, re_enroll=re_enroll, update=update)


def _setup_arm(token: str | None, re_enroll: bool = False, update: bool = False) -> None:
    """`update` is the **Update** duty (see `update_this_box`): every question below is answered from
    what this box already recorded. It always arrives with a None `token` — an Update is never an
    enrollment — so the identity comes back out of tailscaled's state like any re-setup's does."""
    make_user_dirs()
    # prime_sudo above is what makes the state read inside enrollment_from_state possible — the
    # coordinator lives in tailscaled's root-owned state file.
    t = enroll.decode_token(token) if token else enroll.enrollment_from_state()
    node_name = t["node_name"]
    if not update:
        settings.prompt_wandb()
    preflight("arm")

    with ui.step("Joining the mesh"):
        mesh_ip = mesh.up_arm(t["coordinator"], node_name, t["authkey"], _write_root_file,
                              re_enroll=re_enroll)
        system.allow_mesh_iface(config.CCC_TS_TUN)  # before slurmd: slurmctld dials us on 6818
    ui.say(f"  [muted]on the mesh as {node_name} — {mesh_ip}[/muted]")

    # Enroll IMMEDIATELY, before the long apt run: `cuttle arm authorize` is blocked on the Head
    # waiting for exactly this POST, and an unroutable Head is better learned now than after apt.
    # It reports our GPU devices (one /dev/nvidiaN per card natively, a single /dev/dxg under WSL2)
    # for the Head to author gres.conf, and returns the munge key — held in memory, since writing it
    # needs the `munge` user, which apt has not created yet.
    # Arm Features (docs/TODO.md): asked before /enroll so the declaration rides the same POST as the
    # hardware report. The catalog is config.FEATURE_NAMES, a local constant, so this needs nothing
    # from the Head. A re-setup offers what this box declared last time (settings) rather than
    # re-walking the list.
    prior = (settings.load().get("arm") or {})
    features = prior.get("features") or []
    licence_home = prior.get("licence_home", "")
    if not update:
        features = _prompt_arm_features(features)
        # Only asked when this box actually declared something to licence — a Licence with no Feature
        # to unlock is a directory nothing would ever bind.
        licence_home = _prompt_licence_home(licence_home) if features else ""

    with ui.step("Enrolling with the Head"):
        info = enroll.fetch_enrollment(t["mesh_ip"], node_name, mesh_ip, features=features)
        # The name can only be written now: it points at the Head's MESH address, which did not exist
        # until the join a few steps ago. Enrollment itself dialled the admin-supplied `--address`, so
        # the Cluster name is never on the bootstrap path — it only has to work from here on.
        system.write_hosts_entry(t["mesh_ip"])
        # The CA rode in on /enroll, over a channel the join token and Headscale's own view of the
        # caller already authenticated — so this box is spared the fingerprint ceremony entirely.
        if info.get("ca_pem"):
            tls.install_ca(headapi.head_url(config.DOMAIN), info["ca_pem"])

    # Mesh admission (the token) and Slurm admission are separate. The Admin's blocked `arm authorize`
    # sees our /enroll above and writes our gres.conf line within seconds — but if they weren't at
    # their desk and it timed out, they finish with `cuttle arm admit`. Resolved here, before the apt/
    # build block below, so that block is never interleaved with a wait on the Head.
    if not info["admitted"]:
        enroll.wait_for_admit(t["mesh_ip"], node_name)

    with ui.step("Adding package repositories"):
        add_repos()  # no apptainer PPA: this is a donor's box, and it gains no third-party repo
    with ui.step("Installing Slurm, munge and Apptainer"):
        before = install_packages(config.SLURMD_PKG)
        # Apptainer comes from the HEAD, not from Launchpad — the same .deb every other node in this
        # cluster installed, checksum-verified. `info` carried its version + sha256 back from /enroll.
        deb = enroll.fetch_apptainer_deb(t["mesh_ip"], info["apptainer"])
        install_apptainer(deb)
        deb.unlink()  # 35 MB, kept only as long as apt needed to read it
        record_installed(before)
        if system.is_wsl():
            configure_apptainer_wsl()  # after apptainer is installed, before base.sif is built
        enroll.write_munge_key(info["munge_key"])  # now that `munge` exists to own it

    data = settings.load()
    data["arm"] = {"node_name": node_name, "mesh_ip": mesh_ip, "head_mesh_ip": t["mesh_ip"],
                   "ctld_addr": info["ctld_addr"], "gpu_devices": system.gpu_devices(),
                   "features": features, "licence_home": licence_home}
    # Point the box at the Head by NAME — an Arm's owner is a User too, and until now enrolling left
    # them with no Head URL at all, so `cuttle login` on their own Arm said "no Head URL set".
    #
    # The name, not whatever was here before: a remote Arm is off the Head's LAN by definition, and a
    # bundle built on the Head bakes that LAN IP (installer.py). Overwriting is the point — the baked
    # address is the one that cannot work from here. The hosts entry written during enrollment resolves
    # the name to the Head's mesh IP, so this reaches the same place the old literal did, under the one
    # spelling every other box in the cluster also uses.
    data["head"] = headapi.head_url(config.DOMAIN)
    settings.save(data)

    # The Mantle must exist here BEFORE slurmd can accept a Spawn: slurmd setuid()s to the submitting
    # uid, which is the Head API's pinned CCC_UID. Without this account a Spawn scheduled onto this Arm
    # would run as an unknown numeric uid. Its Pod image must be readable by it, at the same path the
    # Head renders into the job script.
    with ui.step("Provisioning the Mantle"):
        provision_mantle()
        # The cluster's own **Guise**, which the enrollment benchmark below runs as. Created here
        # rather than left to the prolog because the benchmark is the FIRST Spawn this box ever runs,
        # and `Prolog=` may still be held back fleet-wide at that moment (enroll.arms_missing). Every
        # other Guise on this box is created lazily, by the prolog, when that User's work first lands.
        auth.provision_guise(config.GUISE_CLUSTER_UID)
    with ui.step("Building the Pod image"):
        build_base_sif()
    # Every Environment this Arm ever runs is built HERE, at job start, out of this box's own bandwidth
    # — a Head-built env had to cross the mesh, and that measured 29 minutes for a 4.6 GB torch tree
    # (config.ENV_TOOLS_DIR). So the tools and the cache are part of enrolling, not Head furniture.
    with ui.step("Installing the Environment build tools"):
        install_env_tools()
        provision_env_cache()
    # Before the benchmark below, which runs this env's interpreter. The long one: a cold torch install
    # over a donor's link. It is the same one-time cost as the base.sif build above, and it is paid here
    # rather than inside the benchmark job because that job has a 10-minute wall.
    with ui.step("Building the benchmark environment"):
        build_bench_env()

    with ui.step("Recording Arm Features"):
        # World-readable like the other rendered configs here: install paths aren't secret, and a
        # Spawn's payload (running as the home-less Mantle) needs to read this on every node. The
        # Licence PATH is likewise not secret — what is secret lives inside that directory, and is
        # bound only into Spawns that asked for a Feature (ccc_head_api.spawns.JOB_SCRIPT).
        #
        # Unconditional, as on the Head: an empty declaration on a re-run must erase the mirror and
        # rewrite the env file, not skip both and leave the old one standing.
        #
        # Prepared first, for the same reason as on the Head: the file about to be written names
        # where the packages and the mirrored install went.
        if features:
            ensure_guise_group()   # as on the Head — the mirrored licence key is group-owned by it
        feature_profiles.prepare(features)
        _write_root_file(config.ARM_FEATURES_ENV,
                         enroll.render_features_env(features, licence_home))
        if licence_home:
            provision_licence_home(licence_home)

    # No slurm.conf here — slurmd fetches it from slurmctld (configless) and self-registers (-Z).
    with ui.step("Starting slurmd"):
        _start_slurmd(node_name, info["ctld_addr"], [f["name"] for f in features])
    with ui.step("Reporting this Arm's Slurm hooks to the Head"):
        # ONLY now — both per-node files are on disk and slurmd is up. Until this lands the Head will
        # render neither `Prolog=` nor `JobContainerType=` (enroll.arms_missing, once per capability),
        # because a node named by either without its file fails its jobs and drains, and does not
        # recover when the file appears later.
        enroll.fetch_enrollment(t["mesh_ip"], node_name, mesh_ip, features=features,
                                provides=[config.ARM_CAP_PROLOG, config.ARM_CAP_TMPFS])
    with ui.step("Checking the Head can reach us"):
        # Only now is there something on 6818 to reach. Raises if the Head can't get back to us.
        enroll.check_head_can_reach_us(t["mesh_ip"])

    if system.is_wsl():
        with ui.step("Forwarding the Dashboard to Windows"):
            install_wsl_dashboard_forward(t["mesh_ip"])  # browse the Head via localhost

    ui.result("arm", node_name)
    ui.result("mesh ip", mesh_ip)
    ui.result("check it", "run `sinfo` on the Head")


def _start_slurmd(node_name: str, ctld_addr: str, feature_names: list[str] | None = None) -> None:
    for d, owner in ((config.SLURM_LOG_DIR, "slurm"), (config.SLURMD_SPOOL_DIR, "root")):
        run(["install", "-d", "-o", owner, "-g", owner, str(d)], sudo=True)
    # Configless serves the conf (which names Epilog=/Prolog=/JobContainerType=) but NOT the per-node
    # files it points at — those must be on the node, written before slurmd starts, or the first Spawn's
    # hook fails and drains this Arm. The prolog creates a **Guise** here; job_container.conf is what
    # the private-/tmp plugin refuses to start without. Both written unconditionally, ahead of the Head
    # ever naming them — harmless on a node whose slurm.conf still says nothing about them.
    _write_root_file(config.SLURM_EPILOG, templates.render_epilog(), mode="755")
    _write_root_file(config.SLURM_PROLOG, templates.render_prolog(), mode="755")
    _write_root_file(config.JOB_CONTAINER_CONF, templates.render_job_container_conf())
    # The same detection that was reported to /enroll, so the count claimed here and the gres.conf
    # File lines the Head wrote from that POST cannot disagree (slurmctld invalidates the node if
    # they do). `feature_names` are the same Arm Features just reported at /enroll.
    _write_root_file(config.SLURMD_DROPIN,
                     enroll.slurmd_dropin(node_name, ctld_addr, system.gpu_vram_mb(),
                                          len(system.gpu_devices()), feature_names))
    run(["systemctl", "daemon-reload"], sudo=True)  # the drop-in rewrites slurmd's ExecStart
    run(["systemctl", "enable", "slurmd"], sudo=True)
    run(["systemctl", "restart", "slurmd"], sudo=True)
