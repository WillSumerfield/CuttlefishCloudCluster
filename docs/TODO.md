# Durable TODOs

## Before full release: revisit pinned system-package versions

MVP `cuttle setup --local` pins the system stack to exact versions (reproducibility over
freshness — see MVP plan). Bump these to current, re-test the single-node loop, and decide the
long-term update cadence (and eventually mirror/snapshot apt sources so pins can't rot).

Target: Ubuntu 24.04 (noble), amd64.

| Package | Pinned | Source |
|---|---|---|
| `slurm-wlm` | `23.11.4-1.2ubuntu5` | noble base repos |
| `apptainer` | `1.5.2-1~noble` | `ppa:apptainer/ppa` |
| `nvidia-container-toolkit` | `1.19.1-1` | NVIDIA `libnvidia-container` stable deb repo |
| base `.sif` image | `pytorch/pytorch:2.12.1-cuda13.0-cudnn9-runtime` | Docker Hub (def bootstrap) |
| `wandb` (in base `.sif`) | `wandb==0.28.0` | pip in def `%post` |

Pin-rot note: PPA + NVIDIA repos prune old versions. If a pin becomes uninstallable
(`package not found`), bump to the current published version and re-test.

## Feature: NVML-enabled Slurm (GPU autodetect / affinity / type / multi-GPU count)

**Wanted feature — deferred, not dropped.** MVP uses explicit `gres.conf` (`Name=gpu
File=/dev/nvidia0`) because Ubuntu's `slurm-wlm` is built **without** the NVML plugin
(`gpu_nvml.so` absent; they ship `gpu_generic`/`gpu_rsmi`/`gpu_nrt`). `AutoDetect=nvml` is fatal
on the stock package (`cannot create gpu context for gpu/nvml`).

NVML would give (auto): device mapping, GPU count, type labels (`--gres=gpu:<model>`), and
CPU↔GPU affinity. On the current box (single socket, single NUMA node, 1 GPU) all of these are
moot — affinity especially (only one socket). **Payoff arrives at multi-GPU and/or multi-socket.**

Recon (already done — build ingredients are all present on the box):
- `libnvidia-ml.so` present (driver); `nvml.h` present (`/usr/include`, `/usr/local/cuda-13.0/...`).
- Only the `gpu_nvml.so` plugin is missing.

Chosen route when we do it: **A2a — rebuild the `slurm-wlm` deb with `--with-nvml`** (dpkg-tracked,
ABI-safe, reproducible) rather than hand-building a loose `.so` (A2b — ABI-fragile, untracked,
drifts). Build the NVML-enabled debs **once**, commit/host them, and have `cuttle setup` install
them with `apt-mark hold` (don't compile all of Slurm on every box). Then switch `gres.conf` back
to `AutoDetect=nvml`. Note: this takes the stack off the pure apt pin (unavoidable — NVML isn't in
the stock repo), so pin our build recipe instead.

## W&B project + entity should be per-Spawn, not per-machine

MVP stores `wandb.project` in `~/.ccc/config/config.yaml` (prompted at setup). Project belongs to
the run, not the box. Move it to `cuttle run --project <name>` (config value becomes the default /
fallback). API key stays in config (it's a machine/user secret); only project moves to run-time.

Entity: same treatment — `cuttle run --entity <slug>` injecting `WANDB_ENTITY` (config default,
fallback to the account default). Currently the account has no default entity, so the validation
script `examples/train.py` hardcodes `entity="Will@ARIA"` — remove that hack once injection exists.
Note: wandb wants the entity **slug** (from the run URL `wandb.ai/<slug>/<project>`), not the
display name.

## Polish: nicer `cuttle setup` UX (install log + TUI)

MVP setup just echoes shell commands + apt spew. Make it a proper installer, oh-my-zsh-style:
- progress TUI (step list with spinners/checks; rich/textual), quiet by default
- full install log written to `~/.ccc/log/setup-<ts>.log` (verbose command output captured there,
  not dumped to the terminal)
- clear final summary (node state, versions, config path)
Applies to prompts too (see W&B key prompt) — group prompts up front, validate, then run unattended.

## Polish: `cuttle` on PATH (CLI distribution)

Today `cuttle` lives only in the project venv (`cli/core/.venv/bin/cuttle`) — users must call the
full path or activate the venv. Ship a real install: an installer/bootstrap that puts `cuttle` on
PATH (symlink into `~/.local/bin`, a `pipx`/`uv tool install`, or a one-line curl bootstrap), so
donors running `cuttle setup --arm` don't need to know the repo layout. Ties into the enrollment UX.

## Security: Arm re-enrollment must not become a permanent impersonation credential

Phase 1 wants **easy recovery**: if the Head's mesh IP ever moves (tailscaled state lost while the
Headscale DB survives, prefix change, DB migration), every Arm is stranded — its `--conf-server`
address is baked into its systemd unit and nothing refetches it. Fixing that by hand, per donor, is
untenable, so re-enrollment should be one command with no fresh token.

That convenience is the security problem. Whatever the Arm keeps on disk to re-enrol unattended is,
by definition, a credential that re-admits that machine to the cluster. Worse, an Arm already holds
the **munge key** — the cluster-wide shared secret. Anyone with it can authenticate to `slurmctld`
as any node *or any user*. Today that key sits readable on every donor box.

Needed before donors are anything less than fully trusted:
- Re-enrollment credential is **per-Arm, revocable, and distinct from the join token** (not a
  reusable preauth key sitting in `config.yaml`).
- Head can revoke a single Arm without re-keying the cluster.
- Rotate munge, or stop relying on it as the only thing between a donor and `slurmctld`.
- Re-enrollment re-derives the Head address rather than trusting the stale one.

## Security: coordinator + control-plane transport

- **Headscale is served over plain `http://<addr>:8080`.** Node keys and preauth keys cross the
  public internet in cleartext, *before* the mesh exists to protect them. Needs TLS (Let's Encrypt,
  or a pinned self-signed cert shipped in the join token) before any real donor enrols.
- **`slurmctld`/`slurmd` bind `0.0.0.0`** — reachable from the LAN and from the operator's personal
  tailnet, not just the CCC mesh. Measured: setting `SlurmctldHost=<host>(<mesh-ip>)` does *not*
  narrow the bind (slurm-wlm 23.11.4 still listens on all interfaces). So this needs a firewall rule
  restricting 6817/6818 to the mesh interface, not a config knob. Note the cost: whatever does the
  restricting makes the mesh a startup dependency of the scheduler.
- **Headscale ACLs**: Arms should reach the Head's Slurm ports and nothing else; Arm→Arm traffic
  should be denied by default. Currently the mesh is flat.
- **slurmrestd JWT** (Phase 1 ⑤): key storage/rotation on the Head.

## Before multi-node / multi-user: GPU cgroup isolation

MVP `slurm.conf` has **no** `ConstrainDevices` (single GPU, single user → nothing to isolate).
Before real sharing, add `cgroup.conf` with `ConstrainDevices=yes` so a Spawn only sees its
allotted GPU(s). Depends on host cgroup v2 + Slurm cgroup plugin.
