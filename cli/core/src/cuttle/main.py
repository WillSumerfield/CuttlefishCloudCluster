"""`cuttle` CLI entrypoint."""

import typer

from . import auth as auth_mod
from . import enroll as enroll_mod
from . import karma as karma_mod
from . import setup as setup_mod
from . import spawns as spawns_mod
from . import teardown as teardown_mod

app = typer.Typer(no_args_is_help=True, add_completion=False, help="CCC front door.")
arm_app = typer.Typer(no_args_is_help=True, help="Manage Arms (run on the Head).")
app.add_typer(arm_app, name="arm")
user_app = typer.Typer(no_args_is_help=True, help="Manage users (run on the Head).")
app.add_typer(user_app, name="user")
# `cuttle karma` with no subcommand = the standings (invoke_without_command + the callback below).
karma_app = typer.Typer(invoke_without_command=True, help="Karma: standings, override, queue bump.")
app.add_typer(karma_app, name="karma")


@app.callback()
def _root() -> None:
    """CCC front door."""


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
    else:
        raise typer.BadParameter("specify a role: cuttle setup --head | --arm")


@arm_app.command("list")
def arm_list():
    """Show Arms: who owns them, and whether they're authorized/enrolled/admitted."""
    enroll_mod.list_arms()


@arm_app.command("authorize")
def arm_authorize(
    name: str = typer.Argument(..., help="Name for the new Arm (its identity in Slurm + the mesh)."),
    user: str = typer.Option(..., "--user", help="The User who owns (donates) the machine."),
):
    """Authorize one machine to join as an Arm. Prints the enrollment token its owner redeems with
    `cuttle setup --arm --token <blob>`, then waits for them and admits them to Slurm.

    The token is a SECRET — it admits one machine, once, within the hour. Send it privately.
    """
    enroll_mod.authorize_arm(name, user, setup_mod._write_root_file)


@arm_app.command("admit")
def arm_admit(name: str = typer.Argument(..., help="The Arm's node name.")):
    """Give an enrolled Arm its GRES line and reconfigure Slurm. Only needed if `arm authorize` gave
    up waiting because the donor hadn't run setup yet — this finishes the job."""
    enroll_mod.admit_arm(name, setup_mod._write_root_file)


@arm_app.command("benchmark")
def arm_benchmark(name: str = typer.Argument(..., help="The Arm's node name.")):
    """Re-measure an Arm's **Power** (throughput) and re-price it. Runs the matmul benchmark inside
    the Pod, on that node, and sets its Slurm Weight — so the fastest free card wins.

    Normally part of `arm authorize`; run it by hand after a driver/GPU change, or if the benchmark
    timed out because the donor's box wasn't ready.
    """
    enroll_mod.benchmark_arm(name)


@arm_app.command("chown")
def arm_chown(
    name: str = typer.Argument(..., help="The Arm's node name."),
    user: str = typer.Option(..., "--user", help="The User who owns (donates) the machine."),
):
    """Re-point an Arm at a different owner. Touches neither the mesh nor Slurm, so the donor's box
    keeps running."""
    enroll_mod.chown_arm(name, user)


@arm_app.command("remove")
def arm_remove(name: str = typer.Argument(..., help="The Arm's node name.")):
    """Delete an Arm from Slurm, the registry, AND the mesh. All three, or the departed donor keeps
    a live mesh address — and mesh membership is what authorizes fetching the munge key."""
    enroll_mod.remove_arm(name, setup_mod._write_root_file)


@app.command()
def login(
    head: str = typer.Option(
        None, "--head", help="Head API URL (e.g. http://<host>:8082); saved for next time."),
):
    """Log in to the Head and store a session token (~/.ccc/credentials). Forces a password change on
    first login."""
    auth_mod.login(head)


@app.command()
def logout():
    """Revoke this box's session on the Head and delete the local token."""
    auth_mod.logout()


@app.command()
def whoami():
    """Print the currently logged-in user."""
    auth_mod.whoami()


@app.command()
def passwd():
    """Change your password."""
    auth_mod.passwd()


@user_app.command("add")
def user_add(
    username: str = typer.Argument(..., help="New user's login name."),
    admin: bool = typer.Option(False, "--admin", help="Grant admin."),
):
    """Create a user (prints a one-time temp password they must change on first login)."""
    auth_mod.user_add(username, admin=admin)


@user_app.command("list")
def user_list():
    """List users."""
    auth_mod.user_list()


@user_app.command("remove")
def user_remove(username: str = typer.Argument(..., help="User to delete (and their sessions).")):
    """Delete a user."""
    auth_mod.user_remove(username)


@karma_app.callback()
def karma(ctx: typer.Context):
    """Show every User's standing: Balance, Escrow, Share, and the queue nice it implies.

    Everyone's, not just yours — Karma ranks Users against each other, so a Balance in isolation says
    nothing. Karma never gates: poor standing loses contention, and loses nothing on an idle cluster.
    """
    if ctx.invoked_subcommand is None:
        karma_mod.standings()


# ignore_unknown_options: a NEGATIVE amount is this command's main use, and `override bob -50000`
# otherwise dies with `No such option: -5` — click reads any leading `-` as a flag. Without this the
# only spelling that works is `override --note x bob -- -50000`, which nobody will ever guess.
@karma_app.command("override", context_settings={"ignore_unknown_options": True})
def karma_override(
    username: str = typer.Argument(..., help="The User whose Balance to change."),
    amount: float = typer.Argument(..., help="Karma to add (or remove, if negative). 1 Karma = one "
                                             "TFLOP-hour of device time."),
    note: str = typer.Option(None, "--note", help="Why — recorded in the ledger."),
):
    """ADMIN: add/remove Karma from a User's Balance. Changes *standing*, so it is felt in every future
    contention (and Decay absorbs it over ~a month). To speed up one Spawn once, use `karma bump`."""
    karma_mod.override(username, amount, note)


@karma_app.command("bump")
def karma_bump(
    spawn_id: int = typer.Argument(..., help="Spawn (Slurm job) id — must still be queued."),
    clear: bool = typer.Option(False, "--clear", help="Undo the bump; the economy takes it back."),
):
    """ADMIN, ON THE HEAD: shove one queued Spawn up the queue. Touches no one's Karma and leaves no
    ledger row — it bypasses the economy rather than participating in it. Bump again to go further.

    Runs here rather than over the API because a bump is a negative nice, and Slurm grants those only
    to an operator — an authority the LAN-facing API deliberately does not have.
    """
    karma_mod.bump(spawn_id, clear=clear)


@app.command()
def teardown(
    local: bool = typer.Option(
        False, "--local", help="Tear down this box's combined Head+Arm."
    ),
    arm: bool = typer.Option(
        False, "--arm", help="Tear down this box's Arm role (reverse `setup --arm`)."
    ),
    keep_packages: bool = typer.Option(
        False, "--keep-packages", help="Leave apt packages + repos installed (fast config reset)."
    ),
    all_: bool = typer.Option(
        False, "--all", help="Also remove ~/.ccc (deletes base.sif — full clean slate)."
    ),
):
    """Reverse setup: --local (Head+Arm) or --arm (remote Arm)."""
    if local and arm:
        raise typer.BadParameter("pick one role: --local or --arm")
    if local:
        teardown_mod.teardown_local(keep_packages=keep_packages, all_=all_)
    elif arm:
        teardown_mod.teardown_arm(keep_packages=keep_packages, all_=all_)
    else:
        raise typer.BadParameter("specify a role: cuttle teardown --local | --arm")


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(
    ctx: typer.Context,
    script: str = typer.Argument(..., help="Python script to run inside the Pod."),
    time: int = typer.Option(
        ..., "--time", help="Wall-clock limit, in MINUTES. Required. The Spawn is killed at this "
                            "limit, and Karma is escrowed against it — over-estimating costs you "
                            "only queue position, and is refunded when the Spawn ends."),
    vram: int = typer.Option(
        ..., "--vram", help="GPU memory this Spawn needs, in GB. Required. Every card with at least "
                            "this much is eligible; Slurm takes the fastest one that is free."),
    gpu: int = typer.Option(1, "--gpu", help="GPUs to request (GRES gpu:N)."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream output until the Spawn ends."),
    project: str = typer.Option(None, "--project", help="W&B project for this Spawn (default: config)."),
    entity: str = typer.Option(
        None, "--entity", help="W&B entity/slug for this Spawn (default: config, then account default)."),
    no_gitignore: bool = typer.Option(
        False, "--no-gitignore", help="Upload the whole working dir (default: honor .gitignore)."),
    node: str = typer.Option(
        None, "--node", help="ADMIN ONLY: pin the Spawn to one Arm by name. Users declare what they "
                             "need (--vram), never which machine they want."),
):
    """Submit a Spawn: uploads the working dir to the Head, which runs
    apptainer exec --nv base.sif python <script>. Requires `cuttle login`. Extra args after the
    script are passed through (e.g. `cuttle run train.py --time 60 --vram 24 -- --epochs 5`).

    --time and --vram have no defaults on purpose. Nothing can price a Spawn whose length nobody
    declared, and a --vram that defaulted to the fleet's biggest card would make every untouched
    field eligible for that one GPU alone — queued behind everything, at the highest Demand, for work
    that may need 2 GB.
    """
    spawns_mod.submit(script, gpu=gpu, follow=follow, args=ctx.args, project=project, entity=entity,
                      no_gitignore=no_gitignore, node=node, minutes=time, vram=vram)


@app.command()
def status(spawn_id: int = typer.Argument(..., help="Spawn (Slurm job) id.")):
    """Show a Spawn's state (queued/running, or DONE with exit code)."""
    spawns_mod.status(spawn_id)


@app.command()
def logs(
    spawn_id: int = typer.Argument(..., help="Spawn (Slurm job) id."),
    follow: bool = typer.Option(False, "--follow", "-f", help="tail -f the Spawn's stdout."),
):
    """Print (or follow) a Spawn's stdout."""
    spawns_mod.logs(spawn_id, follow=follow)


@app.command()
def outputs(spawn_id: int = typer.Argument(..., help="Spawn (Slurm job) id.")):
    """Download a Spawn's $CCC_OUT dir as a tar into the current directory."""
    spawns_mod.outputs(spawn_id)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
