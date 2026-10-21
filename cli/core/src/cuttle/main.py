"""`cuttle` CLI entrypoint."""

from pathlib import Path

import typer

from . import auth as auth_mod
from . import backup as backup_mod
from . import config
from . import dashboard as dashboard_mod
from . import enroll as enroll_mod
from . import features as feature_profiles
from . import headapi as headapi_mod
from . import installer as installer_mod
from . import karma as karma_mod
from . import menu as menu_mod
from . import pickers
from . import settings
from . import setup as setup_mod
from . import spawns as spawns_mod
from . import system
from . import teardown as teardown_mod
from . import ui

# pretty_exceptions_enable=False: ui.run_app owns error rendering (one line, or a traceback under
# --debug); two competing renderers would be unpredictable.
# invoke_without_command: bare `cuttle` opens the menu on a terminal, prints help everywhere else.
app = typer.Typer(invoke_without_command=True, add_completion=False, help="CCC front door.",
                  pretty_exceptions_enable=False)
arm_app = typer.Typer(no_args_is_help=True, help="Manage Arms (run on the Head).")
app.add_typer(arm_app, name="arm")
user_app = typer.Typer(no_args_is_help=True, help="Manage users (run on the Head).")
app.add_typer(user_app, name="user")
# `cuttle karma` with no subcommand = the standings (invoke_without_command + the callback below).
karma_app = typer.Typer(invoke_without_command=True, help="Karma: standings, override, queue bump.")
app.add_typer(karma_app, name="karma")
backup_app = typer.Typer(no_args_is_help=True,
                         help="Backup/restore head.db + arms.json + Headscale's db (run on the Head).")
app.add_typer(backup_app, name="backup")
munge_app = typer.Typer(no_args_is_help=True,
                        help="The cluster's shared munge key (rotate on the Head, sync from an Arm).")
app.add_typer(munge_app, name="munge")
feature_app = typer.Typer(no_args_is_help=True,
                          help="Arm Features: heavy/hardware-licence-gated software CCC knows how to run.")
app.add_typer(feature_app, name="feature")


def _version(value: bool) -> None:
    # Eager: fires during option parsing, before the callback body or any subcommand — so `--version`
    # answers on a box that is not set up or logged in, which is exactly when you ask what you installed.
    if value:
        from importlib.metadata import version
        ui.data(f"cuttle {version('cuttle')}")
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    debug: bool = typer.Option(
        False, "--debug", help="Show full tracebacks instead of a one-line error "
                               "(or set CUTTLE_DEBUG=1)."),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show every shell command setup runs, instead of progress."),
    no_input: bool = typer.Option(
        False, "--no-input", help="Never prompt; a missing argument is an error. Implied when stdin "
                                  "is not a terminal — this forces it on when one is."),
    version: bool = typer.Option(
        False, "--version", "-V", is_eager=True, callback=_version,
        help="Show the cuttle version and exit."),
) -> None:
    """CCC front door."""
    ui.configure(debug=debug, verbose=verbose, no_input=no_input)
    if ctx.invoked_subcommand is None:
        _no_command()


def _no_command() -> None:
    """Bare `cuttle`: the menu on a terminal, help everywhere else.

    Help is the right answer for a pipe or a script — a menu there would block forever waiting for a
    keystroke nobody can send.
    """
    if not ui.interactive():
        cmd = typer.main.get_command(app)
        # info_name is what fills `Usage: <prog>` — without it the help renders "Usage:  [OPTIONS]".
        ui.data(cmd.get_help(typer.Context(cmd, info_name="cuttle")))
        return
    # The alternate screen is already ours — `ui.run_app` claims it for every command, this one
    # included. Results and stdout are replayed onto the real screen afterwards, so nothing you
    # needed goes down with the buffer.
    path = menu_mod.choose(app)
    if path is None:
        return
    # Re-enter through the real parser so the chosen command prompts for its own missing
    # arguments exactly as it would if you had typed it. standalone_mode=False keeps errors
    # flowing to ui.run_app rather than click's own exit path.
    typer.main.get_command(app).main(args=path, standalone_mode=False)


@app.command()
def setup(
    head: bool = typer.Option(
        False, "--head", help="Provision this box as the Head (control plane + co-located Arm)."
    ),
    local: bool = typer.Option(
        False, "--local", help="Alias for --head (single-box case)."
    ),
    arm: bool = typer.Option(
        False, "--arm", help="Enroll this box as a remote Arm (needs --token, or --enrolled to redo)."
    ),
    client: bool = typer.Option(
        False, "--client", help="Submitter's box: dirs, the cluster name + CA, login. No mesh or Slurm."
    ),
    address: str = typer.Option(
        None, "--address",
        help="Head: coordinator's advertised host (default: primary IP; use a public/DDNS addr "
             "so remote Arms can reach it).",
    ),
    token: str = typer.Option(
        None, "--token", help="Arm: the enrollment token from `cuttle arm authorize` (a SECRET)."),
    enrolled: bool = typer.Option(
        False, "--enrolled",
        help="Arm: set this box up again without a token, reusing the enrollment it already has. "
             "Only for a box still on the mesh — nothing is re-enrolled.",
    ),
    re_enroll: bool = typer.Option(
        False, "--re-enroll",
        help="Arm: discard this box's mesh identity and re-join from scratch (needs a fresh token). "
             "For a corrupt state file, or a Head rebuilt behind the same coordinator URL.",
    ),
):
    """Provision a node. --head (or --local) for the Head; --arm to enroll a compute node."""
    if client:
        return setup_mod.setup_client()
    if not (head or local or arm):
        # The wizard. Flags remain the unattended path — the installer and every scripted
        # provisioning run depend on `setup --head --address=…` never stopping to ask.
        if not ui.interactive():
            raise typer.BadParameter("specify a role: cuttle setup --head | --arm")
        with ui.section("Set up this box", lambda: role):
            choices = [
                ui.choice("The Head — control plane, and donates its own GPU", "head"),
                ui.choice("An Arm — donates a GPU to someone else's Head", "arm"),
                ui.choice("A client — just submit Spawns from here", "client"),
            ]
            # Pre-selected with what this box ALREADY is, when it is anything. On a re-run the answer
            # is almost never "make it something else", and the alternative — always landing on "the
            # Head" — invites an Enter that tries to provision a control plane onto a donor's box.
            current = ("head" if menu_mod.is_head() else
                       "arm" if menu_mod.is_arm() else
                       "client" if menu_mod.is_pointed() else None)
            role = ui.ask_select("What is this box?", choices, default=current)
            if role == "head":
                ui.say("  [muted]Arms reach the coordinator at this address before they are on the "
                       "mesh, so it must be reachable from their network.[/muted]")
                # Pre-filled with what this Head already advertises, not primary_ip(): on a re-run the
                # stored address is the answer, and offering the LAN IP invites the operator to accept
                # a revert that strands every remote Arm (see setup.resolve_coordinator_address).
                stored = settings.load().get("mesh", {}).get("head_address")
                address = address or ui.ask_text("Address Arms will reach this Head on?",
                                                 default=stored or system.primary_ip())
            elif role == "arm":
                # A box that has enrolled before is offered its own enrollment back, because the
                # answer to "Enrollment token?" for a re-setup is a secret it cannot produce — the
                # token was printed once, deliberately never logged, and is long spent.
                #
                # Not offered when --re-enroll was asked for: that flag is the decision to throw this
                # enrollment away, so offering to reuse it is offering the opposite of what was asked
                # — and would only be refused below, after the prompts.
                prior = None if re_enroll else (settings.load().get("arm") or {}).get("node_name")
                enrolled = bool(prior) and ui.ask_confirm(
                    f"This box is already enrolled as {prior} — set it up again without a token?",
                    True)
                if not enrolled:
                    ui.say("  [muted]Ask the cluster's admin for a token: "
                           "cuttle arm authorize <name> --user <you>[/muted]")
                    token = token or ui.ask_text("Enrollment token?")
            # Prime sudo while this box is still open, so its prompt is a row of the box already on
            # screen, not the first row of the next one (a section clears the alt screen). Client
            # needs no sudo; setup_head/arm still prime as a no-op backstop for the flagless path.
            if role in ("head", "arm"):
                system.prime_sudo()
        if role == "client":
            return setup_mod.setup_client()
        head, arm = role == "head", role == "arm"
    if head or local:
        if re_enroll:
            raise typer.BadParameter("--re-enroll is an Arm flag; the Head re-registers itself")
        setup_mod.setup_head(address)
    elif arm:
        # --enrolled is the absence of a token, not a variant of one: it says "reuse what this box
        # already redeemed". Pairing it with either of the others asks for two different identities in
        # one run, and --re-enroll in particular is its exact opposite — that one throws the
        # enrollment away, which is why it needs a fresh key and this one refuses to pretend it has.
        if enrolled and token:
            raise typer.BadParameter("--enrolled reuses this box's enrollment; drop --token to use it")
        if enrolled and re_enroll:
            raise typer.BadParameter(
                "--re-enroll discards the enrollment --enrolled reuses. Re-joining needs a fresh "
                "token: cuttle setup --arm --token <token> --re-enroll")
        if not token and not enrolled:
            raise typer.BadParameter(
                "`--arm` needs --token. Ask the cluster's admin to run: "
                "cuttle arm authorize <name> --user <you>\n"
                "If this box is already enrolled and you are just setting it up again: --enrolled")
        setup_mod.setup_arm(token, re_enroll=re_enroll)


@app.command()
def update():
    """Set this box up again with the settings it already has. Asks nothing.

    The re-run after a CCC upgrade or a code change: the API venv is rebuilt, the Dashboard
    re-bundled, every unit file and slurm.conf re-rendered, the certificate re-issued. What it never
    does is change what this box *is* — it does not enroll, re-point, or re-price an Arm, and it does
    not re-measure or re-license hardware it has already measured and licensed.

    The role comes from the box, not a flag. `cuttle setup` remains where every answer is made or
    changed, and a box that was never set up has nothing to update.
    """
    setup_mod.update_this_box()


@arm_app.command("list")
def arm_list(as_json: bool = typer.Option(False, "--json", help="Machine-readable output.")):
    """Show Arms: who owns them, and whether they're authorized/enrolled/admitted."""
    enroll_mod.list_arms(as_json=as_json)


@arm_app.command("authorize")
def arm_authorize(
    name: str = typer.Argument(None, help="Name for the new Arm (its identity in Slurm + the mesh)."),
    user: str = typer.Option(None, "--user", help="The User who owns (donates) the machine."),
):
    """Authorize one machine to join as an Arm. Prints the enrollment token its owner redeems with
    `cuttle setup --arm --token <blob>`, then waits for them and admits them to Slurm.

    The token is a SECRET — it admits one machine, once, within the hour. Send it privately.
    """
    with ui.section("Authorize an Arm", lambda: f"{name} → {user}"):
        name = ui.need(name, "the Arm's name", lambda: ui.ask_text("Name for the new Arm?"))
        user = ui.need(user, "--user", lambda: pickers.pick_user("Who owns it?"))
    enroll_mod.authorize_arm(name, user, setup_mod._write_root_file)


@arm_app.command("admit")
def arm_admit(name: str = typer.Argument(None, help="The Arm's node name.")):
    """Give an enrolled Arm its GRES line and reconfigure Slurm. Only needed if `arm authorize` gave
    up waiting because the donor hadn't run setup yet — this finishes the job."""
    name = ui.need(name, "the Arm's name", lambda: pickers.pick_arm("Which Arm to admit?"))
    enroll_mod.admit_arm(name, setup_mod._write_root_file)


@arm_app.command("benchmark")
def arm_benchmark(name: str = typer.Argument(None, help="The Arm's node name.")):
    """Re-measure an Arm's **Power** (throughput) and re-price it. Runs the matmul benchmark inside
    the Pod, on that node, and sets its Slurm Weight — so the fastest free card wins.

    Normally part of `arm authorize`; run it by hand after a driver/GPU change, or if the benchmark
    timed out because the donor's box wasn't ready.
    """
    name = ui.need(name, "the Arm's name", lambda: pickers.pick_arm("Which Arm to re-benchmark?"))
    enroll_mod.benchmark_arm(name)


@arm_app.command("chown")
def arm_chown(
    name: str = typer.Argument(None, help="The Arm's node name."),
    user: str = typer.Option(None, "--user", help="The User who owns (donates) the machine."),
):
    """Re-point an Arm at a different owner. Touches neither the mesh nor Slurm, so the donor's box
    keeps running."""
    with ui.section("Change an Arm's owner", lambda: f"{name} → {user}"):
        name = ui.need(name, "the Arm's name", lambda: pickers.pick_arm())
        user = ui.need(user, "--user", lambda: pickers.pick_user("New owner?"))
    enroll_mod.chown_arm(name, user)


@arm_app.command("retract")
def arm_retract(
    name: str = typer.Argument(None, help="The Arm's node name."),
    any_owner: bool = typer.Option(
        False, "--any",
        help="Write head.db directly instead of asking the Head who owns it — for an Arm the "
             "API has no owner row for. Head-only. On the Head, retracting someone else's "
             "Arm needs no flag."),
):
    """Pause an Arm: stop it taking new Spawns and end whatever is running on it now (TERM right
    away, a 3-minute grace, then force-killed). Settles at zero either way — Retract must never be a
    way to farm Karma by yanking a card back mid-run. Your own from anywhere; anyone's on the Head."""
    name = ui.need(name, "the Arm's name", lambda: pickers.pick_arm("Which Arm to retract?"))
    enroll_mod.retract_arm(name, any_owner)


@arm_app.command("return")
def arm_return(
    name: str = typer.Argument(None, help="The Arm's node name."),
    any_owner: bool = typer.Option(
        False, "--any",
        help="Write head.db directly instead of asking the Head who owns it — for an Arm the "
             "API has no owner row for. Head-only. On the Head, returning someone else's "
             "Arm needs no flag."),
):
    """Reopen a retracted Arm for new Spawns. Does not save a Spawn the Retract already caught.
    Your own from anywhere; anyone's on the Head."""
    name = ui.need(name, "the Arm's name", lambda: pickers.pick_arm("Which Arm to return?"))
    enroll_mod.return_arm(name, any_owner)


@arm_app.command("sync-key")
def arm_sync_key():
    """ON THIS ARM: re-fetch the cluster's current munge key from the Head. Run after an operator's
    `cuttle munge rotate` — nothing else changes (mesh identity, gres.conf, and slurmd's
    --conf-server are all left alone; this touches only the key)."""
    enroll_mod.sync_munge_key()


@arm_app.command("remove")
def arm_remove(
    name: str = typer.Argument(None, help="The Arm's node name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
):
    """Delete an Arm from Slurm, the registry, AND the mesh. All three, or the departed donor keeps
    a live mesh address — and mesh membership is what authorizes fetching the munge key."""
    name = ui.need(name, "the Arm's name", lambda: pickers.pick_arm("Which Arm to remove?"))
    # Typed confirmation, not y/N: this evicts a donor's machine from the mesh, and the mesh is what
    # authorizes fetching the munge key — re-adding it is a full re-enrollment, not an undo.
    if not yes and ui.interactive():
        ui.caution(f"removing {name} from Slurm, the registry and the mesh — the donor must re-enroll")
        if not ui.ask_typed(f"Remove {name}?", name):
            raise ui.abort("not removed")
    enroll_mod.remove_arm(name, setup_mod._write_root_file)


@app.command()
def login(
    head: str = typer.Option(
        None, "--head", help="Head address or URL (e.g. ccc.glados); saved for next time."),
):
    """Log in to the Head and store a session token (~/.ccc/session). Forces a password change on
    first login."""
    # A box, so the URL/username/password prompts draw as rows *inside* the frame rather than loose
    # lines below it. `who` carries the result out to the summary line the box collapses to.
    who: dict[str, str] = {}
    with ui.section("Log in", lambda: who.get("as", "")):
        # Only asked when no Head is configured yet — `cuttle login` on a set-up box should not
        # re-interrogate you about a URL it already knows.
        if head is None and not settings.load().get("head") and ui.interactive():
            # A default only when we ARE the Head — there the Cluster name resolves (setup wrote the
            # hosts entry) and the cert covers it. On a client that name means nothing until
            # `setup --client` writes its own entry, so never prefill it: accepted, it saves as an
            # unresolvable name and wedges every login.
            default = headapi_mod.head_url(config.DOMAIN) if menu_mod.is_head() else ""
            head = ui.ask_text("Head API URL?", default=default)
            if not head.strip():
                raise ui.abort("no Head URL given",
                               next_step="`cuttle setup --client` — it asks for the address, then "
                                         "points this box at the cluster by name")
        who["as"] = auth_mod.login(head)


@app.command()
def logout():
    """Revoke this box's session on the Head and delete the local token."""
    auth_mod.logout()


@app.command()
def dashboard(
    print_only: bool = typer.Option(False, "--print", help="Print the URL instead of opening it."),
):
    """Open the Dashboard — the web client for this cluster — in a browser. It lives at the same
    address this box logs in to, so nobody has to remember the Head's IP after setup."""
    dashboard_mod.open_dashboard(print_only)


@app.command()
def whoami(as_json: bool = typer.Option(False, "--json", help="Machine-readable output.")):
    """Print the currently logged-in user."""
    auth_mod.whoami(as_json=as_json)


@app.command()
def passwd():
    """Change your password."""
    with ui.section("Change password", lambda: "changed"):
        auth_mod.passwd()


@user_app.command("add")
def user_add(
    username: str = typer.Argument(None, help="New user's login name."),
    admin: bool = typer.Option(None, "--admin/--no-admin", help="Grant admin."),
):
    """Create a user (prints a one-time temp password they must change on first login)."""
    # Optional flags are prompted for only when we are already in a wizard — i.e. when the required
    # argument was missing too. Otherwise `cuttle user add bob` in a script would start demanding an
    # answer for every flag it did not mention, and an absent `--admin` has always meant "no".
    wizard = username is None
    with ui.section("Create a user", lambda: f"{username}{' (admin)' if admin else ''}"):
        username = ui.need(username, "the username", lambda: ui.ask_text("New user's login name?"))
        if admin is None:
            admin = ui.ask_confirm("Grant admin?", False) if wizard else False
    auth_mod.user_add(username, admin=admin)


@user_app.command("list")
def user_list():
    """List users."""
    auth_mod.user_list()


@user_app.command("remove")
def user_remove(
    username: str = typer.Argument(None, help="User to delete (and their sessions)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
):
    """Delete a user."""
    username = ui.need(username, "the username", lambda: pickers.pick_user("Which user to delete?"))
    if not yes and ui.interactive():
        ui.caution(f"deleting {username} and every session they hold")
        if not ui.ask_typed(f"Delete {username}?", username):
            raise ui.abort("not deleted")
    auth_mod.user_remove(username)


@user_app.command("reset")
def user_reset(
    username: str = typer.Argument(None, help="User whose password to reset."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
):
    """Reset a user's password (prints a one-time temp password; kills their existing sessions)."""
    username = ui.need(username, "the username", lambda: pickers.pick_user("Reset whose password?"))
    if not yes and ui.interactive():
        if not ui.ask_confirm(f"Reset {username}'s password? This kills their existing sessions.", False):
            raise ui.abort("not reset")
    auth_mod.user_reset(username)


@karma_app.callback()
def karma(ctx: typer.Context,
          as_json: bool = typer.Option(False, "--json", help="Machine-readable output.")):
    """Show every User's standing: Balance, Escrow, Share, and the queue nice it implies.

    Everyone's, not just yours — Karma ranks Users against each other, so a Balance in isolation says
    nothing. Karma never gates: poor standing loses contention, and loses nothing on an idle cluster.
    """
    if ctx.invoked_subcommand is None:
        karma_mod.standings(as_json=ctx.params.get("as_json", False))


# ignore_unknown_options: a NEGATIVE amount is this command's main use, and `override bob -50000`
# otherwise dies with `No such option: -5` — click reads any leading `-` as a flag. Without this the
# only spelling that works is `override --note x bob -- -50000`, which nobody will ever guess.
@karma_app.command("override", context_settings={"ignore_unknown_options": True})
def karma_override(
    username: str = typer.Argument(None, help="The User whose Balance to change."),
    amount: float = typer.Argument(None, help="Karma to add (or remove, if negative). 1 Karma = one "
                                              "TFLOP-hour of device time."),
    note: str = typer.Option(None, "--note", help="Why — recorded in the ledger."),
):
    """ADMIN: add/remove Karma from a User's Balance. Changes *standing*, so it is felt in every future
    contention (and Decay absorbs it over ~a month). To speed up one Spawn once, use `karma bump`."""
    wizard = username is None
    with ui.section("Adjust a Balance", lambda: f"{username} {amount:+g}"):
        username = ui.need(username, "the username", lambda: pickers.pick_user("Whose Balance?"))
        amount = ui.need(amount, "the amount",
                         lambda: ui.ask_float("How much Karma? (negative removes)"))
        if note is None and wizard:
            note = ui.ask_text("Why? (recorded in the ledger)") or None
    karma_mod.override(username, amount, note)


@karma_app.command("bump")
def karma_bump(
    spawn_id: int = typer.Argument(None, help="Spawn (Slurm job) id — must still be queued."),
    clear: bool = typer.Option(False, "--clear", help="Undo the bump; the economy takes it back."),
):
    """ADMIN, ON THE HEAD: shove one queued Spawn up the queue. Touches no one's Karma and leaves no
    ledger row — it bypasses the economy rather than participating in it. Bump again to go further.

    Runs here rather than over the API because a bump is a negative nice, and Slurm grants those only
    to an operator — an authority the LAN-facing API deliberately does not have.
    """
    spawn_id = ui.need(spawn_id, "the Spawn id",
                       lambda: pickers.pick_spawn("Bump which Spawn?", mine_only=False))
    karma_mod.bump(spawn_id, clear=clear)


@karma_app.command("ledger")
def karma_ledger(
    days: int = typer.Option(30, "--days", help="How far back to read."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
):
    """Your own Karma ledger: the real events (Settlements, Overrides) and the Balance they leave you
    at. Yours alone — standings is everyone's so you can rank; a history needs nobody else's rows."""
    karma_mod.ledger(days=days, as_json=as_json)


@backup_app.command("create")
def backup_create():
    """ON THE HEAD: snapshot head.db, arms.json, and Headscale's own db into one tarball under
    /var/backups/ccc. No downtime — the Head API, Karma tick, and Headscale all keep running."""
    with ui.section("Backing up cluster state", lambda: "head.db + arms.json + headscale db"):
        path = backup_mod.create(setup_mod._write_root_file)
        ui.result("archive", str(path))


@backup_app.command("restore")
def backup_restore(
    archive: str = typer.Argument(None, help="Path to a backup tarball (see /var/backups/ccc)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
):
    """ON THE HEAD: restore head.db, arms.json, and Headscale's db from a backup tarball. Stops the
    Enroll API, Head API, Karma tick, and Headscale; replaces all three files; restarts everything.

    All-or-nothing — overwrites every Spawn/Karma/session/Arm-enrollment change made since the
    backup. There is no undo short of another restore.
    """
    archive = ui.need(archive, "the archive path", lambda: pickers.pick_backup())
    if not yes and ui.interactive():
        ui.caution("this overwrites LIVE head.db, arms.json, and Headscale's db — "
                   "anything created since this backup is gone")
        if not ui.ask_typed(f"Restore {Path(archive).name}?", Path(archive).name):
            raise ui.abort("not restored")
    with ui.section("Restoring cluster state", lambda: Path(archive).name):
        backup_mod.restore(Path(archive), setup_mod._write_root_file)
    ui.ok("restored — the Enroll API, Head API, Karma tick, and Headscale are back up")


@munge_app.command("rotate")
def munge_rotate(yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation.")):
    """ON THE HEAD: mint a fresh cluster-wide munge key. Every already-admitted Arm's copy goes stale
    the instant this returns — each one needs `cuttle arm sync-key`, run by its owner, before it can
    reach slurmctld again. Prints the list of Arms that need it."""
    if not yes and ui.interactive():
        ui.caution("this replaces the key every Arm authenticates to slurmctld with — every "
                   "already-admitted Arm needs `cuttle arm sync-key` afterward, run by its owner")
        if not ui.ask_confirm("Rotate the cluster's munge key?", False):
            raise ui.abort("not rotated")
    enroll_mod.rotate_munge_key()


@feature_app.command("list")
def feature_list(as_json: bool = typer.Option(False, "--json", help="Machine-readable output.")):
    """List the Arm Features CCC supports. Built in, not configurable — the catalog is the set of names
    CCC knows the meaning of, not a list anyone curates. Run anywhere."""
    enroll_mod.list_features(as_json=as_json)


@feature_app.command("show")
def feature_show(name: str = typer.Argument(..., help="A Feature from `cuttle feature list`.")):
    """Describe what CCC arranges for a Feature — the system libraries it builds for you, and which
    Python your own Environment has to be.

    Fetched from the Head rather than built here, so there is one copy of that list in the codebase and
    it is the same one CCC itself builds. Not every Feature has one: a Feature can be nothing but
    placement plus an install path, which needs no runtime to describe."""
    # Plain `print`, not ui.say: Rich would read the `[...]` in a version spec as markup.
    print(feature_profiles.runtime_summary(name), end="")


@app.command("build-installer")
def build_installer(
    out: Path = typer.Option(
        None, "--out", help="Where to write the .tar.gz (default: ./cuttle-installer.tar.gz)."
    ),
    head_url: str = typer.Option(
        None, "--head-url",
        help="Head URL to bake in (default: https://ccc.glados). Only used when built on a "
             "Head — the client comes pre-pointed at this address.",
    ),
    generic: bool = typer.Option(
        False, "--generic",
        help="Bake nothing, even on a Head. For a box that cannot reach this Head's LAN address — "
             "a remote Arm, which points itself at the mesh when it enrolls.",
    ),
):
    """Package a portable client installer (wheel + install.sh + uninstall.sh) as a .tar.gz.

    Run on a Head, it bakes the Cluster name + this cluster's certificate authority in, so the
    installed client is pre-pointed and already trusts the cluster. Run elsewhere — or with --generic
    — it produces a bundle that points itself.

    The baked bundle resolves that name to the Head's LAN IP, so it is for boxes ON that LAN. A remote
    box reaches this Head over the CCC mesh, and it is not on the mesh until it enrolls — so build it
    a --generic bundle and let `setup --arm` point it.
    """
    if generic and head_url:
        raise typer.BadParameter("--generic bakes no address; drop --head-url")
    installer_mod.build_installer(out=out, head_url=head_url, generic=generic)


@app.command()
def teardown(
    local: bool = typer.Option(
        False, "--local", help="This box's combined Head+Arm (reverse `setup --head`)."
    ),
    arm: bool = typer.Option(
        False, "--arm", help="This box's Arm role (reverse `setup --arm`)."
    ),
    client: bool = typer.Option(
        False, "--client", help="This box's client state only — ~/.ccc, the cluster name, the CA."
    ),
    keep_packages: bool = typer.Option(
        False, "--keep-packages", help="Leave apt packages + repos installed (fast config reset)."
    ),
    all_: bool = typer.Option(
        False, "--all", help="Also remove base.sif and the Head's TLS cert (forces an image rebuild "
                             "and a client re-pin next setup). ~/.ccc is removed either way."
    ),
    full: bool = typer.Option(
        False, "--full", help="Sweep EVERYTHING cuttle installed — every role's services, packages, "
                              "mesh, ~/.ccc, base.sif and the TLS cert. For a partial/stuck install."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the typed confirmation."),
):
    """Reverse setup. With no role flag the role is auto-detected; --full sweeps everything."""
    picked = [r for r, on in (("local", local), ("arm", arm), ("client", client)) if on]
    if len(picked) > 1:
        raise typer.BadParameter("pick one role: --local, --arm or --client")
    # Off-terminal, flagless teardown is refused: auto-detecting a role and then destroying head.db
    # with no confirmation and nobody watching is a footgun. A script states its intent — a role,
    # --full, or --yes (which is what the bundled uninstaller passes).
    if not picked and not full and not yes and not ui.interactive():
        raise typer.BadParameter(
            "off-terminal teardown needs an explicit role (--local/--arm/--client), --full, or --yes")
    # --full ignores detection by design (its whole point is a box in an unknown state); otherwise
    # an explicit flag wins, and failing that we detect what this box actually is.
    role = "full" if full else (picked[0] if picked else teardown_mod.detect_role())
    if full:
        all_ = True  # a full sweep always takes local state with it

    # A client teardown only removes ~/.ccc — no head.db, no privilege — so a y/N is proportionate.
    # Every other role can destroy head.db (every account, Balance and ledger row) and reprovisioning
    # is not an undo, so it takes a typed hostname: a confirmation reflex can't answer it by accident.
    if not yes and ui.interactive():
        if role == "client":
            if not ui.ask_confirm("Remove this box's CCC client state (~/.ccc)?", False):
                raise ui.abort("not removed")
        else:
            host = system.hostname()
            ui.caution("this removes Slurm, the mesh, head.db (every account, Balance and ledger row) "
                       "and ~/.ccc" + (" — plus base.sif and the TLS cert" if all_ else ""))
            if not ui.ask_typed(f"Tear down {host}?", host):
                raise ui.abort("not torn down")

    if role == "full":
        teardown_mod.teardown_full(keep_packages=keep_packages)
    elif role == "local":
        teardown_mod.teardown_local(keep_packages=keep_packages, all_=all_)
    elif role == "arm":
        teardown_mod.teardown_arm(keep_packages=keep_packages, all_=all_)
    else:
        teardown_mod.teardown_client()


@app.command()
def cancel(
    spawn_id: int = typer.Argument(None, help="Spawn (Slurm job) id."),
    any_owner: bool = typer.Option(
        False, "--any",
        help="Cancel without asking the Head who owns it — reaps a job the ledger never recorded. "
             "ONLY on the Head (uses sudo scancel). On the Head, cancelling someone else's Spawn "
             "needs no flag."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
):
    """End a Spawn early. Your own from anywhere; anyone's only on the Head."""
    # On the Head the picker offers the WHOLE queue, flag or no flag. The menu can only ever run this
    # command bare (menu.choose returns argv with no options), so a picker filtered to your own Spawns
    # made the operator's road unreachable from the menu entirely — the Spawn you meant to stop was
    # simply not on the list. `karma bump`, the other Head-local command that points at someone else's
    # Spawn, has always listed everything for the same reason.
    spawn_id = ui.need(spawn_id, "the Spawn id",
                       lambda: pickers.pick_spawn(
                           "Which Spawn to cancel?",
                           mine_only=not (any_owner or menu_mod.is_head())))
    spawns_mod.cancel(spawn_id, any_owner=any_owner, yes=yes)


# `--limit` defaults to a sentinel (-1 = unset) so we can resolve it per format: a human table caps at
# 50, but `--json` stays a complete data feed (0 = uncapped) unless the user asks for a cap explicitly.
def _resolve_limit(limit: int, as_json: bool) -> int:
    return limit if limit >= 0 else (0 if as_json else 50)


@app.command()
def queue(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    limit: int = typer.Option(-1, "--limit", show_default=False,
                              help="Cap rows (0 = all). Default 50, uncapped for --json."),
):
    """Show the live Spawn queue — every pending/running Spawn on the cluster, and who submitted it."""
    spawns_mod.queue(as_json=as_json, limit=_resolve_limit(limit, as_json))


@app.command()
def history(
    days: int = typer.Option(30, "--days", help="How far back to read."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    limit: int = typer.Option(-1, "--limit", show_default=False,
                              help="Cap rows (0 = all). Default 50, uncapped for --json."),
):
    """Show past Spawns — everything the live queue has forgotten — with their cost and outcome."""
    spawns_mod.history(days=days, as_json=as_json, limit=_resolve_limit(limit, as_json))


@app.command()
def account(as_json: bool = typer.Option(False, "--json", help="Machine-readable output.")):
    """Show who you are and where you stand: identity, Balance, Escrow, Share, and your queue nice."""
    auth_mod.account(as_json=as_json)


@app.command()
def tide(
    days: int = typer.Option(7, "--days", help="How far back to draw."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
):
    """Show the Tide — how badly each Arm has been wanted, hour by hour, as a sparkline per Arm."""
    karma_mod.tide(days=days, as_json=as_json)


@app.command()
def status(spawn_id: int = typer.Argument(None, help="Spawn (Slurm job) id."),
           as_json: bool = typer.Option(False, "--json", help="Machine-readable output.")):
    """Show a Spawn's state (queued/running, or DONE with exit code)."""
    spawn_id = ui.need(spawn_id, "the Spawn id", lambda: pickers.pick_spawn("Which Spawn?"))
    spawns_mod.status(spawn_id, as_json=as_json)


@app.command()
def logs(
    spawn_id: int = typer.Argument(None, help="Spawn (Slurm job) id."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Interleave both streams live, tagged [out]/[err]."),
):
    """Print (or follow) a Spawn's stdout and stderr."""
    spawn_id = ui.need(spawn_id, "the Spawn id", lambda: pickers.pick_spawn("Read which Spawn?"))
    spawns_mod.logs(spawn_id, follow=follow)


def main() -> None:
    ui.run_app(app)


if __name__ == "__main__":
    main()
