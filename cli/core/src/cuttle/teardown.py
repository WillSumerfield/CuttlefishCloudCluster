"""`cuttle teardown` — reverse `setup`. `--local` (Head+Arm), `--arm` (remote Arm), or `--client`
(just this box's ~/.ccc). With no role flag the role is auto-detected; `--full` sweeps every role.

Default: full uninstall (daemons + packages + repos + Slurm config/state), but keep the base +
benchmark images (multi-GB, frozen per release). Flags: --keep-packages (leave apt pkgs + repos),
--all (also remove the images, the registry store and the Head CA), --full (sweep everything, any role). Tolerant of partial state —
safe to re-run, and every removal step no-ops on a box that never had it.
"""

import glob
import shutil
import time

from . import (api, config, guest, incontainer, menu, mesh, podman, settings, sshconfig, system,
               tidepool, ui)
from .system import run


def detect_role() -> str:
    """What this box is, for a flagless `teardown` (and the uninstaller). "local" for a Head, "arm"
    for a donor node, else "client". Head is the API venv (same predicate the menu uses); an Arm is
    marked by its saved settings or its slurmd drop-in. A Head an earlier teardown left images on is
    still "local", so `teardown --all` there finishes the job."""
    if menu.is_head() or config.TIDEPOOL_KEPT.exists():
        return "local"
    if settings.load().get("arm") or config.SLURMD_DROPIN.exists():
        return "arm"
    return "client"

_SERVICES = [config.ENROLL_API_SERVICE, config.HEAD_API_SERVICE, config.KARMA_SERVICE,
             config.RSYNCD_SERVICE, f"{config.BACKUP_SERVICE}.timer", f"{config.MONITOR_SERVICE}.timer",
             "munge", "slurmctld", "slurmd"]
_ARM_SERVICES = ["slurmd", "munge"]  # an Arm runs no slurmctld / API / rsyncd / headscale
_PACKAGES = ["slurm-wlm", "slurmd", "slurm-client", "podman", "nvidia-container-toolkit", "munge"]
# Every apt source file setup ADDED — and only those. The box's own sources.list/ubuntu.sources were
# never touched (see templates.UBUNTU_SNAPSHOT_LIST_TMPL), so there is nothing there to restore.
_APT_SOURCE_FILES = [
    "/etc/apt/sources.list.d/nvidia-container-toolkit.list",
    "/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg",
    str(config.UBUNTU_SNAPSHOT_LIST),
    # Head-only, but removed unconditionally — rm -f no-ops where it was never added. Both carry the
    # ccc- prefix setup gave them, so a box that had its own NodeSource repo before CCC keeps it.
    str(config.NODESOURCE_LIST),
    config.NODESOURCE_KEYRING,
]


def cancel_dives() -> None:
    """End every Dive still queued or running, BEFORE the services that own them are stopped.

    Head only, and necessarily first: `scancel` asks slurmctld, so once `stop_daemons` has run there
    is nothing left to ask. Slurm gets to end its own jobs — epilogs run, allocations are released —
    rather than having the tree shot out from under it.

    Nothing did this before, and stopping slurmd does NOT end a running batch step: its process tree
    detaches and survives, so a Dive's sshd was still listening on its port hours after the
    cluster it belonged to had been torn down. `end_guise_processes` is the backstop that guarantees
    the ones this misses are gone — this is the graceful half.
    """
    run(["scancel", "--all"], sudo=True, check=False)


def stop_daemons() -> None:
    run(["systemctl", "disable", "--now", *_SERVICES], sudo=True, check=False)


def _leave_ccc_mesh() -> None:
    """Depart the mesh and remove CCC's OWN isolated tailscaled instance (targeted by socket) —
    NEVER the default/personal tailscaled or the shared `tailscale` package, since both Head and Arm
    may be administered over their personal tailnet. Shared by the Head and Arm teardowns."""
    # Drop the ufw rule setup added, before ccc0 disappears. Inert afterwards, but setup's mutations
    # are reversed exactly, the same reason purge_packages works off a recorded manifest.
    system.deny_mesh_iface(config.CCC_TS_TUN)
    # Likewise the ts-input exemption and its timer — and this one is NOT merely inert if left: it is a
    # rule inside the personal daemon's chain, on a box that may keep using that daemon long after CCC
    # is gone.
    mesh.remove_ts_exemption()
    run(["tailscale", f"--socket={config.CCC_TS_SOCKET}", "down"], sudo=True, check=False)
    run(["systemctl", "disable", "--now", config.CCC_TS_SERVICE], sudo=True, check=False)
    run(["rm", "-f", str(config.CCC_TS_UNIT)], sudo=True, check=False)
    run(["rm", "-rf", str(config.CCC_TS_STATEDIR)], sudo=True, check=False)
    # The coordinator's cert this box trusted into its OS CA store (mesh.trust_own_headscale_cert /
    # mesh.ensure_headscale_trusted) — a host-wide trust decision, not a cuttle-local file, so it
    # needs the same undo `update-ca-certificates` gave it on the way in. Runs on both roles:
    # `_leave_ccc_mesh` is shared by the Head (remove_mesh) and every Arm.
    run(["rm", "-f", str(config.HEADSCALE_CA_PATH)], sudo=True, check=False)
    run(["update-ca-certificates"], sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


def remove_mesh(keep_packages: bool = False) -> None:
    """Head mesh teardown: leave the mesh (client) AND tear down the Headscale coordinator (CCC-only,
    so safe to purge). The Arm path calls `_leave_ccc_mesh` only — it runs no Headscale."""
    _leave_ccc_mesh()
    system.deny_tcp_port(config.HEADSCALE_PORT)
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
    run(["rm", "-rf", "--one-file-system", str(config.API_VENV.parent), str(config.CCC_LIB)],
        sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


def remove_head_api() -> None:
    """Head API service (Head-only, CCC-only). Drop its ufw port rule, unit, the staged SPA, and the
    Mantle account. Already stopped/disabled by stop_daemons; head.db (under CCC_LIB) is swept by
    remove_enroll_api."""
    system.deny_tcp_port(config.HEAD_API_PORT)
    system.deny_tcp_port_range(config.TIDEPOOL_PORT_BASE, config.TIDEPOOL_PORT_RANGE)
    # Legacy range Dives used before moving out of the ephemeral port range (config.
    # TIDEPOOL_PORT_BASE) — drop once no Head set up before that move remains.
    system.deny_tcp_port_range(41000, 200)
    run(["rm", "-f", str(config.HEAD_API_UNIT), str(config.SUDOERS_CCC_SLURM)], sudo=True, check=False)
    run(["rm", "-rf", str(config.HEAD_API_WEB_DIR.parent)], sudo=True, check=False)  # /opt/ccc/head-api
    remove_mantle()  # created by provision_head_auth -> provision_mantle
    remove_licence_account()  # before remove_guises: it is in GUISE_GROUP
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


def remove_monitor() -> None:
    """Head-box health monitor: timer + service + script (monitor.install). Its state and SMTP
    credentials live under CCC_LIB, which `remove_enroll_api` takes wholesale.

    Left behind, this is worse than clutter: the unit runs as the Mantle, and every teardown deletes
    that account — so the timer goes on firing and failing every 5 minutes on a box the operator was
    told is clean."""
    run(["systemctl", "disable", "--now", f"{config.MONITOR_SERVICE}.timer"], sudo=True, check=False)
    run(["rm", "-f", str(config.MONITOR_SERVICE_UNIT), str(config.MONITOR_TIMER_UNIT),
         str(config.MONITOR_SCRIPT)], sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


def remove_rollout() -> None:
    """The Head's served cuttle build and the timer that pushes it to Arms (dist.py, rollout.py). Its
    state lives under CCC_LIB, which `remove_enroll_api` takes wholesale."""
    run(["systemctl", "disable", "--now", f"{config.ROLLOUT_SERVICE}.timer"], sudo=True, check=False)
    run(["rm", "-f", str(config.ROLLOUT_SERVICE_UNIT), str(config.ROLLOUT_TIMER_UNIT),
         str(config.ROLLOUT_SCRIPT)], sudo=True, check=False)
    run(["rm", "-rf", str(config.CUTTLE_DIST_DIR)], sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


def remove_head_services() -> None:
    """Every unit `setup --head` installs. ONE list, because there were two — `_teardown_local` and
    `teardown_full` each hand-listed the Head's services, and they had already drifted: the backup
    timer was removed by one and not the other, and nothing anywhere removed the monitor."""
    remove_enroll_api()
    remove_head_api()
    remove_karma()
    remove_backup()
    remove_monitor()
    remove_rollout()
    remove_rsyncd()


def remove_rsyncd() -> None:
    """Stage-in/out daemon (Head-only, CCC-only — safe to remove wholesale). The service was already
    stopped/disabled by stop_daemons; drop its unit + config, the [bench] secret, and every Dive's
    module."""
    run(["rm", "-rf", str(config.RSYNCD_UNIT), str(config.RSYNCD_CONF),
         str(config.RSYNCD_BENCH_SECRETS), str(config.DIVE_RSYNCD)],
        sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


def remove_slurm_config() -> None:
    # job_container.conf goes with /etc/slurm, but its BasePath tree does not: the plugin creates that
    # itself and leaves it behind at shutdown (man 5 job_container.conf), so a torn-down donor box would
    # keep an empty per-job mount tree forever. Every caller stops slurmd first, which is required —
    # slurmd holds BasePath mounted `private` for as long as it runs.
    run(["rm", "-rf",
         str(config.SLURM_CONF.parent),  # /etc/slurm
         str(config.JOB_CONTAINER_ROOT),
         str(config.SLURM_STATE_DIR),
         str(config.SLURMD_SPOOL_DIR),
         str(config.SLURM_LOG_DIR)],
        sudo=True, check=False)
    # Arm-only: the drop-in carrying the configless `-Z --conf --conf-server` flags. Take its whole
    # directory — an empty slurmd.service.d/ left behind would be litter in the distro's unit tree.
    run(["rm", "-rf", str(config.SLURMD_DROPIN.parent)], sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)
    _sweep_job_tmp()


# The per-job files a Dive leaves in /tmp: its scratch dir (remote branch), its sbatch sink, and an
# enrolment benchmark's log. All named after a job id, and it is `remove_slurm_config` above that makes
# a job id ambiguous — dropping the state dir resets Slurm's counter, so the NEXT cluster issues 1, 2, 3
# over the top of a previous one's leftovers. Those leftovers belong to accounts the next cluster's
# **Guise**s cannot write to, and a sink slurmd cannot open kills the batch step outright (see
# the job script's `--output` sink; found on hardware 2026-08-02). So the sweep belongs exactly here,
# beside the reset that causes the collision.
#
# `[0-9]*` and not `*`: only job-id-suffixed names are ours to take. Nothing else in /tmp starting
# `ccc-` is (a Guise's own files are `ccc-u<uid>-…`), and a teardown must never widen its blast radius.
_JOB_TMP_GLOBS = ("/tmp/ccc-[0-9]*", "/tmp/ccc-bench-[0-9]*")


def _sweep_job_tmp() -> None:
    stale = [p for g in _JOB_TMP_GLOBS for p in glob.glob(g)]
    if stale:
        run(["rm", "-rf", *stale], sudo=True, check=False)


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
    run(["rm", "-f", *_APT_SOURCE_FILES], sudo=True, check=False)


def remove_user_dirs() -> None:
    # No `revoke_session` here: it is the FIRST thing every teardown path does, not the last. See there.
    # Before ~/.ccc goes: the aliases in the User's own ~/.ssh name keys that live under it, so
    # removing one without the other leaves Host entries pointing at nothing (sshconfig.remove).
    sshconfig.remove()
    shutil.rmtree(config.CCC_HOME, ignore_errors=True)


def revoke_session() -> None:
    """Tell the Head to drop this box's Session. The FIRST thing every teardown path does.

    Deleting the session file only makes the token unreachable HERE; the Head's own record of the login
    stays valid for the rest of its lifetime, exactly the thing `cuttle logout` avoids.

    **First, because teardown spends its whole run destroying the road to the Head.** Three separate
    steps each cut it: `stop_daemons` (on a Head box the API is local, and it is stopped in step one),
    `_leave_ccc_mesh` (an Arm may reach the Head only over the mesh) and `untrust_and_unname` (the
    Cluster name in /etc/hosts, and the pinned authority the TLS dial needs). Called from
    `remove_user_dirs` — the last step — this could never once have succeeded on a Head.

    **The api layer directly, never `auth._api`.** That is the *command* layer: it translates a failure
    into a verdict via `ui.abort`, which prints and records a ✗ before it raises. Swallowing the
    exception here never swallowed the message, so a teardown that had removed everything correctly
    still ended under a red "Warnings" panel saying the Head was unreachable — while the caller's own
    `except` hid the one thing that would have explained it.

    Best-effort by necessity — this same box may be the Head, or its Head may be long gone — so a
    failure reaches the run log and nothing else, and the Session's own expiry remains the backstop.
    """
    if not config.SESSION_FILE.exists():
        return
    try:
        api.for_head().json("POST", "/logout", token=config.SESSION_FILE.read_text().strip(),
                            timeout=api.TIMEOUT_BEST_EFFORT)
    except Exception as e:
        # `_log`, not `warn`/`fail`: see above. The log directory goes with ~/.ccc moments later, so in
        # practice this survives in the terminal's scrollback — which is where anyone asking why a
        # Session outlived a teardown is looking anyway.
        ui._log(f"CAUTION session not revoked ({e}) — it expires on its own")


def remove_arm_state() -> None:
    """CCC's own state on a donor's box: the mirrored Feature install (up to ~1 GB), the mirrored
    Licence key beside it, the Environment build cache (up to config.NODE_ENV_CACHE_MAX_GB), and the
    /opt/ccc that images left behind.

    Only `remove_enroll_api` cleared CCC_LIB, and an Arm never runs the Enroll API — so a
    Feature-declaring donor who ran plain `cuttle teardown --arm` kept the whole mirror, against the
    promise that CCC leaves nothing behind.

    NOT the Licence *directory*: that is the Licence account's home, and it holds machine activations
    the vendor may grant a finite number of times. See `remove_licence_account`."""
    run(["rm", "-rf", "--one-file-system", str(config.CCC_LIB)], sudo=True, check=False)
    prune_opt_dir()


def prune_opt_dir() -> None:
    """Drop /opt/ccc once its children are gone. `rmdir`, never `rm -rf`: whatever is still in there
    was kept deliberately (the registry store and the TLS cert both survive a teardown without
    --all)."""
    run(["rmdir", "--ignore-fail-on-non-empty", str(config.CCC_OPT)], sudo=True, check=False)


def remove_containers(all_: bool) -> None:
    """CCC's Podman state (podman.remove): job containers, registry units, CDI hook, launcher, creds.
    Before the package purge — it needs `podman`. Images + registry store only under --all: they are
    frozen per release and multi-GB, and --all is the only way a changed image reaches this box."""
    podman.remove(all_)
    incontainer.remove()


def remove_env_tools() -> None:
    """The pinned uv + micromamba (config.ENV_TOOLS_DIR). Every node has them — every node builds
    Environments now — so both teardowns drop them.

    UNCONDITIONAL, unlike the images: these are ~50MB, `setup` re-fetches and re-verifies them
    on every run regardless, and nothing downstream is pinned to the copy on disk. Keeping them past a
    teardown would be a pure leak, which is what it was until this existed."""
    run(["rm", "-rf", str(config.ENV_TOOLS_DIR)], sudo=True, check=False)


def remove_tls() -> None:
    """The Head's CA and serving cert. Head-only, and --all only: every client has PINNED that CA and
    every box in the cluster has installed it, so dropping it means the next setup mints a different
    one and nothing trusts this Head until each of them is re-trusted. Same preserve-unless--all rule
    as the images, for a different reason."""
    run(["rm", "-rf", str(config.CCC_TLS_DIR)], sudo=True, check=False)


def untrust_and_unname() -> None:
    """Undo what setup did to this box's *host* on behalf of the cluster: the /etc/hosts line carrying
    the **Cluster name**, and the CA in all three trust stores.

    Every role, every teardown, `--all` or not — deliberately unlike `remove_tls` above. That one keeps
    the Head's own keypair so a redeploy does not un-trust the fleet; this one is about a box that has
    left. A machine still resolving `ccc.<name>` to a Head it no longer talks to, and still trusting a
    CA whose private key lives there, is exactly the residue a teardown exists to remove. Same
    reasoning, and the same unconditional placement, as the headscale CA in `_leave_ccc_mesh`.

    On a guest, the OS around this box may hold both as well — and cannot be made to give them up
    from in here, so `guest.withdraw` hands its owner the means instead. It goes FIRST: the address
    baked into the script it leaves behind is read from the very /etc/hosts line the next call
    removes.
    """
    guest.withdraw()
    system.remove_hosts_entry()
    system.run(["rm", "-f", str(config.CLUSTER_NAME_FILE), str(config.ARM_UPDATER_RECORD)], sudo=True,
               check=False)
    system.untrust_ccc_ca()


def _summarise(what: str, keep_packages: bool, kept_images: bool = True,
               kept_tls: bool | None = None) -> None:
    """What the run actually did — as results, not screen output.

    The box is erased when it closes, so a `print` inside it is either overdrawn or (under the
    alternate screen) held back and replayed detached from the thing it described. Results are the
    mechanism for outliving the box: they are printed in a panel once the terminal is handed back.

    `kept_tls=None` means this role never had an authority to keep — only the Head mints one.
    """
    ui.result("torn down", what)
    kept = [label for label, keep in (("packages + repos", keep_packages),
                                      ("the base + benchmark images", kept_images),
                                      ("the cluster's certificate authority", kept_tls)) if keep]
    ui.result("kept", ", ".join(kept) if kept else "nothing — this box is clean")
    if kept_tls is False:
        # Every box in the cluster INSTALLED that CA and every client PINNED it. The next setup mints
        # a different one and none of them trusts this Head until each is re-trusted — worth saying at
        # the moment it becomes true, because nothing else will say it.
        ui.result("every other box", "must re-trust: the CA is gone, so the next setup mints a new "
                                     "one. `cuttle setup --client` (or --arm) on each of them")


# --- Arm-only teardown -------------------------------------------------------

def stop_arm_daemons() -> None:
    run(["systemctl", "disable", "--now", *_ARM_SERVICES], sudo=True, check=False)


def remove_munge_key() -> None:
    """The cluster-wide shared secret this Arm was seeded with. Purging the munge package would drop
    it too, but --keep-packages must not leave a live key behind on a departed Arm."""
    run(["rm", "-f", str(config.MUNGE_KEY)], sudo=True, check=False)


def remove_mantle() -> None:
    """The Dive-execution identity `setup` created on this node (provision_mantle). Dropped on both
    the Head and an Arm — a departed node must not keep a cluster account at a pinned uid."""
    run(["userdel", config.HEAD_API_USER], sudo=True, check=False)
    run(["groupdel", config.HEAD_API_USER], sudo=True, check=False)


def remove_guises() -> None:
    """Every **Guise** this node accumulated — one per User whose Dive ever landed here, created by
    the Slurm prolog as they arrived.

    Found by their GECOS tag, which is the only reason it exists: the accounts are named after uids,
    not people, so nothing else on a donor's box distinguishes them from the box's own users. That
    matters most on the machine we care about most — a donor's — where the whole promise is that CCC
    leaves nothing behind.

    `userdel` with no `--remove`: a Guise is `useradd -M`, so there is no home to delete, and nothing
    of the donor's can be caught in the blast radius. The private group goes with the account (userdel
    removes a group that matches the user's name and has no other members); the shared group is dropped
    once explicitly, after the last member is gone.
    """
    end_guise_processes()   # userdel refuses an account something is still running as
    for name in guise_names():
        run(["userdel", name], sudo=True, check=False)
    run(["groupdel", config.GUISE_GROUP], sudo=True, check=False)


def guise_names() -> list[str]:
    """Every **Guise** account on this box, by name.

    Found by their GECOS tag, which is the only reason it exists: the accounts are named after uids,
    not people, so nothing else on a donor's box distinguishes them from the box's own users. That
    matters most on the machine we care about most — a donor's — where the whole promise is that CCC
    leaves nothing behind.
    """
    r = run(["getent", "passwd"], sudo=True, capture_output=True, text=True, check=False)
    names = []
    for line in (r.stdout or "").splitlines():
        f = line.split(":")
        if len(f) > 4 and f[0].startswith(config.GUISE_PREFIX) and f[4].startswith(config.GUISE_GECOS):
            names.append(f[0])
    return names


def end_guise_processes(grace: float = 5.0) -> None:
    """Make sure nothing is still running as a **Guise**, so the `userdel` above can remove one.

    `userdel` REFUSES an account with a live process ("currently used by process") and every call in
    this module is check=False, so a surviving Dive used to make its Guise silently outlive the
    teardown. The next `setup` then found that account already there, `useradd --groups` no-opped on
    it, and the freshly re-created GUISE_GROUP came back WITHOUT it — leaving that one Guise off the
    Runas list of the Head's sudoers rule, so every Dive it submitted was refused by sudo. The whole
    chain is silent, and surfaces days later as one User who cannot run anything.

    TERM, then KILL after a grace period. `cancel_dives` has usually already done this the polite
    way — what reaches here is what Slurm no longer knows about.
    """
    names = guise_names()
    if not names:
        return
    # pkill exits 0 only if it actually signalled something. Waiting is conditional on that: a cleanly
    # stopped cluster has nothing running as a Guise, which is the common case, and an unconditional
    # grace period would spend it doing nothing on every teardown.
    signalled = [run(["pkill", "-TERM", "-u", name], sudo=True, check=False).returncode == 0
                 for name in names]
    if not any(signalled):
        return
    time.sleep(grace)
    for name in names:
        run(["pkill", "-KILL", "-u", name], sudo=True, check=False)


def remove_licence_account() -> None:
    """Drop the **Licence** account, and deliberately NOT its home.

    Called beside `remove_mantle`, which every teardown path already runs BEFORE `remove_guises` —
    and that order is load-bearing here, because this account is a GUISE_GROUP member and the
    `groupdel` at the end of `remove_guises` refuses a group that still has one.

    `userdel` with no `--remove`, for a reason that does not apply to a Guise: this account's home is
    the Licence directory, and what is in it is a machine activation the operator may only be able to
    perform a limited number of times. Teardown is routine here — a full teardown+setup is the normal
    redeploy — so destroying activation state on the way past would spend a real, finite resource on
    a routine operation. The next setup re-creates the account pointing at the same directory and the
    activation is simply still there.

    The directory is left group-owned by GUISE_GROUP, whose gid is pinned (config.GUISE_GROUP_GID), so
    it still resolves to the same group after the next setup re-creates it.
    """
    run(["userdel", config.LICENCE_USER], sudo=True, check=False)


def teardown_arm(keep_packages: bool = False, all_: bool = False) -> None:
    """Reverse `setup --arm`. Leaves the mesh and wipes local state — but an Arm has no `headscale`
    binary, so it cannot delete its own node on the Head, which is left holding a live mesh address.

    **The Head operator must run `cuttle arm remove <name>`.** Not hygiene: `cuttle arm authorize`
    refuses a name whose mesh node still exists (it will not evict a machine as a side-effect), so a
    stale node blocks re-enrolling this box entirely. It also still holds the address that authorizes
    fetching the munge key.

    No Headscale/head-api/rsyncd here; an Arm runs none.

    And no `cancel_dives`, deliberately: scancel asks the CONTROLLER, so a donor taking their own box
    back would end Dives running on everyone else's machines too. `remove_guises` ends the ones
    running HERE, and the Head marks this node down like any other that went away."""
    with ui.section("Tearing down this Arm", lambda: "removed"):
        system.prime_sudo()   # sudo as the box's first row, before any step rows grow the frame
        _teardown_arm(keep_packages=keep_packages, all_=all_)


def _teardown_arm(keep_packages: bool = False, all_: bool = False) -> None:
    revoke_session()      # before the mesh goes: it may be this box's only road to the Head
    with ui.step("Stopping Slurm and munge"):
        stop_arm_daemons()
    with ui.step("Leaving the mesh"):
        _leave_ccc_mesh()
    with ui.step("Removing Slurm's config and this node's integration"):
        remove_slurm_config()
    with ui.step("Removing the cluster key and the CCC accounts"):
        remove_munge_key()
        remove_mantle()  # created by setup_arm.provision_mantle
        remove_licence_account()  # before remove_guises: it is in GUISE_GROUP
        remove_guises()  # one per User whose Dive ever landed here — created by the Slurm prolog
    with ui.step("Removing the cluster name and its certificate authority"):
        untrust_and_unname()    # BEFORE the purge below — it needs certutil, which the purge takes
    with ui.step("Removing CCC's containers" + (" and images" if all_ else "")):
        remove_containers(all_)  # BEFORE the purge below — it needs podman
    if not keep_packages:
        with ui.step("Purging packages and repositories"):
            purge_packages()
            remove_repos()
    with ui.step("Removing this box's CCC state"):
        remove_user_dirs()      # ~/.ccc always (see teardown_local)
        remove_arm_state()      # the Feature mirror + the env build cache, both under CCC_LIB
        remove_env_tools()      # uv + micromamba: re-fetched by every setup, so never worth keeping
        prune_opt_dir()         # after the /opt removals above: with those gone, /opt/ccc is empty here
    _summarise("Arm", keep_packages, kept_images=not all_)
    # Not hygiene — see `teardown_arm`: the stale Head-side node BLOCKS re-enrolling this box, so it
    # is the one thing the operator must still do, and it belongs with the results rather than in a
    # box that erases itself.
    ui.result("still to do", "on the Head: `cuttle arm remove <name>` (or let re-enrollment reap it)")
    if system.is_wsl():
        ui.result("and in Windows", f"(admin Command Prompt) {system.wsl_keepalive_cmds()[1]}")


def teardown_local(keep_packages: bool = False, all_: bool = False) -> None:
    with ui.section("Tearing down the cluster", lambda: "removed"):
        system.prime_sudo()   # sudo as the box's first row, before any step rows grow the frame
        _teardown_local(keep_packages=keep_packages, all_=all_)


def _teardown_local(keep_packages: bool = False, all_: bool = False) -> None:
    revoke_session()      # before stop_daemons: on a Head box the API it asks is the one being stopped
    with ui.step("Ending any Dives still running"):
        cancel_dives()   # before stop_daemons: scancel needs slurmctld
    with ui.step("Stopping the Tidepools" + (" and deleting their images" if all_ else "")):
        tidepool.teardown(all_)  # unmounted BEFORE anything rm -rf's CCC_LIB, where they mount
    with ui.step("Stopping the cluster's services"):
        stop_daemons()
    with ui.step("Removing the Head's services"):
        remove_head_services()
    with ui.step("Tearing down the mesh"):
        remove_mesh(keep_packages=keep_packages)
    with ui.step("Removing Slurm's config and state"):
        remove_slurm_config()
    with ui.step("Removing the cluster name and its certificate authority"):
        untrust_and_unname()    # BEFORE the purge below — it needs certutil, which the purge takes
    with ui.step("Removing CCC's containers" + (" and images" if all_ else "")):
        remove_containers(all_)  # BEFORE the purge below — it needs podman
    if not keep_packages:
        with ui.step("Purging packages and repositories"):
            purge_packages()
            remove_repos()
    with ui.step("Removing this box's CCC state"):
        remove_user_dirs()      # ~/.ccc (session, config, cert pins) always — leaving your login and
                                # pins behind after a teardown is what surprised people
        remove_env_tools()      # uv + micromamba: re-fetched by every setup, so never worth keeping
        if all_:
            remove_tls()        # pinned by every client — kept unless --all
    _summarise("local cluster", keep_packages, kept_images=not all_, kept_tls=not all_)


# --- client-only teardown ----------------------------------------------------

def teardown_client() -> None:
    """Reverse `setup --client`: ~/.ccc (config, session token, the pinned CA), plus the two things setup
    put outside it — the **Cluster name** in /etc/hosts and the CA in this box's trust stores.

    Those two are why this now primes sudo, which a client teardown never used to need. `libnss3-tools`
    is deliberately left: nothing records that CCC installed it, and it is a general-purpose package
    something else may since have come to depend on.
    """
    with ui.section("Removing client state", lambda: "removed"):
        system.prime_sudo()   # sudo as the box's first row, before any step rows grow the frame
        revoke_session()      # before untrust_and_unname takes the Cluster name and the pinned CA
        with ui.step("Removing this box's CCC state"):
            untrust_and_unname()
            remove_user_dirs()
        # Inside the box, deliberately: `_release` drains the facts when the section closes, so a
        # result registered after it is never printed at all.
        ui.result("removed", f"{config.CCC_HOME}, the cluster name, and the trusted CA")
        ui.result("kept", f"{config.NSS_TOOLS_PKG} — unrecorded, and shared with anything else here")
        ui.result("this box", "is a plain machine again; `cuttle setup --client` sets it up")


# --- full sweep (any role, unknown/partial state) ----------------------------

def teardown_full(keep_packages: bool = False) -> None:
    """Sweep EVERYTHING cuttle could have installed, in ANY role, plus all local state. For a box in
    an unknown or partial state (a half-finished setup, or an old install whose role we can't tell).

    Runs every removal step — Head services AND Arm bits — regardless of what this box actually is.
    Each step is already tolerant of never having been applied (units that don't exist, packages not
    installed, files absent), so the belt-and-suspenders pass is safe on a plain client too."""
    with ui.section("Removing everything CCC", lambda: "removed"):
        system.prime_sudo()     # sudo as the box's first row, before any step rows grow the frame
        revoke_session()        # before anything below cuts the road to the Head — whichever box this is
        with ui.step("Stopping the Tidepools and deleting their images"):
            tidepool.teardown(True)  # unmounted BEFORE anything rm -rf's CCC_LIB, where they mount
        with ui.step("Stopping every CCC service"):
            cancel_dives()     # first: scancel needs the slurmctld the next line stops
            stop_daemons()      # Head services (no-ops where the units never existed)
            stop_arm_daemons()  # Arm bits
        with ui.step("Removing the Head's services"):
            remove_head_services()
        with ui.step("Tearing down the mesh"):
            remove_mesh(keep_packages=keep_packages)
        with ui.step("Removing Slurm, the cluster key and the CCC accounts"):
            remove_munge_key()
            remove_slurm_config()   # shared by both roles
            remove_mantle()
            remove_licence_account()  # before remove_guises: it is in GUISE_GROUP
            remove_guises()
        with ui.step("Removing the cluster name and its certificate authority"):
            untrust_and_unname()  # BEFORE the purge — it needs certutil, which the purge takes
        with ui.step("Removing CCC's containers and images"):
            remove_containers(True)  # BEFORE the purge — it needs podman
        if not keep_packages:
            with ui.step("Purging packages and repositories"):
                purge_packages()
                remove_repos()
        with ui.step("Removing this box's CCC state"):
            remove_user_dirs()  # local state — a full sweep always takes it
            remove_tls()
            prune_opt_dir()     # last: everything that lived under /opt/ccc is gone by now
    _summarise("everything CCC", keep_packages, kept_images=False, kept_tls=False)
