# Durable TODOs

Designed but not built. Everything in [`Architecture.md`](Architecture.md) exists; everything here
does not.

## Volatile Arms and preemption — [effort: medium]

**Every enrolled Arm is a permanent one today.** The `--volatile` flag and Slurm preemption
(`PreemptMode=REQUEUE`/`SUSPEND`) are design only, waiting on the hardening phase.

The gap matters less than it looks: **Retract/Return is built and applies to every Arm**, so an owner
can already reclaim their machine instantly without the volatility flag existing. What volatility
would add is the *scheduler* knowing a machine is reclaimable — placing shorter work there, and
preempting rather than killing.

## Public reachability — [effort: medium]

The Head is assumed reachable over the LAN or the CCC mesh. There is no public route.

Two things are missing and they are separable: a **route** (a tunnel, or port-forward + DDNS) and a
**real certificate**. Today the Head signs its own and clients pin it on first use, which is
sufficient on a LAN and not sufficient on the internet.

The current workaround for remote donors — advertising the Head's personal-tailnet address — requires
the donor's box to join that tailnet, which is exactly the grant the CCC mesh exists to avoid asking
for. It is fine for machines the operator owns. It is not a general answer.

Nothing else is hardened for an internet-facing deployment either; see
[`Security.md`](Security.md#what-is-not-defended).

## Fleet-wide telemetry — [effort: medium]

Rogue-actor detection is scoped to the **Head's own box**. Watching a donor's machine the way the
Head watches itself is a separate, deliberate call that has not been made — an Arm is a machine CCC
does not own, and building the monitor generic now would decide that by accident.

Also unbuilt: per-Arm Prometheus/Grafana, per-user alerting, and W&B-triggered NaN/freeze pings.

## Staging: dedup and live logs — [effort: low]

Every Spawn stages a fresh copy of its project; there is no persistent cache. A parameter sweep
re-uploads the same tree N times. Open: dedup/caching for sweeps, and live log streaming for remote
Arms.

## Feature: Automatic GPU partitioning — the Beak — [effort: high]

**Not built — blocked on hardware, not on decisions.** Today a Spawn holds a
*whole* card; a job needing 2 GB parks on a 24 GB GPU alone. The **Beak** carves a card into
**Slice**s so several Spawns hold it at once. Glossary terms (Device, Slice, Carvable, Beak) are in
`CONTEXT.md`; this section is the implementation design.

**MIG only.** A card is sliceable or whole — "shared" always means hardware-isolated, never an honour
system. MPS/time-slicing is rejected: no memory isolation, so one Spawn can OOM another. Consequence:
the RTX 5080 and every WSL Arm (`/dev/dxg` is one node for all GPUs) are permanently whole-card. The
Blackwell 6000 Pros live on the **Head**, so all carvable hardware is Commons — Karma spent on a Slice
is destroyed, not transferred.

**The Head holds Spawns; `gres.conf` never lies.** Probed on Slurm 23.11.4 (disposable
slurmctld, live cluster untouched): a GRES type declared with `Count=0` is rejected at submit
*identically* to an undeclared type — Slurm validates against configured **counts**, not type names.
Declaring maximum counts instead is worse: slurmctld would believe `gpu:big:1,gpu:small:4` are
simultaneously available and double-allocate the same silicon. **Slurm cannot represent
mutually-exclusive GRES.** So `gres.conf` always states the exact current carve (real device files →
`ConstrainDevices` keeps enforcing, counts genuinely add up), and CCC holds Spawns whose shape does not
exist yet in the Head DB, showing one merged queue ordered by the same Karma Share. The Head **rejects**
what the fleet can never do (`slurm.py:116`, already checks full card VRAM, survives slicing untouched)
and **holds** only what it cannot do yet.

**Waiting is the Beak's job, not Slurm's.** Slurm cannot reserve a shape that does not exist, so the
Beak computes earliest-feasible-start itself from running Slices' declared ends. It carves for a
lower-priority Spawn only if that Spawn's declared end falls before the top pending Spawn's earliest
start — otherwise it holds the space **deliberately idle**. (16 GB tenant ending t=100, 96 GB Spawn
waiting: a 30-min 64 GB Spawn is carved for; a 200-min one is not.) This makes `--time` load-bearing
for placement, not just pricing.

**Rules settled by scenario:**
- Re-carve is **withdraw-then-destroy**: rewrite `gres.conf` and reconfigure, confirm nothing holds it,
  *then* touch the hardware. Slurm can never allocate what it cannot see.
- An **idle** Slice has no claim — destroy it for a higher-Karma Spawn. Only a *held* Slice is immutable.
- A **settle window** before acting, so submit/cancel churn costs no MIG resets.
- Never hand out a Slice larger than requested; reshape instead.
- Greedy down the priority queue, subject to the waiting rule above.

**Pricing.** Power is **measured per profile**, never derived — a quarter card is not a quarter of a
card's Power. Measured once at `setup --head`, while the cluster provably has no work. Demand attaches
to the **physical card** and is counted in whole-card-hours, so carving can never move a price. Escrow
opens when the Head *accepts* a Spawn, held or not, or queueing for the scarcest hardware becomes the
one free action in the economy.

**Open, needs the real hardware:**
- Read the actual profile table (`nvidia-smi mig -lgip`); everything above assumes coarse pre-declared
  sizes and *positional* placement rules (a contiguity/alignment constraint, so free capacity can exist
  in a shape nothing fits — the Beak is a bin-packer, not a subtraction).
- Confirm `scontrol reconfigure` can change GRES record counts for *idle* records without renumbering
  and mis-pointing a record a running Spawn holds (`gres.conf:287` warns about the general case).
- Settle-window length.
- NVML (`gpu_nvml.so`, section above) stays a **convenience**, not a blocker: `man 5 gres.conf`
  `MultipleFiles` lets MIG instances be hand-declared, and `enroll.render_gres` already hand-writes
  `gres.conf` from what each node reports.

Notably, only the head is planned to have multi-gpus. Could we instead make multi-gpu setup manually defined?

## IsaacSim feature implementation
Currently, the isaacsim arm feature is not implemented. We should fix that.

## Recipe Tidepools should keep all files
Recipe tidepools are just saved workspaces that can be revisted anytime (at the cost of karma, of course).