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
  LAN+mesh Head API + dashboard, rsync stage-in/out, the auth store, and a co-located Arm. Seeds the
  first admin account. `--address` = the coordinator's advertised host (default: primary IP; use a host
  remote Arms can actually reach). `--local` is an alias for the single-box case. Idempotent.
- `cuttle setup --arm --invite <blob> [--re-enroll]` — enroll **this** box as a remote Arm: join the
  mesh (blocks for operator approval), fetch the munge key, register a dynamic `slurmd -Z`. On WSL it
  also forwards the Head dashboard to `localhost` for the Windows browser.
- Both roles create the **Mantle** (the `ccc` account every Spawn runs as, at a uid pinned
  cluster-wide) and build `/opt/ccc/images/base.sif`.
- `cuttle teardown --local | --arm [--keep-packages] [--all]` — reverse setup. Default keeps `~/.ccc`
  and base.sif; `--keep-packages` = fast config-only reset; `--all` also deletes both. Purges only the
  packages setup recorded (`~/.ccc/config/apt-installed.txt`). Never touches personal Tailscale.

### Enrollment (Head-side)
- `cuttle invite` — print the cluster invite (Coordinator URL + Head mesh IP). Public, not a secret.
- `cuttle arm list` — enrolled Arms and whether they're admitted.
- `cuttle arm approve <name> --auth-id <id>` — grant a pending Arm's mesh + Slurm admission.
- `cuttle arm remove <name>` — delete an Arm from Slurm, the registry, and the mesh.

### Spawns
All of these go through the Head API and need `cuttle login` — the CLI is a network client, so you can
submit from any box, whether or not it ever ran `setup`.

- `cuttle run <script> [--gpu N] [-f] [--node NAME] [--project P] [--entity E] [--no-gitignore]
  [-- ARGS]` — submit a **Spawn**: uploads the working dir (honoring `.gitignore` unless
  `--no-gitignore`) to the Head, which runs `apptainer exec --nv base.sif python <script>` as the
  **Mantle**. `--node` pins it to one Arm (default: Slurm schedules it). `--project`/`--entity` set W&B
  per-Spawn. `-f` streams stdout. Job id = spawn id.
- `cuttle status <id>` — `queued`/`running`, `DONE (EXIT N)`, or `FAILED` (the Spawn was killed or its
  Arm dropped before staging results home — no exit code was ever recorded).
- `cuttle logs <id> [-f]` — print/tail the Spawn's stdout. A co-located Spawn streams live; a remote
  Arm's output arrives at stage-out, so `-f` there is post-hoc.
- `cuttle outputs <id>` — download the Spawn's `$CCC_OUT` dir as a tar.

### Accounts
- `cuttle login [--head <url>]` / `logout` / `whoami` / `passwd` — session lives in `~/.ccc/credentials`
  (0600). First login on a fresh account forces a password rotation.
- `cuttle user add|list|remove <name>` — Head-side only (runs as the Mantle against the auth store).
  `add` prints a one-time temp password; the operator never learns a reusable one.

## Dashboard
`cuttle setup --head` runs a read-only web dashboard (Vite/React) at `http://<head>:8082/`, reachable
over the LAN and the CCC mesh — live Arms + Spawn queue. Enrollment stays on a separate **mesh-only**
API (`:8081`) so the munge-key handout never touches the LAN.

## Config / artifacts
- `cuttle setup` prompts for the W&B API key + default project/entity → `~/.ccc/config/config.yaml` (0600).
- `~/.ccc` holds **only** the operator's settings + session credentials. Spawn artifacts are not here:
  they live on the Head under `/var/lib/ccc/head/spawns/<id>/{out,err,exit,outputs}` (owned by the
  Mantle) and are read back over the API — a submitter never touches the Head's disk.
- The Pod image is at `/opt/ccc/images/base.sif`, the same path on every node, so it is readable by the
  home-less Mantle.
- Pinned versions live in `src/cuttle/config.py` (mirrored in `docs/TODO.md`).
