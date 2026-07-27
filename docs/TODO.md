# Durable TODOs

## Untriaged — launch-readiness grill (2026-07-15)

Found by exploring the actual `head/api` + `head/web` code against `temp/ROADMAP.md`, `docs/TODO.md`,
and `CONTEXT.md` — each item below was confirmed absent from all three (not deferred, not resolved,
just never written down). Direction below is a first pass, not a locked design; each item gets its own
grill before landing in `temp/ROADMAP.md`'s new **Phase 7 — Loose Ends**.

- **[high] No lighter redeploy path than full `teardown`+`setup`.** Several hardware slices (Mantle uid pin,
  the ccc-user rename) needed a full reprovision to ship a code change. Direction from the grill:
  containerize the three Head-side app services (**Head API, Enroll API, karma tick**) so an update is
  an image pull + restart; explicitly **leave Slurm/Headscale/munge bare-metal** (GPU/cgroup/mesh-bind
  needs would fight containerizing them, and the roadmap already defaults Slurm to bare-metal). →
  Phase 7. Supersedes/absorbs the "Polish: `cuttle` on PATH" item below where they overlap (the
  installer question is about the *client* CLI, not the Head services — keep both).
- **[med] No rate-limiting anywhere** (confirmed by grep). Decided direction: throttle `/login` (closes a
  real brute-force hole even among trusted Users — a guessed weak temp password, a compromised laptop)
  and add a **per-User max-concurrent-Spawn cap** (Escrow prices flooding the queue but doesn't hard-cap
  it). → Phase 7.
- **[high] Nothing monitors the Head box itself.** Phase 2's telemetry item watches Arms (rogue GPU use);
  Phase 7(→8)'s completeness pass only *audits* unbounded `head/projects`/`head/spawns` growth once.
  Wanted: disk-space + process-liveness checks, a GPU/CPU usage-over-time monitor with anomaly
  reporting, an explicit pass hunting memory leaks / unbounded disk growth (not just an audit — ongoing
  monitoring), and email alerting for all of the above. → Phase 7.


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

## Before full release: revisit pinned system-package versions — [effort: med]

MVP `cuttle setup --local` pins the system stack to exact versions (reproducibility over
freshness — see MVP plan). Bump these to current, re-test the single-node loop, and decide the
long-term update cadence (and eventually mirror/snapshot apt sources so pins can't rot).

Target: Ubuntu 24.04 (noble), amd64.

| Package | Pinned | Source |
|---|---|---|
| `slurm-wlm` | `23.11.4-1.2ubuntu5` | noble base repos |
| `apptainer` | _unpinned (PPA latest)_ | `ppa:apptainer/ppa` — keeps only its newest build, so an exact pin breaks on every release |
| `nvidia-container-toolkit` | `1.19.1-1` | NVIDIA `libnvidia-container` stable deb repo |
| base `.sif` image | `pytorch/pytorch:2.12.1-cuda13.0-cudnn9-runtime` | Docker Hub (def bootstrap) |
| `wandb` (in base `.sif`) | `wandb==0.28.0` | pip in def `%post` |

Pin-rot note: PPA + NVIDIA repos prune old versions. If a pin becomes uninstallable
(`package not found`), bump to the current published version and re-test.

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

**Depends on whole-GPU isolation landing first** (the `ConstrainDevices` item below, → Phase 9): you
cannot safely subdivide a card the scheduler cannot even constrain a job to. **Permanent WSL exception:**
`/dev/dxg` is one device node for all GPUs, so per-GPU partitioning can never work there (see that item).
Also touches Karma (a partition is a fractional Power/Demand unit, not a whole card) and `cuttle run`
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

## Arm re-enrollment reads the coordinator URL out of a `debug` command → Phase 2 — [effort: med]

`mesh.joined_to()` decides whether to wipe an Arm's mesh identity by comparing the daemon's
`ControlURL` against the token's coordinator. That field is exposed only by `tailscale debug prefs`
— `status --json` has no `ControlURL` (checked on 1.98.8). `debug` is explicitly an unstable
interface, so a Tailscale upgrade may change or drop it.

**This got sharper with slice 5.** It used to be that a mis-reported `False` merely wiped the identity
and asked for a fresh approval — annoying, not dangerous. Now the enrollment token's preauth key is
**single-use**, so a spurious wipe on a *re-run* of `setup --arm` throws away a working identity and
then redeems an already-spent key: `tailscale up` fails outright, and the donor needs the Admin to mint
a whole new token. Re-running setup is meant to be idempotent, so this turns an unstable-interface
regression into a hard stop. The safe fallback if that day comes is to parse the state file directly.

## `cuttle run` on a remote Arm — RESOLVED by ④ (stage-in/out)

Was: `spawns.py` rendered one sbatch script whose paths were the *submitter's*, but the script runs
on whichever node Slurm picks — `--output`/`--error`/`.exit`/`python <script>` all resolved to Head
paths absent on an Arm (`slurmstepd: couldn't chdir`).

Now: the job branches on `[ -d "$staging_dir" ]` — "are the project files already on this node?" The
Head copies the upload into node-local scratch (never running in the served [project] tree) and writes
results straight to the Spawn dir. A remote Arm stages the project IN from the Head's mesh-bound rsync
daemon into `/tmp` scratch, runs, and stages out/err/exit/outputs BACK to the daemon's [spawns] module.
Both produce the same Head-side layout `/var/lib/ccc/head/spawns/<id>/{out,err,exit,outputs}` (owned by
the Mantle; read back over the API). Results are collected from `$CCC_OUT` (injected into the Pod) —
narrows the roadmap's "`$CCC_OUT` / `./outputs`" to just `$CCC_OUT`, so per-Spawn isolation (layout A)
holds and nothing writes into the source tree.

Leftover litter, not blocking: the sbatch wrapper's own stdout/stderr go to `/tmp/ccc-<jobid>.slurm`
on the node (rsync diagnostics) and are never reaped.

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

## setup_arm interleaves apt install with Head enrollment → Phase 7, merged into "CLI install/setup UX" — [effort: low]

`setup_arm` order is: mesh-admission gate (`up_arm` blocks on approval) → **apt install slurmd +
apptainer + build base.sif** → fetch munge key → slurm-admission gate (`wait_for_admit`) → start
slurmd. The local install work has no dependency on either Head gate, yet sits between them, so a
package install runs mid-conversation with the Head. Cleaner: do all local heavy-lifting (install,
base.sif) as one block either fully before or fully after the enroll/approval exchange. Merged
2026-07-15 into the "CLI install/setup UX needs a real pass" item at the top of this file — same
cleanup pass, not a standalone fix.

## WSL2 Arms: supported, with one permanent limit — [status — no task; permanent limit]

`preflight()` now detects WSL (`/proc/sys/kernel/osrelease` contains `microsoft`) and allows it as an
Arm, never as a Head — headscale and slurmctld have never run there. `setup_arm` adds two lines to
Apptainer's config on WSL boxes (`bind path = /usr/lib/wsl`, `libdxcore.so` in `nvliblist.conf`),
after which plain `apptainer exec --nv` behaves as on native Linux. Verified: `torch.cuda` sees the
RTX 4090 and a matmul runs. The platform quirk stops at the Arm; no job-layer flags.

Permanent limit, not a TODO: `/dev/dxg` is a single device node for **all** GPUs, so device cgroups
can never partition GPUs per-Spawn on WSL. See the cgroup item below — a WSL Arm is single-GPU-
exclusive forever, or excluded from multi-user sharing.

An `apt` upgrade of apptainer will raise a dpkg conffile prompt on both edited files.

## Before multi-node / multi-user: GPU cgroup isolation → Phase 9 — [effort: med]

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

## Low Karma long jobs get cancelled / Spawn Code Format Solidifcation — [effort: high]
There should be some disincentive for queuing long jobs when you have very low karma. Jobs should probably be cancelled. 
This ties into a large project which should enforce that spawn codebases support several features which help save progress & build properly. 
Maybe W&B helps with this? Unsure.
