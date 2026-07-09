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

## Before multi-node / multi-user: GPU cgroup isolation

MVP `slurm.conf` has **no** `ConstrainDevices` (single GPU, single user → nothing to isolate).
Before real sharing, add `cgroup.conf` with `ConstrainDevices=yes` so a Spawn only sees its
allotted GPU(s). Depends on host cgroup v2 + Slurm cgroup plugin.
