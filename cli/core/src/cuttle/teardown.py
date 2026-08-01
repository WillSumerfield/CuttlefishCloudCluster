"""`cuttle teardown` — reverse `setup`. `--local` (Head+Arm), `--arm` (remote Arm), or `--client`
(just this box's ~/.ccc). With no role flag the role is auto-detected; `--full` sweeps every role.

Default: full uninstall (daemons + packages + repos + Slurm config/state), but keep ~/.ccc
(preserves the multi-GB base.sif). Flags: --keep-packages (leave apt pkgs + repos),
--all (also remove ~/.ccc), --full (sweep everything, any role). Tolerant of partial state —
safe to re-run, and every removal step no-ops on a box that never had it.
"""

import shutil

from . import config, menu, settings, system, ui
from .system import run


def detect_role() -> str:
    """What this box is, for a flagless `teardown` (and the uninstaller). "local" for a Head, "arm"
    for a donor node, else "client". Head is the API venv (same predicate the menu uses); an Arm is
    marked by its saved settings or its slurmd drop-in."""
    if menu.is_head():
        return "local"
    if settings.load().get("arm") or config.SLURMD_DROPIN.exists():
        return "arm"
    return "client"

_SERVICES = [config.ENROLL_API_SERVICE, config.HEAD_API_SERVICE, config.KARMA_SERVICE,
             config.RSYNCD_SERVICE, "munge", "slurmctld", "slurmd"]
_ARM_SERVICES = ["slurmd", "munge"]  # an Arm runs no slurmctld / API / rsyncd / headscale
_PACKAGES = ["slurm-wlm", "slurmd", "apptainer", "nvidia-container-toolkit", "munge"]
# Every apt source file setup ADDED — and only those. The box's own sources.list/ubuntu.sources were
# never touched (see templates.UBUNTU_SNAPSHOT_LIST_TMPL), so there is nothing there to restore.
_APT_SOURCE_FILES = [
    "/etc/apt/sources.list.d/nvidia-container-toolkit.list",
    "/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg",
    str(config.UBUNTU_SNAPSHOT_LIST),
]


def stop_daemons() -> None:
    run(["systemctl", "disable", "--now", *_SERVICES], sudo=True, check=False)


def _leave_ccc_mesh() -> None:
    """Depart the mesh and remove CCC's OWN isolated tailscaled instance (targeted by socket) —
    NEVER the default/personal tailscaled or the shared `tailscale` package, since both Head and Arm
    may be administered over their personal tailnet. Shared by the Head and Arm teardowns."""
    # Drop the ufw rule setup added, before ccc0 disappears. Inert afterwards, but setup's mutations
    # are reversed exactly, the same reason purge_packages works off a recorded manifest.
    system.deny_mesh_iface(config.CCC_TS_TUN)
    run(["tailscale", f"--socket={config.CCC_TS_SOCKET}", "down"], sudo=True, check=False)
    run(["systemctl", "disable", "--now", config.CCC_TS_SERVICE], sudo=True, check=False)
    run(["rm", "-f", str(config.CCC_TS_UNIT)], sudo=True, check=False)
    run(["rm", "-rf", str(config.CCC_TS_STATEDIR)], sudo=True, check=False)
    # The coordinator's cert this box trusted into its OS CA store (Phase 9 #3,
    # mesh.trust_own_headscale_cert / mesh.ensure_headscale_trusted) — a host-wide trust decision,
    # not a cuttle-local file, so it needs the same undo `update-ca-certificates` gave it on the way
    # in. Runs on both roles: `_leave_ccc_mesh` is shared by the Head (remove_mesh) and every Arm.
    run(["rm", "-f", str(config.HEADSCALE_CA_PATH)], sudo=True, check=False)
    run(["update-ca-certificates"], sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


def remove_mesh(keep_packages: bool = False) -> None:
    """Head mesh teardown: leave the mesh (client) AND tear down the Headscale coordinator (CCC-only,
    so safe to purge). The Arm path calls `_leave_ccc_mesh` only — it runs no Headscale."""
    _leave_ccc_mesh()
    run(["systemctl", "disable", "--now", "headscale"], sudo=True, check=False)
    run(["rm", "-rf", str(config.HEADSCALE_CONF.parent), str(config.HEADSCALE_LIB)],
        sudo=True, check=False)
    if not keep_packages:
        run(["apt-get", "purge", "-y", "headscale"], sudo=True, check=False)
        # NOTE: `tailscale` package + repo left in place — shared with the personal daemon.


def remove_enroll_api() -> None:
    """Enroll API (Head-only, CCC-only). Drop its unit, its one sudo grant, the shared API venv
    (install_enroll built it), and the munge-owned registry dir."""
    run(["rm", "-f", str(config.ENROLL_API_UNIT), str(config.SUDOERS_MUNGE_HEADSCALE)],
        sudo=True, check=False)
    run(["rm", "-rf", str(config.API_VENV.parent), str(config.CCC_LIB)], sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


def remove_head_api() -> None:
    """Head API service (Head-only, CCC-only). Drop its ufw port rule, unit, the staged SPA, and the
    Mantle account. Already stopped/disabled by stop_daemons; head.db (under CCC_LIB) is swept by
    remove_enroll_api."""
    system.deny_tcp_port(config.HEAD_API_PORT)
    run(["rm", "-f", str(config.HEAD_API_UNIT), str(config.SUDOERS_CCC_SLURM)], sudo=True, check=False)
    run(["rm", "-rf", str(config.HEAD_API_WEB_DIR.parent)], sudo=True, check=False)  # /opt/ccc/head-api
    remove_mantle()  # created by provision_head_auth -> provision_mantle
    remove_guises()  # one per User, created by provision_head_auth -> ensure_guise_accounts
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


def remove_karma() -> None:
    """Karma tick (Head-only, CCC-only). Already stopped/disabled by stop_daemons; the ledger lives in
    head.db (under CCC_LIB) and is swept by remove_enroll_api."""
    run(["rm", "-f", str(config.KARMA_UNIT)], sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


def remove_backup() -> None:
    """Backup timer + its script (Head-only, CCC-only). The archives under BACKUP_DIR are the whole
    point of this feature and are NEVER touched here, even under --all — a teardown that deletes your
    only disaster-recovery copy defeats it."""
    run(["systemctl", "disable", "--now", f"{config.BACKUP_SERVICE}.timer"], sudo=True, check=False)
    run(["rm", "-f", str(config.BACKUP_SERVICE_UNIT), str(config.BACKUP_TIMER_UNIT),
         str(config.BACKUP_SCRIPT)], sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


def remove_rsyncd() -> None:
    """Stage-in/out daemon (Head-only, CCC-only — safe to remove wholesale). The service was already
    stopped/disabled by stop_daemons; drop its unit + config, the [bench] secret, and every open
    **Siphon**. RSYNCD_DIR lives under HEAD_STATE_DIR, which teardown removes wholesale anyway — it is
    named here too so the credentials go with the daemon that honoured them, whatever the order."""
    run(["rm", "-rf", str(config.RSYNCD_UNIT), str(config.RSYNCD_CONF),
         str(config.RSYNCD_BENCH_SECRETS), str(config.RSYNCD_DIR)], sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


def remove_slurm_config() -> None:
    run(["rm", "-rf",
         str(config.SLURM_CONF.parent),  # /etc/slurm
         str(config.SLURM_STATE_DIR),
         str(config.SLURMD_SPOOL_DIR),
         str(config.SLURM_LOG_DIR)],
        sudo=True, check=False)
    # Arm-only: the drop-in carrying the configless `-Z --conf --conf-server` flags. Take its whole
    # directory — an empty slurmd.service.d/ left behind would be litter in the distro's unit tree.
    run(["rm", "-rf", str(config.SLURMD_DROPIN.parent)], sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


def purge_packages() -> None:
    # Purge ONLY what setup recorded as newly installed — never pre-existing packages, and no
    # global `autoremove` (which would sweep unrelated system orphans, e.g. nvidia driver bits).
    if config.APT_MANIFEST.exists():
        pkgs = config.APT_MANIFEST.read_text().split()
        if not pkgs:
            ui.say("  [muted]apt manifest empty — setup installed nothing; skipping the purge[/muted]")
            return
    else:
        pkgs = _PACKAGES
        ui.say("  [muted]no apt manifest (pre-manifest setup?) — purging only the 4 explicit "
               "packages, no autoremove[/muted]")
    run(["apt-get", "purge", "-y", *pkgs], sudo=True, check=False)


def remove_repos() -> None:
    # The apptainer PPA is added on the Head only now, but the removal stays unconditional: it is
    # idempotent, and boxes set up before that change still have it.
    run(["add-apt-repository", "-y", "--remove", config.APPTAINER_PPA], sudo=True, check=False)
    run(["rm", "-f", *_APT_SOURCE_FILES], sudo=True, check=False)


def remove_user_dirs() -> None:
    shutil.rmtree(config.CCC_HOME, ignore_errors=True)


def remove_images() -> None:
    """The node's base.sif (built to /opt so the home-less Mantle can read it). Every node has one —
    Head and Arm alike — so both teardowns drop it, and both only under --all: it is multi-GB, and a
    config-only re-verify must not trigger an apptainer rebuild."""
    run(["rm", "-rf", str(config.CCC_IMAGES_DIR)], sudo=True, check=False)


def remove_tls() -> None:
    """The Head's self-signed cert. Head-only, and --all only: every client has PINNED this cert, so
    dropping it means the next setup mints a different one and every client refuses to connect until
    it re-pins. Same preserve-unless--all rule as base.sif, for a different reason."""
    run(["rm", "-rf", str(config.CCC_TLS_DIR)], sudo=True, check=False)


def _summarise(what: str, keep_packages: bool, kept_images: bool = True,
               kept_tls: bool | None = None) -> None:
    """What the run actually did — as results, not screen output.

    The box is erased when it closes, so a `print` inside it is either overdrawn or (under the
    alternate screen) held back and replayed detached from the thing it described. Results are the
    mechanism for outliving the box: they are printed in a panel once the terminal is handed back.

    `kept_tls=None` means this role never had a cert to keep — only the Head mints one.
    """
    ui.result("torn down", what)
    kept = [label for label, keep in (("packages + repos", keep_packages),
                                      ("base.sif", kept_images),
                                      ("the Head's TLS cert", kept_tls)) if keep]
    ui.result("kept", ", ".join(kept) if kept else "nothing — this box is clean")
    if kept_tls is False:
        # Every client PINNED that cert. The next setup mints a different one and every client
        # refuses to connect until it re-pins — worth saying at the moment it becomes true.
        ui.result("clients", "must re-pin: the TLS cert is gone, so the next setup mints a new one")


# --- Arm-only teardown -------------------------------------------------------

def stop_arm_daemons() -> None:
    run(["systemctl", "disable", "--now", *_ARM_SERVICES], sudo=True, check=False)


def undo_apptainer_wsl() -> None:
    """Reverse `setup.configure_apptainer_wsl` — strip the two additive lines. Only meaningful on a
    WSL Arm (guarded), so a native-Linux Arm's apptainer.conf is never touched. Idempotent: the sed
    deletes are no-ops if the lines are already gone."""
    if not system.is_wsl():
        return
    # `\#...#` picks # as the address delimiter so the /usr/lib/wsl path needs no escaping.
    run(["sed", "-i", f"\\#^bind path = {config.WSL_LIB_ROOT}$#d", str(config.APPTAINER_CONF)],
        sudo=True, check=False)
    run(["sed", "-i", f"/^{config.WSL_EXTRA_NVLIB}$/d", str(config.APPTAINER_NVLIBLIST)],
        sudo=True, check=False)
    ui.say(f"  [muted]reverted the WSL Apptainer config ({config.WSL_LIB_ROOT} bind + "
           f"{config.WSL_EXTRA_NVLIB})[/muted]")


def remove_wsl_dashboard_forward() -> None:
    """Reverse install_wsl_dashboard_forward. Tolerant of partial state (a native Arm never had it,
    so these are harmless no-ops) — no is_wsl guard needed, matching the rest of teardown."""
    run(["systemctl", "disable", "--now",
         f"{config.DASH_FORWARD_SERVICE}.socket", f"{config.DASH_FORWARD_SERVICE}.service"],
        sudo=True, check=False)
    run(["rm", "-f", str(config.DASH_FORWARD_SOCKET_UNIT), str(config.DASH_FORWARD_SERVICE_UNIT)],
        sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


def remove_munge_key() -> None:
    """The cluster-wide shared secret this Arm was seeded with. Purging the munge package would drop
    it too, but --keep-packages must not leave a live key behind on a departed Arm."""
    run(["rm", "-f", str(config.MUNGE_KEY)], sudo=True, check=False)


def remove_mantle() -> None:
    """The Spawn-execution identity `setup` created on this node (provision_mantle). Dropped on both
    the Head and an Arm — a departed node must not keep a cluster account at a pinned uid."""
    run(["userdel", config.HEAD_API_USER], sudo=True, check=False)
    run(["groupdel", config.HEAD_API_USER], sudo=True, check=False)


def remove_guises() -> None:
    """Every **Guise** this node accumulated — one per User whose Spawn ever landed here, created by
    the Slurm prolog as they arrived (temp/spawn-isolation.md Phase 3f).

    Found by their GECOS tag, which is the only reason it exists: the accounts are named after uids,
    not people, so nothing else on a donor's box distinguishes them from the box's own users. That
    matters most on the machine we care about most — a donor's — where the whole promise is that CCC
    leaves nothing behind.

    `userdel` with no `--remove`: a Guise is `useradd -M`, so there is no home to delete, and nothing
    of the donor's can be caught in the blast radius. The private group goes with the account (userdel
    removes a group that matches the user's name and has no other members); the shared group is dropped
    once explicitly, after the last member is gone.
    """
    r = run(["getent", "passwd"], sudo=True, capture_output=True, text=True, check=False)
    for line in (r.stdout or "").splitlines():
        f = line.split(":")
        if len(f) > 4 and f[0].startswith(config.GUISE_PREFIX) and f[4].startswith(config.GUISE_GECOS):
            run(["userdel", f[0]], sudo=True, check=False)
    run(["groupdel", config.GUISE_GROUP], sudo=True, check=False)


def teardown_arm(keep_packages: bool = False, all_: bool = False) -> None:
    """Reverse `setup --arm`. Leaves the mesh and wipes local state — but an Arm has no `headscale`
    binary, so it cannot delete its own node on the Head, which is left holding a live mesh address.

    **The Head operator must run `cuttle arm remove <name>`.** Not hygiene: `cuttle arm authorize`
    refuses a name whose mesh node still exists (it will not evict a machine as a side-effect), so a
    stale node blocks re-enrolling this box entirely. It also still holds the address that authorizes
    fetching the munge key.

    No Headscale/head-api/rsyncd here; an Arm runs none."""
    with ui.section("Tearing down this Arm", lambda: "removed"):
        system.prime_sudo()   # sudo as the box's first row, before any step rows grow the frame
        _teardown_arm(keep_packages=keep_packages, all_=all_)


def _teardown_arm(keep_packages: bool = False, all_: bool = False) -> None:
    with ui.step("Stopping Slurm and munge"):
        stop_arm_daemons()
    with ui.step("Leaving the mesh"):
        _leave_ccc_mesh()
    with ui.step("Removing Slurm's config and this node's integration"):
        remove_slurm_config()
        undo_apptainer_wsl()
        remove_wsl_dashboard_forward()
    with ui.step("Removing the cluster key and the CCC accounts"):
        remove_munge_key()
        remove_mantle()  # created by setup_arm.provision_mantle
        remove_guises()  # one per User whose Spawn ever landed here — created by the Slurm prolog
    if not keep_packages:
        with ui.step("Purging packages and repositories"):
            purge_packages()
            remove_repos()
    with ui.step("Removing this box's CCC state"):
        remove_user_dirs()      # ~/.ccc always (see teardown_local)
        if all_:
            remove_images()     # base.sif kept unless --all — multi-GB, and in /opt, not ~/.ccc
    _summarise("Arm", keep_packages, kept_images=not all_)
    # Not hygiene — see `teardown_arm`: the stale Head-side node BLOCKS re-enrolling this box, so it
    # is the one thing the operator must still do, and it belongs with the results rather than in a
    # box that erases itself.
    ui.result("still to do", "on the Head: `cuttle arm remove <name>` (or let re-enrollment reap it)")


def teardown_local(keep_packages: bool = False, all_: bool = False) -> None:
    with ui.section("Tearing down the cluster", lambda: "removed"):
        system.prime_sudo()   # sudo as the box's first row, before any step rows grow the frame
        _teardown_local(keep_packages=keep_packages, all_=all_)


def _teardown_local(keep_packages: bool = False, all_: bool = False) -> None:
    with ui.step("Stopping the cluster's services"):
        stop_daemons()
    with ui.step("Removing the Head's services"):
        remove_enroll_api()
        remove_head_api()
        remove_karma()
        remove_backup()
        remove_rsyncd()
    with ui.step("Tearing down the mesh"):
        remove_mesh(keep_packages=keep_packages)
    with ui.step("Removing Slurm's config and state"):
        remove_slurm_config()
    if not keep_packages:
        with ui.step("Purging packages and repositories"):
            purge_packages()
            remove_repos()
    with ui.step("Removing this box's CCC state"):
        remove_user_dirs()      # ~/.ccc (session, config, cert pins) always — leaving your login and
                                # pins behind after a teardown is what surprised people
        if all_:
            remove_images()     # base.sif is multi-GB and lives in /opt, not ~/.ccc; the Head cert
            remove_tls()        # is pinned by every client — both kept unless --all
    _summarise("local cluster", keep_packages, kept_images=not all_, kept_tls=not all_)


# --- client-only teardown ----------------------------------------------------

def teardown_client() -> None:
    """Reverse `setup --client`. A client only ever got ~/.ccc (config, credentials, cert pins) — no
    apt, no mesh, no Slurm, no sudo — so removing that dir is the whole job."""
    with ui.section("Removing client state", lambda: "removed"):
        with ui.step("Removing this box's CCC state"):
            remove_user_dirs()
        # Inside the box, deliberately: `_release` drains the facts when the section closes, so a
        # result registered after it is never printed at all.
        ui.result("removed", f"{config.CCC_HOME} — config, credentials, cert pins")
        ui.result("this box", "is a plain machine again; `cuttle login` sets it up")


# --- full sweep (any role, unknown/partial state) ----------------------------

def teardown_full(keep_packages: bool = False) -> None:
    """Sweep EVERYTHING cuttle could have installed, in ANY role, plus all local state. For a box in
    an unknown or partial state (a half-finished setup, or an old install whose role we can't tell).

    Runs every removal step — Head services AND Arm bits — regardless of what this box actually is.
    Each step is already tolerant of never having been applied (units that don't exist, packages not
    installed, files absent), so the belt-and-suspenders pass is safe on a plain client too."""
    with ui.section("Removing everything CCC", lambda: "removed"):
        system.prime_sudo()     # sudo as the box's first row, before any step rows grow the frame
        with ui.step("Stopping every CCC service"):
            stop_daemons()      # Head services (no-ops where the units never existed)
            stop_arm_daemons()  # Arm bits
        with ui.step("Removing the Head's services"):
            remove_enroll_api()
            remove_head_api()
            remove_karma()
            remove_rsyncd()
        with ui.step("Tearing down the mesh"):
            remove_mesh(keep_packages=keep_packages)
        with ui.step("Removing Slurm, the cluster key and the CCC accounts"):
            undo_apptainer_wsl()
            remove_wsl_dashboard_forward()
            remove_munge_key()
            remove_slurm_config()   # shared by both roles
            remove_mantle()
            remove_guises()
        if not keep_packages:
            with ui.step("Purging packages and repositories"):
                purge_packages()
                remove_repos()
        with ui.step("Removing this box's CCC state"):
            remove_user_dirs()  # local state — a full sweep always takes it
            remove_images()
            remove_tls()
    _summarise("everything CCC", keep_packages, kept_images=False, kept_tls=False)
