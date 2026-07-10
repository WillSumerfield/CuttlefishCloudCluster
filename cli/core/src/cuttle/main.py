"""`cuttle` CLI entrypoint."""

import typer

from . import enroll as enroll_mod
from . import setup as setup_mod
from . import spawns as spawns_mod
from . import teardown as teardown_mod

app = typer.Typer(no_args_is_help=True, add_completion=False, help="CCC front door.")
arm_app = typer.Typer(no_args_is_help=True, help="Manage Arms (run on the Head).")
app.add_typer(arm_app, name="arm")


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
    invite: str = typer.Option(None, "--invite", help="Arm: invite printed by `cuttle invite`."),
    re_enroll: bool = typer.Option(
        False, "--re-enroll",
        help="Arm: discard this box's mesh identity and re-join from scratch (needs re-approval). "
             "For a corrupt state file, or a Head rebuilt behind the same coordinator URL.",
    ),
):
    """Provision a node. --head (or --local) for the Head; --arm to enroll a compute node."""
    if head or local:
        if re_enroll:
            raise typer.BadParameter("--re-enroll is an Arm flag; the Head re-registers itself")
        setup_mod.setup_head(address)
    elif arm:
        if not invite:
            raise typer.BadParameter("`--arm` needs --invite (get one from the Head: `cuttle invite`)")
        setup_mod.setup_arm(invite, re_enroll=re_enroll)
    else:
        raise typer.BadParameter("specify a role: cuttle setup --head | --arm")


@app.command()
def invite() -> None:
    """Head: print the cluster invite (coordinator URL + Head mesh IP).

    Not a credential — it contains no secret and can be published. It lets a machine *ask* to join;
    only `cuttle arm approve` lets it in.
    """
    print(enroll_mod.mint_invite())


@arm_app.command("list")
def arm_list():
    """Show enrolled Arms and whether they've been admitted."""
    enroll_mod.list_arms()


@arm_app.command("approve")
def arm_approve(
    name: str = typer.Argument(..., help="The Arm's node name (what its owner chose)."),
    auth_id: str = typer.Option(..., "--auth-id", help="Auth-id the Arm printed while waiting."),
):
    """Approve an Arm: grant its pending mesh admission, then give it a GRES line and reconfigure
    Slurm. The cluster's front door, and the only privileged step in enrollment."""
    enroll_mod.approve_arm(name, auth_id, setup_mod._write_root_file)


@arm_app.command("remove")
def arm_remove(name: str = typer.Argument(..., help="The Arm's node name.")):
    """Delete an Arm from Slurm, the registry, AND the mesh. All three, or the departed donor keeps
    a live mesh address — and mesh membership is what authorizes fetching the munge key."""
    enroll_mod.remove_arm(name, setup_mod._write_root_file)


@app.command()
def teardown(
    local: bool = typer.Option(
        False, "--local", help="Tear down this box's combined Head+Arm (MVP)."
    ),
    keep_packages: bool = typer.Option(
        False, "--keep-packages", help="Leave apt packages + repos installed (fast config reset)."
    ),
    all_: bool = typer.Option(
        False, "--all", help="Also remove ~/.ccc (deletes base.sif — full clean slate)."
    ),
):
    """Reverse setup. MVP supports --local only."""
    if not local:
        raise typer.BadParameter("MVP supports only: cuttle teardown --local")
    teardown_mod.teardown_local(keep_packages=keep_packages, all_=all_)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(
    ctx: typer.Context,
    script: str = typer.Argument(..., help="Python script to run inside the Pod."),
    gpu: int = typer.Option(1, "--gpu", help="GPUs to request (GRES gpu:N)."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream output until the Spawn ends."),
):
    """Submit a Spawn: sbatch → apptainer exec --nv base.sif python <script>. Extra args after
    the script are passed through (e.g. `cuttle run train.py --gpu 1 -- --epochs 5`)."""
    spawns_mod.submit(script, gpu=gpu, follow=follow, args=ctx.args)


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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
