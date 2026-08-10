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
from . import recipes as recipes_mod
from . import settings
from . import setup as setup_mod
from . import spawns as spawns_mod
from . import system
from . import teardown as teardown_mod
from . import tidepool as tidepool_mod
from . import ui
from . import watch as watch_mod
from . import workspaces as workspaces_mod

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
wandb_app = typer.Typer(no_args_is_help=True,
                        help="Your W&B key, held on the Head and injected into every Spawn.")
app.add_typer(wandb_app, name="wandb")
munge_app = typer.Typer(no_args_is_help=True,
                        help="The cluster's shared munge key (rotate on the Head, sync from an Arm).")
app.add_typer(munge_app, name="munge")
feature_app = typer.Typer(no_args_is_help=True,
                          help="Arm Features: heavy/hardware-licence-gated software CCC knows how to run.")
app.add_typer(feature_app, name="feature")
recipe_app = typer.Typer(no_args_is_help=True,
                         help="Recipes: frozen, re-runnable snapshots of a project (32GB allowance).")
app.add_typer(recipe_app, name="recipe")
workspace_app = typer.Typer(no_args_is_help=True,
                            help="Workspaces: working directories that survive the session (32GB "
                                 "allowance, divided into a Storage per workspace).")
app.add_typer(workspace_app, name="workspace")


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
    # Sweeper is a detached process with nothing keeping it alive across reboot, so every command
    # is an opportunity to notice results are still owed and restart one. Swallows all errors — a
    # client whose watch dir is unreadable must still be able to run `status`.
    watch_mod.ensure_sweeper()
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
        False, "--arm", help="Enroll this box as a remote Arm (needs --token)."
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
            role = ui.ask_select("What is this box?", [
                ui.choice("The Head — control plane, and donates its own GPU", "head"),
                ui.choice("An Arm — donates a GPU to someone else's Head", "arm"),
                ui.choice("A client — just submit Spawns from here", "client"),
            ])
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
        if not token:
            raise typer.BadParameter(
                "`--arm` needs --token. Ask the cluster's admin to run: "
                "cuttle arm authorize <name> --user <you>")
        setup_mod.setup_arm(token, re_enroll=re_enroll)


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
        False, "--any", help="Retract on someone else's behalf (Head-only, admin)."),
):
    """Pause your own Arm: stop it taking new Spawns and end whatever is running on it now (TERM right
    away, a 3-minute grace, then force-killed). Settles at zero either way — Retract must never be a
    way to farm Karma by yanking a card back mid-run."""
    name = ui.need(name, "the Arm's name", lambda: pickers.pick_arm("Which Arm to retract?"))
    enroll_mod.retract_arm(name, any_owner)


@arm_app.command("return")
def arm_return(
    name: str = typer.Argument(None, help="The Arm's node name."),
    any_owner: bool = typer.Option(
        False, "--any", help="Return on someone else's behalf (Head-only, admin)."),
):
    """Reopen a retracted Arm for new Spawns. Does not save a Spawn the Retract already caught."""
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
        ui.warn(f"removing {name} from Slurm, the registry and the mesh — the donor must re-enroll")
        if not ui.ask_typed(f"Remove {name}?", name):
            raise ui.abort("not removed")
    enroll_mod.remove_arm(name, setup_mod._write_root_file)


@app.command()
def login(
    head: str = typer.Option(
        None, "--head", help="Head address or URL (e.g. ccc.glados); saved for next time."),
):
    """Log in to the Head and store a session token (~/.ccc/credentials). Forces a password change on
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
        ui.warn(f"deleting {username} and every session they hold")
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


@wandb_app.command("set")
def wandb_set(
    key: str = typer.Argument(None, help="Your W&B API key (prompted for if omitted)."),
):
    """Store your W&B API key on the Head. It is injected into every Spawn you submit, from the CLI
    or the Dashboard, and is held encrypted — no other User can read it, and it never touches this
    box's disk."""
    # Visible, not a password prompt: a pasted key has to be verifiable by eye.
    key = ui.need(key, "the key", lambda: ui.ask_text("Paste your W&B API key"))
    auth_mod.wandb_set(key)


@wandb_app.command("show")
def wandb_show(
    reveal: bool = typer.Option(False, "--reveal", help="Print the key in full, not masked."),
):
    """Show whether a W&B key is stored for you, masked to its last 4 characters."""
    auth_mod.wandb_show(reveal=reveal)


@wandb_app.command("clear")
def wandb_clear():
    """Forget your stored W&B key. Your Spawns go back to running with W&B disabled."""
    auth_mod.wandb_clear()


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
        ui.warn("this overwrites LIVE head.db, arms.json, and Headscale's db — "
                "anything created since this backup is gone")
        if not ui.ask_typed(f"Restore {Path(archive).name}?", Path(archive).name):
            raise ui.abort("not restored")
    with ui.section("Restoring cluster state", lambda: Path(archive).name):
        backup_mod.restore(Path(archive), setup_mod._write_root_file)
    ui.ok("restored — the Enroll API, Head API, Karma tick, and Headscale are back up")
    # The one thing a restore silently does NOT bring back. Said here because the alternative is a User
    # discovering it as a Spawn that quietly logged nothing (see backup.py's docstring for why the
    # keyfile is left out of the archive on purpose).
    ui.warn("stored W&B keys did not come back — the key that decrypts them is deliberately left out "
            "of every backup, so each User must re-paste theirs (`cuttle wandb set`, or the "
            "Dashboard's Account page)")


@munge_app.command("rotate")
def munge_rotate(yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation.")):
    """ON THE HEAD: mint a fresh cluster-wide munge key. Every already-admitted Arm's copy goes stale
    the instant this returns — each one needs `cuttle arm sync-key`, run by its owner, before it can
    reach slurmctld again. Prints the list of Arms that need it."""
    if not yes and ui.interactive():
        ui.warn("this replaces the key every Arm authenticates to slurmctld with — every "
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


# --- recipes ------------------------------------------------------------------
# Client duties, so these work from any machine (CONTEXT.md: the CLI is at operation parity with the
# Dashboard). A Recipe holds the code, so `save`/`update` upload the working dir exactly as `run` does —
# what differs is that neither runs anything. See cuttle.recipes.

@recipe_app.command("list")
def recipe_list(as_json: bool = typer.Option(False, "--json", help="Machine-readable output.")):
    """Your saved Recipes, and what they hold against your 32GB allowance. Yours alone — a Recipe holds
    your actual source, so unlike the queue there is no everyone-else view of them."""
    recipes_mod.listing(as_json=as_json)


@recipe_app.command("save", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def recipe_save(
    ctx: typer.Context,
    name: str = typer.Argument(None, help="Name for the Recipe — how you run it later. Yours only, so "
                                          "it need only be unique among your own."),
    script: str = typer.Argument(None, help="Python script the Recipe runs, relative to this directory."),
    time: int = typer.Option(None, "--time", help="Wall-clock limit in MINUTES, frozen with the code."),
    vram: int = typer.Option(None, "--vram", help="TOTAL GPU memory needed, in GB, across the cards "
                                                  "asked for. Frozen with the code."),
    gpu: int = typer.Option(1, "--gpus", "--gpu", help="GPUs to hold at once, all on one machine. "
                                                      "--vram is the total across them."),
    output: list[str] = typer.Option(
        None, "--output", "-o", help="Subdir under $CCC_OUT the script writes results to (repeatable). "
                                     "Required — a Recipe with none could never be run."),
    manifest: str = typer.Option(
        None, "--manifest", help="REQUIRED. Dependency file the Recipe's Python environment is built "
                                 "from, frozen with the code — a Recipe freezes what its work runs "
                                 "IN, not just the code."),
    project: str = typer.Option(None, "--project", help="W&B project, frozen with the code."),
    entity: str = typer.Option(None, "--entity", help="W&B entity/slug, frozen with the code."),
    no_gitignore: bool = typer.Option(
        False, "--no-gitignore", help="Upload the whole working dir (default: honor .gitignore)."),
    directory: Path = typer.Option(
        None, "--dir", help="Project directory to freeze (default: the current one). The script and "
                            "--manifest paths are relative to it."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the preview and confirmation."),
):
    """Freeze a directory into a Recipe: the code, plus how to run it.

    Saving RUNS NOTHING — no Spawn, no Karma, no queue place. It uploads the working dir and keeps it,
    so the Recipe can be re-run later from any machine with nothing to hand.

    Unlike everything else `cuttle` uploads, this is kept indefinitely: a Spawn's project is deleted the
    moment it stages in and its results expire after 7 days, but nothing on the Head ever deletes a
    Recipe. It holds space against your 32GB allowance until you run `cuttle recipe delete`.

    Extra args after the script are frozen as the Recipe's arguments
    (`cuttle recipe save nightly train.py --time 60 --vram 24 -o checkpoints
    --manifest pyproject.toml -- --epochs 5`).
    """
    # Same shape as `cuttle run`: bare invocation walks the wizard, flag-driven one prompts for
    # nothing. `recipes._validate` is the unconditional hard gate — the block below alone can't
    # enforce "at least one output" since a flag-driven save skips it.
    wizard = name is None or script is None
    with ui.section("Save a Recipe", lambda: f"{name}  {script}"):
        name = ui.need(name, "the recipe's name", lambda: ui.ask_text("Name for this recipe?"))
        script = ui.need(script, "the script", lambda: ui.ask_text("Which script? (path)"))
        time = ui.need(time, "--time", lambda: ui.ask_int("Wall-clock limit? (minutes)"))
        vram = ui.need(vram, "--vram", lambda: ui.ask_int("GPU memory needed? (GB, total across cards)"))
        # Asked outright, never suggested — see `cuttle run`'s own prompt for why.
        manifest = ui.need(manifest, "--manifest", lambda: ui.ask_text(
            "Which dependency file? (pyproject.toml, uv.lock, requirements.txt, environment.yml)"))
        output = list(output) if output else []
        if wizard:
            while not output:
                got = ui.ask_text("Output folder name (--output) — where the script writes results")
                if got:
                    output.append(got)
                else:
                    ui.warn("at least one output folder is required")
    recipes_mod.save(name, script, minutes=time, vram=vram, gpu=gpu, args=ctx.args, outputs=output,
                     manifest=manifest, project=project, entity=entity, no_gitignore=no_gitignore,
                     yes=yes, root=spawns_mod.project_root(directory))


@recipe_app.command("update", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def recipe_update(
    ctx: typer.Context,
    name: str = typer.Argument(None, help="Recipe to re-capture."),
    script: str = typer.Option(None, "--script", help="Change the entry point too (default: keep)."),
    time: int = typer.Option(None, "--time", help="Change the wall-clock limit (default: keep)."),
    vram: int = typer.Option(None, "--vram", help="Change the TOTAL VRAM need (default: keep)."),
    gpu: int = typer.Option(None, "--gpus", "--gpu", help="Change how many GPUs to hold (default: "
                                                         "keep)."),
    output: list[str] = typer.Option(
        None, "--output", "-o", help="REPLACE the declared outputs (repeatable; default: keep)."),
    manifest: str = typer.Option(
        None, "--manifest", help="Change the dependency file the environment is built from "
                                 "(default: keep)."),
    project: str = typer.Option(None, "--project", help="Change the W&B project (default: keep)."),
    entity: str = typer.Option(None, "--entity", help="Change the W&B entity (default: keep)."),
    no_gitignore: bool = typer.Option(
        False, "--no-gitignore", help="Upload the whole working dir (default: honor .gitignore)."),
    directory: Path = typer.Option(
        None, "--dir", help="Project directory to re-capture (default: the current one)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the preview and confirmation."),
):
    """Re-freeze a Recipe around a directory's current code.

    Every setting keeps what the Recipe already holds unless you pass a flag, so a bare
    `cuttle recipe update nightly` refreshes the CODE and changes nothing else.

    The Recipe keeps running its old snapshot until this lands. On the Head an update is
    delete-then-save: the old copy's bytes come off your allowance before the new ones are charged, so
    this is measured by what it GROWS BY — and if it doesn't fit, the old snapshot is left untouched.
    """
    name = ui.need(name, "the recipe's name", lambda: ui.ask_text("Which recipe to update?"))
    recipes_mod.update(name, script=script, minutes=time, vram=vram, gpu=gpu,
                       args=ctx.args or None, outputs=list(output) if output else None,
                       manifest=manifest, project=project, entity=entity,
                       no_gitignore=no_gitignore, yes=yes,
                       root=spawns_mod.project_root(directory))


@recipe_app.command("run")
def recipe_run(name: str = typer.Argument(None, help="Recipe to run.")):
    """Run a Recipe's frozen snapshot as a Spawn — one call, from any machine, nothing to hand.

    Every setting was already answered when the Recipe was saved, so this sends nothing but the name.
    """
    name = ui.need(name, "the recipe's name", lambda: ui.ask_text("Which recipe to run?"))
    recipes_mod.run(name)


@recipe_app.command("delete")
def recipe_delete(
    name: str = typer.Argument(None, help="Recipe to delete."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
):
    """Delete a Recipe and free its bytes. The only thing that ever removes one — Recipes never expire
    and are never evicted, so your shelf only shrinks when you say so."""
    name = ui.need(name, "the recipe's name", lambda: ui.ask_text("Which recipe to delete?"))
    recipes_mod.delete(name, yes=yes)


# --- workspaces ---------------------------------------------------------------
# A **Workspace** is the durable form of a Tide Pool: a working directory the Head keeps, so opening it
# again resumes where the last visit stopped. Sibling of `recipe` above — a Recipe is frozen and re-run
# in batch, a Workspace is mutable and visited interactively. See cuttle.workspaces.

@workspace_app.command("list")
def workspace_list(as_json: bool = typer.Option(False, "--json", help="Machine-readable output.")):
    """Your workspaces: what each holds against its space, when you last visited, and which is live."""
    workspaces_mod.listing(as_json=as_json)


@workspace_app.command("new")
def workspace_new(
    name: str = typer.Argument(None, help="Name for the workspace — how you open it later. Yours "
                                          "only, so it need only be unique among your own."),
    storage: int = typer.Option(None, "--storage", help="How much this workspace may hold, in GB, taken "
                                                    "out of your 32GB workspace allowance when you "
                                                    "create it. Required — nobody else can guess it."),
    time: int = typer.Option(None, "--time", help="Wall-clock limit for a VISIT, in minutes. The "
                                                  "workspace persists; a session does not."),
    vram: int = typer.Option(None, "--vram", help="TOTAL GPU memory a visit needs, in GB."),
    gpu: int = typer.Option(1, "--gpus", "--gpu", help="GPUs a visit holds at once, all on one "
                                                      "machine."),
    directory: Path = typer.Option(
        None, "--dir", help="Directory to seed it with (default: the current one). This is the ONLY "
                            "upload — after it, the workspace changes from inside its own sessions."),
    manifest: str = typer.Option(
        None, "--manifest", help="Dependency file a visit builds its environment from. Optional, like "
                                 "a tide pool's — a workspace opened to poke at a box declares none."),
    output: list[str] = typer.Option(
        None, "--output", "-o", help="Folder under $CCC_OUT a visit writes results to (repeatable). "
                                     "Optional. $CCC_OUT is a folder INSIDE the workspace, so an "
                                     "output is kept like everything else AND sent home at the end of "
                                     "the session for `cuttle outputs` to fetch."),
    feature: list[str] = typer.Option(
        None, "--feature", help="Require an Arm Feature for visits (repeatable)."),
    no_gitignore: bool = typer.Option(
        False, "--no-gitignore", help="Seed with the whole directory (default: honor .gitignore)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the preview and confirmation."),
):
    """Make a directory into a workspace: a working dir the Head keeps between sessions.

    Unlike `cuttle run --tidepool`, nothing here is thrown away when the session ends. The tree is
    mirrored back to the Head as you work and again when you leave, so `cuttle workspace open` picks up
    exactly where you stopped — including anything you installed, downloaded or half-finished.

    Creating one costs no Karma; VISITING one does, exactly like any other Spawn. What it costs instead
    is storage: `--storage` is carved out of your 32GB workspace allowance the moment you create it, and
    it is yours to divide up. A session that fills its storage stops being saved and says so — nothing
    is ever evicted to make room.
    """
    output = list(output) if output else []
    feature = list(feature) if feature else []
    with ui.section("New workspace", lambda: f"{name}  {storage}GB"):
        name = ui.need(name, "the workspace's name", lambda: ui.ask_text("Name for this workspace?"))
        # No default, deliberately, and it is the one field only its owner can answer: this Allowance
        # is the one CCC hands over to be divided up, so a default would be CCC dividing it for them.
        storage = ui.need(storage, "--storage",
                          lambda: ui.ask_int("How much storage? (GB, out of your 32GB)"))
        time = ui.need(time, "--time", lambda: ui.ask_int("Wall-clock limit per visit? (minutes)"))
        vram = ui.need(vram, "--vram", lambda: ui.ask_int("GPU memory needed? (GB, total across cards)"))
    workspaces_mod.new(name, storage_gb=storage, vram=vram, minutes=time, gpu=gpu, manifest=manifest,
                       outputs=output, features=feature,
                       root=spawns_mod.project_root(directory), no_gitignore=no_gitignore, yes=yes)


@workspace_app.command("open")
def workspace_open(
    name: str = typer.Argument(None, help="Workspace to open."),
    time: int = typer.Option(None, "--time", help="Override the wall-clock limit for THIS visit."),
    vram: int = typer.Option(None, "--vram", help="Override the VRAM need for THIS visit."),
    gpu: int = typer.Option(None, "--gpus", "--gpu", help="Override the GPU count for THIS visit."),
    manifest: str = typer.Option(None, "--manifest", help="Override the dependency file for THIS visit."),
    output: list[str] = typer.Option(None, "--output", "-o", help="Override the outputs for THIS visit."),
    feature: list[str] = typer.Option(None, "--feature", help="Override required Features for THIS visit."),
):
    """Open a workspace and drop into its shell, resuming where the last visit left off.

    A visit is a Tide Pool bound to the workspace: same queue, same wait for a card, same Karma. What
    differs is that everything you leave behind is still there next time.

    A workspace holds ONE visit at a time. Opening one that is already live ATTACHES to that session
    rather than starting a second — two sessions writing one tree would each overwrite the other.

    Flags here override the workspace's own declaration for this session only; `cuttle workspace
    update` is what changes it for good.
    """
    name = ui.need(name, "the workspace's name",
                   lambda: ui.ask_text("Which workspace to open?"))
    workspaces_mod.open_(name, vram=vram, minutes=time, gpu=gpu, manifest=manifest,
                         outputs=list(output) if output else None,
                         features=list(feature) if feature else None)


@workspace_app.command("update")
def workspace_update(
    name: str = typer.Argument(None, help="Workspace to change."),
    storage: int = typer.Option(None, "--storage", help="Resize it, in GB. Grows if your allowance has "
                                                        "room; never shrinks below what the tree holds."),
    time: int = typer.Option(None, "--time", help="Change the per-visit wall-clock limit."),
    vram: int = typer.Option(None, "--vram", help="Change the per-visit VRAM need."),
    gpu: int = typer.Option(None, "--gpus", "--gpu", help="Change how many GPUs a visit holds."),
    manifest: str = typer.Option(None, "--manifest", help="Change the dependency file visits build "
                                                          "from (pass '' to declare none)."),
    output: list[str] = typer.Option(None, "--output", "-o", help="REPLACE the declared outputs."),
    feature: list[str] = typer.Option(None, "--feature", help="REPLACE the required Features."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
):
    """Change what a visit asks for, and how much the workspace may hold.

    This never touches the files — a workspace is seeded once and written to from inside its own
    sessions after that. Every setting keeps what it already holds unless you pass a flag.
    """
    name = ui.need(name, "the workspace's name", lambda: ui.ask_text("Which workspace to update?"))
    workspaces_mod.update(name, storage_gb=storage, vram=vram, minutes=time, gpu=gpu, manifest=manifest,
                          outputs=list(output) if output else None,
                          features=list(feature) if feature else None, yes=yes)


@workspace_app.command("download")
def workspace_download(
    name: str = typer.Argument(None, help="Workspace to download."),
    subpath: str = typer.Argument(None, help="A folder inside it (default: the whole tree)."),
    dest: Path = typer.Option(None, "--dest", help="Where to write the tar (default: here)."),
):
    """Download a workspace, or one folder inside it, as a tar.

    Works while a visit is live — you just get the tree as of the last mirror rather than the instant
    the session is at.
    """
    name = ui.need(name, "the workspace's name", lambda: ui.ask_text("Which workspace to download?"))
    workspaces_mod.download(name, subpath, dest=dest)


@workspace_app.command("delete")
def workspace_delete(
    name: str = typer.Argument(None, help="Workspace to delete."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the typed confirmation."),
):
    """Delete a workspace and free its space. The only thing that ever removes one — workspaces never
    expire and are never evicted.

    You have to type the name back rather than answering y/N: this is the one thing CCC holds that has
    no expiry, no eviction and no second copy anywhere.
    """
    name = ui.need(name, "the workspace's name", lambda: ui.ask_text("Which workspace to delete?"))
    workspaces_mod.delete(name, yes=yes)


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
            ui.warn("this removes Slurm, the mesh, head.db (every account, Balance and ledger row) "
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


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(
    ctx: typer.Context,
    script: str = typer.Argument(
        None, help="Python script to run inside the Pod. With --tidepool: a Spawn id to attach/reattach "
                   "to instead of requesting a new one (unused otherwise)."),
    tidepool: bool = typer.Option(
        False, "--tidepool", help="A live interactive shell instead of a script — see "
                                  "docs/elements/tide-pool.md. Requests one (or attaches to an existing id given as "
                                  "the positional arg), waits for placement, and drops you into it. "
                                  "Disconnecting does NOT end it — `cuttle cancel <id>` does, or "
                                  "--time running out."),
    time: int = typer.Option(
        None, "--time", help="Wall-clock limit, in MINUTES. Required. The Spawn is killed at this "
                            "limit, and Karma is escrowed against it — over-estimating costs you "
                            "only queue position, and is refunded when the Spawn ends."),
    vram: int = typer.Option(
        None, "--vram", help="TOTAL GPU memory this Spawn needs, in GB, across the cards you asked "
                            "for. Required. Every card holding its share is eligible; Slurm takes the "
                            "fastest set that is free."),
    gpu: int = typer.Option(
        1, "--gpus", "--gpu", help="GPUs to hold at once, all on one machine. --vram is the total "
                                   "across them. Refused if no Arm has this many."),
    name: str = typer.Option(
        None, "--name", help="What to call this Spawn in the queue and the archive. Required — CCC "
                             "never invents one. Free text (48 chars): \"lr sweep 3e-4\" is as valid "
                             "as \"sweep3\". A Recipe run or a Workspace visit needs none; it takes "
                             "the name you already gave that Recipe or Workspace."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream output until the Spawn ends."),
    project: str = typer.Option(None, "--project", help="W&B project for this Spawn (default: config)."),
    entity: str = typer.Option(
        None, "--entity", help="W&B entity/slug for this Spawn (default: config, then account default)."),
    no_gitignore: bool = typer.Option(
        False, "--no-gitignore", help="Upload the whole working dir (default: honor .gitignore)."),
    directory: Path = typer.Option(
        None, "--dir", help="Project directory to upload (default: the current one). The script, "
                            "--manifest and --output paths are all relative to it. Results still "
                            "download to where you are, not to it."),
    node: str = typer.Option(
        None, "--node", help="ADMIN ONLY: pin the Spawn to one Arm by name. Users declare what they "
                             "need (--vram), never which machine they want."),
    output: list[str] = typer.Option(
        None, "--output", "-o", help="Name of a subdir under $CCC_OUT your script writes results to "
                                     "(repeatable). Required — at least one (optional, with a y/n "
                                     "warning, for --tidepool). Anything your script writes OUTSIDE "
                                     "these dirs is still recoverable for a day after the Spawn ends "
                                     "(`cuttle recover`) — but declare the right dirs here rather than "
                                     "relying on it."),
    feature: list[str] = typer.Option(
        # No short alias: `-f` is already `--follow` on this command.
        None, "--feature", help="Require an Arm Feature (repeatable). Optional — most Spawns need "
                                "none; unsatisfiable requests are refused, not queued."),
    manifest: str = typer.Option(
        None, "--manifest", help="REQUIRED (optional with --tidepool). Dependency file to build this "
                                 "Spawn's Python environment from, relative to the project root — "
                                 "pyproject.toml, uv.lock, requirements.txt, environment.yml, or one "
                                 "you keep under another name (envs/cuda12.yml). There is no default "
                                 "and nothing is detected: the environment carries its own Python and "
                                 "inherits nothing, so it is yours to name. With --tidepool it is "
                                 "optional, and passing one uploads this directory too."),
    no_download: bool = typer.Option(
        False, "--no-download", help="Do NOT fetch this Spawn's results automatically. By default "
                                     "they are downloaded into ~/ccc/outputs/spawn-<id>-outputs/ as "
                                     "soon as it finishes, whether or not this command is running."),
    dest: Path = typer.Option(
        None, "--dest", help="Where to put this Spawn's results (default: ~/ccc/outputs)."),
):
    """Submit a Spawn: uploads the working dir to the Head, which builds the environment you
    declared and runs your script against it inside a Pod. Requires `cuttle login`. Extra args after the
    script are passed through (e.g. `cuttle run train.py --time 60 --vram 24 -o checkpoints -- --epochs 5`).

    --time and --vram have no defaults on purpose. Nothing can price a Spawn whose length nobody
    declared, and a --vram that defaulted to the fleet's biggest card would make every untouched
    field eligible for that one GPU alone — queued behind everything, at the highest Demand, for work
    that may need 2 GB.

    --output/-o has no default either, and none is offered anywhere: a Spawn nobody named an output
    for is one whose results (docs/elements/spawns.md) have nowhere
    retrievable to land — `mkdir -p "$CCC_OUT/<name>"` runs before your script does, once per name.

    A wrong or incomplete --output isn't fatal the instant the Spawn ends: the Recovery Window
    (docs/elements/storage.md) keeps the whole working dir the Spawn ran in — everything outside your declared dirs too —
    recoverable on the Head for a day afterward (`cuttle recover <id>`), up to 16GB. Past that, or
    past the window, it's gone — declare `--output` correctly rather than leaning on it.

    What you DID declare is downloaded automatically, into ~/ccc/outputs/spawn-<id>-outputs/, as the
    Spawn finishes — you do not have to be running this command, or logged into this terminal, or even
    have this machine switched on at the time (it resumes next time you run `cuttle`). `--no-download`
    turns that off for one Spawn.

    On the Head that same copy is kept 7 days, and only while your 32GB of output space has room for
    it — past either, `cuttle outputs <id>` says so rather than 404ing (docs/elements/storage.md).
    Nothing on the Head is an archive; the automatic download is what makes that safe.

    --tidepool replaces the whole script/output/upload story above with a live shell — see
    cuttle.tidepool for that flow; everything below this point is the ordinary script path.
    """
    output = list(output) if output else []
    feature = list(feature) if feature else []
    # Resolved once, before anything is validated or packed, so every path below means the same thing.
    root = spawns_mod.project_root(directory)
    if tidepool:
        # --manifest is OPTIONAL here and nowhere else, so it is passed through rather than prompted
        # for: a Tide Pool opened to poke at a box has no project behind it to name a file in. Naming
        # one uploads the project directory and builds that Environment into the shell.
        if manifest and not (root / manifest).is_file():
            raise ui.abort(f"manifest {manifest!r} was not found (looked for it relative to the "
                           f"project directory, {root})")
        return tidepool_mod.run(script, minutes=time, vram=vram, gpu=gpu, node=node, outputs=output,
                                features=feature, follow=follow, download=not no_download,
                                manifest=manifest, no_gitignore=no_gitignore, root=root, name=name)
    wizard = script is None
    # The card count shows only past one: at a single Device it is what every Spawn does, and `24G`
    # already IS the per-card figure, so printing it would add a word and say nothing.
    with ui.section("Submit a Spawn",
                    lambda: f"{script}  {time}min  {vram}G" + (f"  {gpu} GPUs" if gpu > 1 else "")):
        # No file picker: you pass the path. Prompting through a directory listing is slower than
        # typing a name you already know, and wrong the moment the script lives in a subdirectory.
        script = ui.need(script, "the script", lambda: ui.ask_text("Which script? (path)"))
        # Required exactly as --time and --vram are, and asked the same way: a person types it, or a
        # scripted run that gave none is refused. CCC has nothing to fall back to — a Name it chose
        # would be a Name nobody chose, which is the whole point of requiring one.
        name = ui.need(name, "--name", lambda: ui.ask_text("What should this Spawn be called?"))
        # --time and --vram get no pre-filled default, deliberately: a default --vram of the
        # biggest card would make every untouched field eligible for it alone — queued at the
        # highest Demand for work that may need 2 GB.
        time = ui.need(time, "--time", lambda: ui.ask_int("Wall-clock limit? (minutes)"))
        vram = ui.need(vram, "--vram", lambda: ui.ask_int("GPU memory needed? (GB, total across cards)"))
        # Asked outright, never suggested. Offering what happens to be lying in the directory would be
        # CCC inferring which file is the instruction — and a suggestion is an inference, accepted by
        # whoever presses Enter. The Environment carries its own Python and inherits nothing, so which
        # file it is built from decides everything the work runs against; that is the User's to say.
        manifest = ui.need(manifest, "--manifest", lambda: ui.ask_text(
            "Which dependency file? (pyproject.toml, uv.lock, requirements.txt, environment.yml)"))
        if wizard:
            gpu = ui.ask_int("How many GPUs? (all on one machine)", default=str(gpu))
            project = project or (ui.ask_text("W&B project? (blank = config default)") or None)
            entity = entity or (ui.ask_text("W&B entity? (blank = config default)") or None)
            no_gitignore = not ui.ask_confirm("Honor .gitignore when uploading?", True)
            follow = follow or ui.ask_confirm("Stream output until it ends?", False)
            # Collected here, like every other wizard field — but never pre-filled, and the hard gate
            # below still runs regardless of how these were collected. At least one is required before
            # the loop lets you move on; blank only ends it once you have one.
            # `folder`/`feat`, not `name`: that is this command's own --name parameter now, and a loop
            # variable would quietly overwrite the Spawn's Name with the last output folder typed.
            while not output:
                folder = ui.ask_text("Output folder name (--output) — where your script writes results")
                if folder:
                    output.append(folder)
                else:
                    ui.warn("at least one output folder is required")
            while ui.ask_confirm("Add another output folder?", False):
                folder = ui.ask_text("Output folder name")
                if folder:
                    output.append(folder)
            # Optional, never gates submission. Offered from the built-in catalog
            # (config.FEATURE_NAMES) — a constant, so no Head call and no failure mode here. Whether
            # an Arm actually carries a Feature is checked at submit, not here.
            if not feature and ui.ask_confirm("Require any Arm Features?", False):
                for feat in config.FEATURE_NAMES:
                    if ui.ask_confirm(f"Require {feat!r}?", False):
                        feature.append(feat)
    # Hard validation, unconditional — NOT gated on `wizard`. A flag-driven `cuttle run` never passes
    # through the wizard block above, so this is the only gate a direct invocation ever sees. No y/n
    # override for either: a Spawn with no output declared or a script that isn't there is refused, not
    # warned about.
    if not output:
        raise ui.abort("at least one --output/-o is required — name the folder(s) under $CCC_OUT your "
                       "script writes results to (e.g. `--output checkpoints`)")
    if not (root / script).is_file():
        raise ui.abort(f"script {script!r} was not found (looked for it relative to the project "
                       f"directory, {root})")
    # Same shape as the script check, and for the same reason: caught here rather than as a 422 after
    # the whole project directory has been uploaded. The Head re-checks it against what actually
    # arrived, which is the check that counts — .gitignore can drop a file that is right here.
    if not (root / manifest).is_file():
        raise ui.abort(f"manifest {manifest!r} was not found (looked for it relative to the project "
                       f"directory, {root})")
    if wizard:
        spawns_mod.preview(script, minutes=time, vram=vram, gpu=gpu, no_gitignore=no_gitignore,
                          outputs=output, features=feature, manifest=manifest, root=root)
        if not ui.ask_confirm("Submit?", True):
            raise ui.abort("not submitted")
    spawns_mod.submit(script, gpu=gpu, follow=follow, args=ctx.args, project=project, entity=entity,
                      no_gitignore=no_gitignore, node=node, minutes=time, vram=vram, outputs=output,
                      features=feature, manifest=manifest, download=not no_download, dest=dest,
                      root=root, name=name)


@app.command()
def cancel(
    spawn_id: int = typer.Argument(None, help="Spawn (Slurm job) id."),
    any_owner: bool = typer.Option(
        False, "--any",
        help="Cancel a Spawn you don't own. Admin, and ONLY on the Head (uses sudo scancel)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
):
    """End a Spawn early. Your own from anywhere; anyone's only on the Head, with --any."""
    spawn_id = ui.need(spawn_id, "the Spawn id",
                       lambda: pickers.pick_spawn("Which Spawn to cancel?", mine_only=not any_owner))
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


@app.command()
def outputs(spawn_id: int = typer.Argument(None, help="Spawn (Slurm job) id.")):
    """Download a Spawn's $CCC_OUT dir as a tar into the current directory."""
    spawn_id = ui.need(spawn_id, "the Spawn id", lambda: pickers.pick_spawn("Download which Spawn?"))
    spawns_mod.outputs(spawn_id)


@app.command()
def recover(spawn_id: int = typer.Argument(None, help="Spawn (Slurm job) id.")):
    """Download a Spawn's Recovery Window copy — everything it wrote OUTSIDE its declared $CCC_OUT,
    while the day-long window since it ended is still open. See `--output`'s help on `cuttle run` for
    what this is a safety net for; `cuttle outputs` is the separate, permanent $CCC_OUT download."""
    spawn_id = ui.need(spawn_id, "the Spawn id", lambda: pickers.pick_spawn("Recover which Spawn?"))
    spawns_mod.recover(spawn_id)


def main() -> None:
    ui.run_app(app)


if __name__ == "__main__":
    main()
