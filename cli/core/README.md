# cli/core — `cuttle` (Python)

The Python (`typer`) core of the `cuttle` front door. MVP scope: single-node local cluster.

## Dev

```bash
cd cli/core
uv sync
uv run cuttle --help
```

## Commands

- `cuttle setup --local` — provision **this** box as a combined Head+Arm (native Ubuntu 24.04,
  x86_64, NVIDIA GPU). Drives `apt`+`sudo`, renders `/etc/slurm/{slurm,gres}.conf`, starts
  `munge`+`slurmctld`+`slurmd`, builds `~/.ccc/images/base.sif`. **Modifies the system (sudo).**
  Idempotent — re-run to reload config.
- `cuttle teardown --local` — reverse setup: stop+disable daemons, purge apt packages + repos,
  remove Slurm config/state. Keeps `~/.ccc` (base.sif) by default. `--keep-packages` for a fast
  config-only reset; `--all` to also delete `~/.ccc`. Tolerant of partial state (safe to re-run).
  Purges **only** packages setup recorded as newly installed (`~/.ccc/config/apt-installed.txt`) —
  never pre-existing packages, and no global `autoremove` (won't touch unrelated system orphans).

- `cuttle run <script> [--gpu N] [-f] [-- ARGS]` — submit a **Spawn**: `sbatch` → `apptainer exec
  --nv base.sif python <script>`. Prints `submitted spawn <id>` (job id) and detaches; `-f`/`--follow`
  streams stdout until it ends. W&B env injected from config. Slurm job id = spawn id.
- `cuttle status <id>` — queued/running state, or `DONE exit N` after it finishes (read from the
  `<id>.exit` file the wrapper writes — survives `scontrol` forgetting the job; no slurmdbd).
- `cuttle logs <id> [-f]` — print/tail the Spawn's stdout (`~/.ccc/spawns/<id>.out`).

Config: `cuttle setup` prompts for the W&B API key + project → `~/.ccc/config/config.yaml` (0600).
Spawn artifacts: `~/.ccc/spawns/<id>.{out,err,exit}`.

Pinned versions live in `src/cuttle/config.py` (mirrored in `docs/TODO.md`).
