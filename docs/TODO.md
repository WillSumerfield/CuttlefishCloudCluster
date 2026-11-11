# Durable TODOs

Designed but not built. Everything in the wiki's Contributing/Architecture exists; everything here
does not. This file is the dev design backlog.

## Feature: Automatic GPU partitioning — the Beak — [effort: high]

**Not built — blocked on hardware, not on decisions.** Today a Dive holds a
*whole* card; a job needing 2 GB parks on a 24 GB GPU alone. The **Beak** carves a card into
**Slice**s so several Dives hold it at once. Glossary terms (Device, Slice, Carvable, Beak) are in
`CONTEXT.md`; this section is the implementation design.

**MIG only.** A card is sliceable or whole — "shared" always means hardware-isolated, never an honour
system. MPS/time-slicing is rejected: no memory isolation, so one Dive can OOM another. Consequence:
the RTX 5080 is permanently whole-card. The
Blackwell 6000 Pros live on the **Head**, so all carvable hardware is Commons — Karma spent on a Slice
is destroyed, not transferred.

**The Head holds Dives; `gres.conf` never lies.** Probed on Slurm 23.11.4 (disposable
slurmctld, live cluster untouched): a GRES type declared with `Count=0` is rejected at submit
*identically* to an undeclared type — Slurm validates against configured **counts**, not type names.
Declaring maximum counts instead is worse: slurmctld would believe `gpu:big:1,gpu:small:4` are
simultaneously available and double-allocate the same silicon. **Slurm cannot represent
mutually-exclusive GRES.** So `gres.conf` always states the exact current carve (real device files →
`ConstrainDevices` keeps enforcing, counts genuinely add up), and CCC holds Dives whose shape does not
exist yet in the Head DB, showing one merged queue ordered by the same Karma Share. The Head **rejects**
what the fleet can never do (`slurm.py:116`, already checks full card VRAM, survives slicing untouched)
and **holds** only what it cannot do yet.

**Waiting is the Beak's job, not Slurm's.** Slurm cannot reserve a shape that does not exist, so the
Beak computes earliest-feasible-start itself from running Slices' declared ends. It carves for a
lower-priority Dive only if that Dive's declared end falls before the top pending Dive's earliest
start — otherwise it holds the space **deliberately idle**. (16 GB tenant ending t=100, 96 GB Dive
waiting: a 30-min 64 GB Dive is carved for; a 200-min one is not.) This makes `--time` load-bearing
for placement, not just pricing.

**Rules settled by scenario:**
- Re-carve is **withdraw-then-destroy**: rewrite `gres.conf` and reconfigure, confirm nothing holds it,
  *then* touch the hardware. Slurm can never allocate what it cannot see.
- An **idle** Slice has no claim — destroy it for a higher-Karma Dive. Only a *held* Slice is immutable.
- A **settle window** before acting, so submit/cancel churn costs no MIG resets.
- Never hand out a Slice larger than requested; reshape instead.
- Greedy down the priority queue, subject to the waiting rule above.

**Pricing.** Power is **measured per profile**, never derived — a quarter card is not a quarter of a
card's Power. Measured once at `setup --head`, while the cluster provably has no work. Demand attaches
to the **physical card** and is counted in whole-card-hours, so carving can never move a price. Escrow
opens when the Head *accepts* a Dive, held or not, or queueing for the scarcest hardware becomes the
one free action in the economy.

**Open, needs the real hardware:**
- Read the actual profile table (`nvidia-smi mig -lgip`); everything above assumes coarse pre-declared
  sizes and *positional* placement rules (a contiguity/alignment constraint, so free capacity can exist
  in a shape nothing fits — the Beak is a bin-packer, not a subtraction).
- Confirm `scontrol reconfigure` can change GRES record counts for *idle* records without renumbering
  and mis-pointing a record a running Dive holds (`gres.conf:287` warns about the general case).
- Settle-window length.
- NVML (`gpu_nvml.so`, section above) stays a **convenience**, not a blocker: `man 5 gres.conf`
  `MultipleFiles` lets MIG instances be hand-declared, and `enroll.render_gres` already hand-writes
  `gres.conf` from what each node reports.

## A runtime profile for `isaaclab` — [effort: medium]

`isaaclab` is catalogued and placed, and that is all: an Arm declaring it gets a Slurm constraint and
`$ISAACLAB_PATH`, and the submitter's script does the rest. It has no **Profile**
(`ccc_head_api.features`) the way `vlearn` does — no env vars, binds, working directory, cache, or
system libraries arranged on the Dive's behalf.

That is what is missing. A Feature carrying a profile is the difference between CCC knowing what a
name *means* and CCC merely placing on it, and the parts a submitter cannot do for themselves are
exactly the parts a profile exists for: the system libraries Isaac links against are CCC's to arrange
Arm-side — unpacked prefixes put on the wheel's `.so` RUNPATH, as `vlearn` now does
(`cuttle.features.rpath_wheel_libs`) — not Python packages that any **Environment** could install.

Blocked on having the software to probe — the profile has to state real ABI tags, real library paths
and real cache locations, and none of those can be guessed. Note that `ccc_head_api.features` and
`test_features.py` currently record placement-only as the *finished* answer for `isaaclab`; both say
so deliberately and both change when this is built.

The profile machinery `vlearn` uses is the model to copy: an Arm-side `ArmProfile`
(`cuttle.features`) with the package wheels, system-library specs and a `profile.json` descriptor;
the in-container hook (`cuttle.feature_hook`) that arranges it after the Environment build; and the
Head-side `Profile` (`ccc_head_api.features`) holding the description and the supplied-package gate.
It is all Podman/in-container now — the old Apptainer render path is gone.
