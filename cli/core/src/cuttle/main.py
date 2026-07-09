"""`cuttle` CLI entrypoint."""

import typer

from . import setup as setup_mod
from . import spawns as spawns_mod
from . import teardown as teardown_mod

app = typer.Typer(no_args_is_help=True, add_completion=False, help="CCC front door.")


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
):
    """Provision a node. --head (or --local) for the Head; --arm to enroll a compute node."""
    if head or local:
        setup_mod.setup_head(address)
    elif arm:
        raise typer.BadParameter("`--arm` enrollment lands in the next step (needs --token).")
    else:
        raise typer.BadParameter("specify a role: cuttle setup --head | --arm")


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
