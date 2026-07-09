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
- Re-enrollment credential is **per-Arm and revocable**. Note the Arm's `tailscaled` state already
  *is* such a credential (its node key re-authenticates it to the mesh unattended) — so revocation
  means expiring that node in Headscale, not just deleting a file.
- Head can revoke a single Arm without re-keying the cluster.
- Rotate munge, or stop relying on it as the only thing between a donor and `slurmctld`.
- Re-enrollment re-derives the Head address rather than trusting the stale one.

## Security: `/enroll` gives the munge key to any mesh member (accepted for Phase 1)

Enrollment serves `/etc/munge/munge.key` over a Head API bound to the mesh address only, so the sole
authorization is *being on the mesh*. Deliberate, and accepted while every Arm is a machine we own.

Consequence, stated plainly: **mesh compromise == Slurm compromise.** There is no second factor. A
stale Arm enrolled months ago can re-fetch the key; so can any machine that ever spent a preauth
key. This doesn't widen exposure (an enrolled Arm holds the key on disk regardless) but it doesn't
narrow it either — and munge is cluster-wide and root-equivalent: whoever holds it authenticates to
`slurmctld` as any node or any user.

Mesh admission is an operator decision (`cuttle arm approve`), not a secret — so nothing *leaks* a
way in. But once in, a machine may fetch the munge key, and that says nothing about what it may then
take from the **cluster**. Closing the gap is the per-Arm revocable-credential item above. Until
then, treat mesh membership as full cluster trust: approve a machine only if you'd hand its owner
the cluster's root credential, because you are.

## Blocker: the coordinator must be reachable before an Arm is on the mesh

An Arm reaches Headscale to *get* a mesh address, so the coordinator address can never be a mesh
address. Today the Head owns exactly two others: its LAN IP (useless to Arms off that LAN) and its
personal-tailnet IP (useless unless the Arm is already on the operator's personal tailnet).

Measured while enrolling the WSL Arm: no LAN path (different networks), and no route to
`100.114.172.14` until that box joined the personal tailnet. We joined it — a **test-only shortcut**.
It makes personal-tailnet membership a prerequisite for becoming an Arm, which is exactly the grant
the CCC mesh exists to avoid asking a donor for. So remote enrollment is *not* proven end-to-end:
the mesh join, approval, `/enroll`, munge fetch, and `slurmd -Z` are all exercised faithfully; only
the first hop — how a donor finds the coordinator — is faked.

Real options: port-forward + DDNS (needs router access; makes the TLS item below a hard blocker,
since node keys would cross the internet in cleartext), or a tunnel (Cloudflare Tunnel / `tailscale
funnel`) for public HTTPS with no port-forward. A funnel keeps the personal tailnet load-bearing for
coordinator *availability*, though not for Arm membership. Decide before any donor enrols.

## Security: coordinator + control-plane transport

- **Headscale is served over plain `http://<addr>:8080`.** Node keys and preauth keys cross the
  public internet in cleartext, *before* the mesh exists to protect them. Needs TLS (Let's Encrypt,
  or a pinned self-signed cert shipped in the join token) before any real donor enrols.
- **`slurmctld`/`slurmd` bind `0.0.0.0`.** Measured: setting `SlurmctldHost=<host>(<mesh-ip>)` does
  *not* narrow the bind (slurm-wlm 23.11.4 still listens on all interfaces). Correction to an
  earlier claim here: the ports are not actually LAN-reachable, because the Head runs `ufw` with
  `default deny incoming`. The bind is wide; the reachable surface is not. That's luck, not design —
  nothing in `cuttle setup` puts that firewall there or checks it (see the ufw item below).
- **Headscale ACLs**: Arms should reach the Head's Slurm ports and nothing else; Arm→Arm traffic
  should be denied by default. Currently the mesh is flat.
- **slurmrestd JWT** (Phase 1 ⑤): key storage/rotation on the Head.

## Two tailscaled daemons both claim MagicDNS's `100.100.100.100`

`tailscaled-ccc` installs `100.100.100.100 dev ccc0` (route table 52) even with
`--accept-dns=false` and Headscale `magic_dns: false`. The personal daemon wants that route too, for
the personal tailnet's MagicDNS. **Last daemon to start wins** — so this flips across reboots.

Measured, and it corrects an earlier theory: this is *not* caused by the mesh prefix containing the
address. We moved the mesh from `100.100.0.0/16` to `100.99.0.0/16` and the route persisted.
tailscaled routes quad100 into its tun unconditionally.

Harmless today: nothing on the Head queries `100.100.100.100` (CCC never touches systemd-resolved,
and everything addresses machines by IP). It bites the day MagicDNS is used on the personal tailnet
from a CCC box, as an intermittent, reboot-dependent DNS failure that looks nothing like CCC.

No known fix from our side — tailscaled has no flag to suppress the quad100 route. Options if it
ever matters: a routing rule pinning quad100 to the personal tun, or accepting that a CCC box can't
use personal MagicDNS.

## `cuttle setup` should own the `ccc0` firewall rule

The Head's `ufw` drops inbound on `ccc0`, so an Arm can join the mesh, get an address, ping nothing,
and fail `/enroll` with a bare timeout. Cost us an hour. The mesh interface must be open for the
cluster to work at all:

    sudo ufw allow in on ccc0 comment 'ccc mesh (slurm + head api)'

Interface-scoped, not port-scoped: `srun` allocates ephemeral ports for task I/O, and multi-node
jobs need Arm↔Arm traffic. This grants the mesh nothing it didn't already have — `docs/TODO.md`
already states mesh membership means full cluster trust.

Make `setup_head` add it **only if ufw is already active**. Enabling a default-deny firewall on a
donor's box is exactly the host-global mutation the tailscaled isolation exists to avoid. If ufw is
inactive, do nothing and say so.

Not in scope: the coordinator's own rule (`allow in on tailscale0 to any port 8080`) — that depends
on how the coordinator gets exposed, which is the reachability item above.

## Arm re-enrollment needs a state wipe

`setup_arm` assumes a virgin `tailscaled-ccc`. If the Head ever deleted the Arm's node (`cuttle arm
remove`, a prefix change, a rebuilt Headscale DB), the Arm's state file still holds a node key
Headscale no longer knows; `tailscale up` then blocks forever with no auth URL and no error. The
Head already handles the mirror-image case in `mesh.forget_state()`.

Give the Arm the same: detect a rejected/unknown node key (or just always `--reset` from a wiped
state on an explicit `cuttle setup --arm --re-enroll`) instead of requiring a hand-run
`rm /var/lib/tailscale-ccc/tailscaled.state`. Ties into the re-enrollment credential item above.

## `preflight()`'s native-Linux assert doesn't detect WSL

`assert system.is_native_linux_x86()` passed on a WSL2 Ubuntu 24.04 Arm, which is precisely the
platform it exists to reject. The guard is decorative today. Either make it real (check
`/proc/sys/kernel/osrelease` for `microsoft`, or `/run/WSL`) and gate WSL behind an explicit flag,
or delete it and admit WSL is supported. Right now it neither rejects nor blesses.

## Before multi-node / multi-user: GPU cgroup isolation

MVP `slurm.conf` has **no** `ConstrainDevices` (single GPU, single user → nothing to isolate).
Before real sharing, add `cgroup.conf` with `ConstrainDevices=yes` so a Spawn only sees its
allotted GPU(s). Depends on host cgroup v2 + Slurm cgroup plugin.

**Measured on the first remote job:** `srun -w will-personal --gres=gpu:1` succeeded, but
`SLURM_JOB_GPUS` came back **empty**. Slurm counts the GRES for scheduling and never binds a device
or tells the step which GPU it holds. GPU allocation is therefore *advisory* — a job can touch a GPU
it wasn't given. Harmless at one GPU per node; disqualifying the moment two Spawns share a box.

**Measured, and awkward:** the WSL2 Arm reports cgroup **v1** (`stat -fc %T /sys/fs/cgroup` → `tmpfs`).
So `ConstrainDevices` cannot work there at all, and its GPU is `/dev/dxg` — a single device node for
*all* GPUs, which device cgroups cannot partition per-GPU even under v2. A WSL Arm therefore can
never isolate GPUs. Either WSL Arms stay single-GPU-exclusive forever, or they're excluded from
shared-GPU scheduling. Decide when GPU sharing becomes real.
