# Durable TODOs

## UNTESTED ON HARDWARE: remote-Arm Spawn execution (Phase-3a slice 3)

Slice 3 (pinned Mantle uid, cluster-wide `/opt/ccc/images/base.sif`, `/tmp` scratch on the Arm,
pre-created Spawn dir, ledger-aware status, `cuttle run --node`) was committed **without ever running
on a second box** — the remote Arm was unavailable. It is verified only offline: both job-script
branches render and pass `bash -n`, and the API's submit/status/outputs/attribution paths were driven
end-to-end against stub `sbatch`/`squeue`/`sinfo`.

What that leaves genuinely unproven is **only the `else` branch of `JOB_SCRIPT`** — the remote-Arm path
that rsync-stages the project in from the Head's `[project]` module and stages results back to
`[spawns]`. Everything else in the slice runs on the Head's co-located Arm and *is* testable on one box.

To verify (needs the Head + one remote Arm):
1. `cuttle teardown --local --all`, then `cuttle setup --head --address <tailnet-ip>` — the uid pin
   means `ccc` must be dropped and recreated. Re-seed `admin`, rotate the temp password.
2. `cuttle arm authorize <arm-name> --user admin` on the Head, then `cuttle setup --arm --token <blob>`
   on the donor box (creates the Mantle at the pinned uid there, builds its own base.sif).
3. `cuttle run test.py --node <arm-name> -f` → expect stage-in, a Pod run as the Mantle, stage-out,
   then `cuttle outputs <id>` yielding `result.txt` at exit 0.
4. Re-run without `--node` (co-located path must still work), and `scancel` one mid-flight (must report
   `FAILED`, not 404).

Known risk if it fails: the stage-out `rsync` writing into `rsync://<head>/spawns/<jid>/`. The dir is
now pre-created Head-side, but the daemon drops to the Mantle per-module (`uid=`/`gid=` in
`ccc-rsyncd.conf`) and that write path has never been exercised.

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

## ~~W&B project + entity should be per-Spawn, not per-machine~~ — DONE (⑦)

`cuttle run --project/--entity` inject `WANDB_PROJECT`/`WANDB_ENTITY` per-Spawn (CLI flag → config
default → project 'ccc' / entity account-default; empty entity is omitted, not passed). API key stays
in config. The `examples/train.py` `entity="Will@ARIA"` hack is gone. Reminder kept for users: wandb
wants the entity **slug** (`wandb.ai/<slug>/<project>`), not the display name.

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
- **slurmrestd JWT** (deferred from ⑤ to **Phase 3**): ⑤ reads Slurm by shelling out to
  `squeue`/`sinfo --json` (identical data model, no daemon/JWT). slurmrestd lands with job submission
  in Phase 3, where its JWT key storage/rotation gets done properly rather than as a stopgap here.

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

## Firewalls other than ufw are invisible to us

`system.allow_mesh_iface()` opens `ccc0` on both roles, but only when **ufw** is already enforcing.
A donor running firewalld, bare nftables, or an endpoint agent gets no rule and no warning. The
`GET /probe` check at the end of `setup_arm` catches the *symptom* (the Head can't reach our slurmd)
regardless of cause, which is why detecting every firewall was judged not worth it. Revisit if a
real donor ever trips it.

Not in scope: the coordinator's own rule (`allow in on tailscale0 to any port 8080`) — that depends
on how the coordinator gets exposed, which is the reachability item above.

## `cuttle teardown --arm` does not exist

`teardown_local` is the only teardown. A donor who wants to leave has no supported path: stop and
disable slurmd, drop `/etc/default/slurmd`, leave the mesh (`tailscale down` + wipe CCC state),
remove the `ccc0` ufw rule, and undo the WSL Apptainer config if it was applied. The Head half
(`cuttle arm remove`) already exists and is what evicts the node from the mesh and Slurm — an Arm
tearing itself down cannot do that for itself, so the two commands are complements, not substitutes.

## `preflight()`'s GPU check passes on WSL for the wrong reason

`preflight()` runs `nvidia-smi`. On WSL that binary lives in `/usr/lib/wsl/lib`, which is **not** on
`PATH` except in an interactive login shell (measured: `command not found` inside an `srun` job).
So the check passes during `cuttle setup --arm` only because a human is running it from a login
shell. It would fail from any non-interactive context, and it isn't really verifying the GPU.
Fix: fall back to `/usr/lib/wsl/lib/nvidia-smi` when `is_wsl()` and the binary isn't on `PATH`.

## Arm re-enrollment reads the coordinator URL out of a `debug` command

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

## Phase 4: Sweep staging reuses one dir across trials

④ (Phase 1) stages per **Spawn = per Slurm job**: one ephemeral node-local dir, wiped when the job
exits. Deliberately no persistent per-project cache — leaving multi-GB working dirs on donor boxes is
bad tenancy, so each Spawn copies fresh. That re-transfers the project on every Spawn; accepted, the
priority is not hogging donor disk.

A **Sweep** (Phase 4) is multiple trials, each its own Slurm job under Slurm dependencies. Staging
per-job would re-transfer the dataset once *per trial* — the hogging-vs-re-transfer tension, back at
sweep granularity. Phase 4 should stage the project **once per Sweep** and reuse it across trials
(stage inside a single allocation that fans out trials, or a sweep-keyed dir with a "sweep done →
wipe" signal), so a sweep neither re-transfers nor leaves persistent state behind.

## Live log streaming for remote Spawns

④ stages `.out`/`.err`/`.exit` home **at job end** (Option 1). So `cuttle logs -f` on a *remote*
Spawn shows nothing until the job finishes — a 6-hour run is blind until it exits. `cuttle status`
still works live (that reads `squeue` + the staged `.exit`, not log contents); only live scrollback
is missing. Local Spawns (Head's co-located Arm) already stream, since the file is written in place.

Fix later: have the job push `.out`/`.err` to the Head periodically (background rsync loop, every few
seconds) so `logs -f` tracks a remote run live. Deferred for the machinery — a background pusher
racing the main process, with its own stall/failure modes — which Phase 1 doesn't need.

## setup_arm interleaves apt install with Head enrollment (CLI-cleanup item)

`setup_arm` order is: mesh-admission gate (`up_arm` blocks on approval) → **apt install slurmd +
apptainer + build base.sif** → fetch munge key → slurm-admission gate (`wait_for_admit`) → start
slurmd. The local install work has no dependency on either Head gate, yet sits between them, so a
package install runs mid-conversation with the Head. Cleaner: do all local heavy-lifting (install,
base.sif) as one block either fully before or fully after the enroll/approval exchange. Fold into the
planned full CLI cleanup, not a standalone fix.

## Remote scratch orphans on SIGKILL

④ wipes a remote Spawn's node-local scratch (`/tmp/ccc-<jobid>`) with a bash `trap ... EXIT`. That
fires on normal exit and ordinary signals, but **not** SIGKILL — OOM-killer, `scancel -9`, and (Phase
2) a Retract all bypass it, orphaning the scratch dir on the donor. Fix with a Slurm epilog that
`rm -rf`s the job's scratch, or a boot-time sweep of `/tmp/ccc-*`. Pairs naturally with Phase-2 Retract
(which needs an epilog anyway). **Severity dropped (slice 3):** scratch moved from the (home-less)
Mantle's home to `/tmp`, so an orphan is now swept by the OS at reboot rather than accumulating
forever. Until the epilog lands, a hard-killed Spawn still hogs donor disk until the next reboot.

## WSL2 Arms: supported, with one permanent limit

`preflight()` now detects WSL (`/proc/sys/kernel/osrelease` contains `microsoft`) and allows it as an
Arm, never as a Head — headscale and slurmctld have never run there. `setup_arm` adds two lines to
Apptainer's config on WSL boxes (`bind path = /usr/lib/wsl`, `libdxcore.so` in `nvliblist.conf`),
after which plain `apptainer exec --nv` behaves as on native Linux. Verified: `torch.cuda` sees the
RTX 4090 and a matmul runs. The platform quirk stops at the Arm; no job-layer flags.

Permanent limit, not a TODO: `/dev/dxg` is a single device node for **all** GPUs, so device cgroups
can never partition GPUs per-Spawn on WSL. See the cgroup item below — a WSL Arm is single-GPU-
exclusive forever, or excluded from multi-user sharing.

An `apt` upgrade of apptainer will raise a dpkg conffile prompt on both edited files.

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
