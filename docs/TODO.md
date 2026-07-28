# Durable TODOs

## Dashboard Spawns silently disable W&B → Phase 5 — [effort: med/high]

`head/web/src/api.ts` hardcodes `wandb_env: {WANDB_MODE: 'disabled'}` on every browser submission,
because the browser has no W&B API key to send. Meanwhile `cuttle run --project/--entity` works fine.

The Dashboard is supposed to be the **primary** submission surface, and it is strictly worse than the
secondary one at the thing the cluster exists for. Worse, it's *silent* — the Spawn runs, and the
metrics simply never appear.

Fix (decided in the Ph5 grill): a **per-User W&B key** held by the Head (Account page → paste once), and
injected into every Spawn that User submits, from *either* client. Consequence to handle deliberately:
the Head now stores a third-party secret. It must be **encrypted at rest** — unlike a password it has to
be recoverable in plaintext to be used, so it cannot just be argon2'd like `pw_hash`.

## Pinned system-package versions: still need a long-term update cadence — [effort: low]

MVP `cuttle setup --local` pins the system stack to exact versions (reproducibility over
freshness — see MVP plan). Bumped to current 2026-07-27 and re-tested the single-node loop
(`cuttle run` → GPU matmul + wandb import inside the rebuilt `base.sif`, exit 0).

`slurm-wlm`, `nvidia-container-toolkit`, and `apptainer` were already at the newest version their
configured repos serve — nothing to bump there right now. Only the base image and `wandb` had
newer releases: `pytorch/pytorch:2.12.1-cuda13.0-cudnn9-runtime` → `2.13.0-cuda13.0-cudnn9-runtime`
(same CUDA major, matches the installed driver/toolkit), `wandb==0.28.0` → `0.28.1`.

Target: Ubuntu 24.04 (noble), amd64.

Still open: decide the long-term update cadence, and eventually mirror/snapshot apt sources so
pins can't rot. Pin-rot note unchanged: PPA + NVIDIA repos prune old versions, so an uninstallable
pin (`package not found`) means bump to current and re-test.

## Feature: NVML-enabled Slurm (GPU autodetect / affinity / type / multi-GPU count) — [effort: high]

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

## Feature: Automatic GPU partitioning (fractional sharing — MIG / MPS / time-slicing) — [effort: high]

**Wanted feature — deferred, not dropped** (added 2026-07-26). Today a Spawn holds a *whole* card
(`--gres=gpu:N`); a job that needs 2 GB still parks on a 24 GB GPU alone. Automatic partitioning would
carve one physical GPU into slices so several Spawns share a card, and the scheduler would place them
without the operator hand-partitioning anything:
- **MIG** (NVIDIA A100/H100+): hardware-isolated instances, real memory/SM partitioning. Cleanest, but
  hardware-gated (the current fleet's cards may not support it) and slices are coarse, pre-declared sizes.
- **MPS / time-slicing**: works on any card, but no hard memory isolation — one Spawn can OOM another
  sharing the GPU. Needs Karma/pricing to understand a fractional hold, and an isolation story.

**Depends on whole-GPU isolation landing first** (the `ConstrainDevices` item below) — you cannot
safely subdivide a card the scheduler cannot even constrain a job to. **That prerequisite is written
(2026-07-28) and awaiting hardware verification**; partitioning itself stays unscheduled until it is
verified and real multi-GPU hardware exists to design against. **Permanent WSL
exception:** `/dev/dxg` is one device node for all GPUs, so per-GPU partitioning can never work there
(see that item). Also touches Karma (a partition is a fractional Power/Demand unit, not a whole card) and `cuttle run`
(how a user asks for a slice). Decide scope when GPU sharing becomes real.

## Security: Arm re-enrollment must not become a permanent impersonation credential → Phase 9 — [effort: high]

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

## Security: `/enroll` gives the munge key to any mesh member (accepted for Phase 1) → Phase 9 — [effort: high]

Enrollment serves `/etc/munge/munge.key` over a Head API bound to the mesh address only, so the sole
authorization is *being on the mesh*. Deliberate, and accepted while every Arm is a machine we own.

Consequence, stated plainly: **mesh compromise == Slurm compromise.** There is no second factor. A
stale Arm enrolled months ago can re-fetch the key; so can any machine that ever spent a preauth
key. This doesn't widen exposure (an enrolled Arm holds the key on disk regardless) but it doesn't
narrow it either — and munge is cluster-wide and root-equivalent: whoever holds it authenticates to
`slurmctld` as any node or any user.

Two things changed with the Phase-3a slice-5 enrollment reversal, and neither closes this:
- Mesh admission **is now a secret** (the enrollment token carries a preauth key), where it used to be
  a pure operator decision with nothing to leak. The token is single-use and lives an hour, and it
  crosses the LAN to reach the donor. Acceptable only because CCC is LAN-scoped (#8 deferred) and
  donors are never strangers.
- Mesh membership alone **no longer fetches the munge key**: `/enroll` refuses a node name the Admin
  never pre-declared. So an unauthorized machine that somehow reaches the mesh gets nothing.

What still stands is the core of it: an **authorized** Arm fetches the key and holds it on disk
forever, and munge is cluster-wide and root-equivalent — whoever holds it authenticates to `slurmctld`
as any node or any user. Closing *that* is the per-Arm revocable-credential item above. Until then,
treat authorizing an Arm as full cluster trust: run `cuttle arm authorize` for a machine only if you'd
hand its owner the cluster's root credential, because you are.

## Blocker: the coordinator must be reachable before an Arm is on the mesh → Phase 6 — [effort: med/high]

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

## Security: coordinator + control-plane transport → Phase 9 — [effort: high]

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
- ~~slurmrestd JWT~~ — **abandoned (2026-07-15)**, not just deferred. The plan was always to defer it
  from ⑤ to Phase 3 for job submission's sake, but Phase 3 shipped entirely on the `squeue`/`sinfo
  --json` shell-out (identical data model, no daemon/JWT) and submission goes straight to `sbatch`
  over the munge socket — slurmrestd never filled a gap either path left open. Adding it now would
  mean a fourth daemon plus JWT key storage/rotation to log, health-check, and back up, for no
  capability this cluster's scale needs. Struck as a stale plan, not a live TODO.

## Two tailscaled daemons both claim MagicDNS's `100.100.100.100` — [effort: low — accept/watch, no known fix]

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

## Firewalls other than ufw are invisible to us — [effort: low — deferred until a donor trips it]

`system.allow_mesh_iface()` opens `ccc0` on both roles, but only when **ufw** is already enforcing.
A donor running firewalld, bare nftables, or an endpoint agent gets no rule and no warning. The
`GET /probe` check at the end of `setup_arm` catches the *symptom* (the Head can't reach our slurmd)
regardless of cause, which is why detecting every firewall was judged not worth it. Revisit if a
real donor ever trips it.

Not in scope: the coordinator's own rule (`allow in on tailscale0 to any port 8080`) — that depends
on how the coordinator gets exposed, which is the reachability item above.

## Remote-Arm sbatch wrapper litters `/tmp/ccc-<jobid>.slurm` on the node — [effort: low]

Leftover from ④'s stage-in/out fix: the sbatch wrapper's own stdout/stderr (rsync diagnostics, not
the Spawn's) write to `/tmp/ccc-<jobid>.slurm` on whichever node ran it, and nothing reaps them.
Not blocking — just unbounded litter on donor boxes over time.

## Phase 4: Sweep staging reuses one dir across trials — [effort: med]

④ (Phase 1) stages per **Spawn = per Slurm job**: one ephemeral node-local dir, wiped when the job
exits. Deliberately no persistent per-project cache — leaving multi-GB working dirs on donor boxes is
bad tenancy, so each Spawn copies fresh. That re-transfers the project on every Spawn; accepted, the
priority is not hogging donor disk.

A **Sweep** (Phase 4) is multiple trials, each its own Slurm job under Slurm dependencies. Staging
per-job would re-transfer the dataset once *per trial* — the hogging-vs-re-transfer tension, back at
sweep granularity. Phase 4 should stage the project **once per Sweep** and reuse it across trials
(stage inside a single allocation that fans out trials, or a sweep-keyed dir with a "sweep done →
wipe" signal), so a sweep neither re-transfers nor leaves persistent state behind.

## Live log streaming for remote Spawns → Phase 5 — [effort: med]

④ stages `.out`/`.err`/`.exit` home **at job end** (Option 1). So `cuttle logs -f` on a *remote*
Spawn shows nothing until the job finishes — a 6-hour run is blind until it exits. `cuttle status`
still works live (that reads `squeue` + the staged `.exit`, not log contents); only live scrollback
is missing. Local Spawns (Head's co-located Arm) already stream, since the file is written in place.

Fix later: have the job push `.out`/`.err` to the Head periodically (background rsync loop, every few
seconds) so `logs -f` tracks a remote run live. Deferred for the machinery — a background pusher
racing the main process, with its own stall/failure modes — which Phase 1 doesn't need.

## GPU cgroup isolation — WRITTEN, awaiting hardware verification — [effort: med]

**The bug this fixes, measured on the first remote job:** `srun -w will-personal --gres=gpu:1`
succeeded, but `SLURM_JOB_GPUS` came back **empty**. Slurm counted the GRES for scheduling and never
bound a device or told the step which GPU it held. GPU allocation was therefore *advisory* — a job
could touch a GPU it wasn't given. Harmless at one GPU per node; disqualifying the moment two Spawns
share a box, and the hard prerequisite for GPU partitioning (above).

**Built (2026-07-28):** `ProctrackType=proctrack/cgroup` + `TaskPlugin=task/cgroup` in `slurm.conf`,
plus a new `cgroup.conf` (`CgroupPlugin=autodetect`, `ConstrainDevices=yes`) authored by the Head and
served to every Arm over configless. Scope decisions, all deliberate:
- **Native Linux Arms only.** A WSL Arm can never be GPU-isolated — its GPU is `/dev/dxg`, one device
  node for *all* cards, which device cgroups cannot partition even under v2. Permanent, not deferred.
- **Cluster-wide config.** Configless serves one `cgroup.conf` to every node; no per-node override
  exists. The WSL Arm reports cgroup **v1** (`stat -fc %T /sys/fs/cgroup` → `tmpfs`) and what slurmd
  does with that is **unverified** — ship it, re-run `setup` on both boxes, fix whatever really breaks.
  No pre-built fallback or exemption ahead of that test.
- **`ConstrainDevices` only** — no `ConstrainRAMSpace`/`ConstrainCores`; nothing sets a RAM or core
  budget (a Spawn declares `--time`/`--vram`), so a limit would be a number nobody asked for.
- **No preflight cgroup-version check** — `setup.py`'s WSL/`dxg` warning stays as-is; a symmetric
  cgroup warning waits until the hardware test shows it's needed.

**Remaining, and the only thing left here:** re-run `cuttle setup --head` and `cuttle setup --arm`
(both restart their daemons — a plugin change needs a restart, `scontrol reconfigure` won't do it),
then confirm a remote Spawn reports a populated `SLURM_JOB_GPUS` and a cgroup device list matching its
`gres` allocation. Cross-job isolation on a *shared* card stays unverifiable until a multi-GPU box
exists (today's fleet is one GPU per node) — a documented gap, not a blocker.

## Low Karma long jobs get cancelled / Spawn Code Format Solidifcation — [effort: high]
There should be some disincentive for queuing long jobs when you have very low karma. Jobs should probably be cancelled. 
This ties into a large project which should enforce that spawn codebases support several features which help save progress & build properly. 
Maybe W&B helps with this? Unsure.
