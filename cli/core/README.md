# cli/core — `cuttle` (Python)

The Python (`typer`) core of the `cuttle` front door: provisions nodes, enrolls remote Arms, submits
Spawns. (A host-native Go launcher is in the design — see `docs/DesignDoc.md` — but not yet built.)

## Dev

```bash
cd cli/core
uv sync
uv run cuttle --help
```

## Commands

### Setup / teardown
- `cuttle setup --head [--address <host>]` — provision **this** box as the Head: self-hosted Headscale
  coordinator + isolated `tailscaled-ccc` mesh, `slurmctld` + munge, the mesh-only enroll API, the
  LAN+mesh web dashboard, rsync stage-in/out, and a co-located Arm. Builds `~/.ccc/images/base.sif`.
  `--address` = the coordinator's advertised host (default: primary IP; use a host remote Arms can
  actually reach). `--local` is an alias for the single-box case. Idempotent.
- `cuttle setup --arm --invite <blob> [--re-enroll]` — enroll **this** box as a remote Arm: join the
  mesh (blocks for operator approval), fetch the munge key, register a dynamic `slurmd -Z`. On WSL it
  also forwards the Head dashboard to `localhost` for the Windows browser.
- `cuttle teardown --local | --arm [--keep-packages] [--all]` — reverse setup. Default keeps `~/.ccc`
  (base.sif); `--keep-packages` = fast config-only reset; `--all` also deletes `~/.ccc`. Purges only
  the packages setup recorded (`~/.ccc/config/apt-installed.txt`). Never touches personal Tailscale.

### Enrollment (Head-side)
- `cuttle invite` — print the cluster invite (Coordinator URL + Head mesh IP). Public, not a secret.
- `cuttle arm list` — enrolled Arms and whether they're admitted.
- `cuttle arm approve <name> --auth-id <id>` — grant a pending Arm's mesh + Slurm admission.
- `cuttle arm remove <name>` — delete an Arm from Slurm, the registry, and the mesh.

### Spawns
- `cuttle run <script> [--gpu N] [-f] [--project P] [--entity E] [-- ARGS]` — submit a **Spawn**:
  `sbatch` → `apptainer exec --nv base.sif python <script>`. Stages the project in/out for remote
  Arms. `--project`/`--entity` set W&B per-Spawn. `-f`/`--follow` streams stdout. Job id = spawn id.
- `cuttle status <id>` — queued/running, or `DONE exit N` (read from `~/.ccc/spawns/<id>/exit`, which
  survives `scontrol` forgetting the job — no slurmdbd yet).
- `cuttle logs <id> [-f]` — print/tail the Spawn's stdout (`~/.ccc/spawns/<id>/out`).

## Dashboard
`cuttle setup --head` runs a read-only web dashboard (Vite/React) at `http://<head>:8082/`, reachable
over the LAN and the CCC mesh — live Arms + Spawn queue. Enrollment stays on a separate **mesh-only**
API (`:8081`) so the munge-key handout never touches the LAN.

## Config / artifacts
- `cuttle setup` prompts for the W&B API key + default project/entity → `~/.ccc/config/config.yaml` (0600).
- Per-Spawn artifacts: `~/.ccc/spawns/<id>/{out,err,exit,outputs}`.
- Pinned versions live in `src/cuttle/config.py` (mirrored in `docs/TODO.md`).
