# Durable TODOs

## Feature: Automatic GPU partitioning — the Beak — [effort: high]

**Designed 2026-07-29, not built. Blocked on hardware, not on decisions.** Today a Spawn holds a
*whole* card; a job needing 2 GB parks on a 24 GB GPU alone. The **Beak** carves a card into
**Slice**s so several Spawns hold it at once. Glossary terms (Device, Slice, Carvable, Beak) are in
`CONTEXT.md`; this section is the implementation design.

**MIG only.** A card is sliceable or whole — "shared" always means hardware-isolated, never an honour
system. MPS/time-slicing is rejected: no memory isolation, so one Spawn can OOM another. Consequence:
the RTX 5080 and every WSL Arm (`/dev/dxg` is one node for all GPUs) are permanently whole-card. The
Blackwell 6000 Pros live on the **Head**, so all carvable hardware is Commons — Karma spent on a Slice
is destroyed, not transferred.

**The Head holds Spawns; `gres.conf` never lies.** Probed on 23.11.4 (2026-07-29, disposable
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

## Recovery Window -- [effort: med]
A Spawn's declared `$CCC_OUT` outputs already survive forever on the Head — nothing deletes them. The
gap: everything ELSE a Spawn wrote (relative paths, its whole working dir) is destroyed the instant it
ends, by the `cleanup()` EXIT trap's `rm -rf "$scratch"` (`head/api/src/ccc_head_api/spawns.py`). A
User who mis-declared `--output` loses that work permanently, with zero warning. "Hold" is avoided as
a name — it already means Escrow in this codebase.

- Always on, every Spawn (including Tide Pools) — the mistake this catches is by definition one the
  User didn't know they were making, so it can't be opt-in.
- On Spawn end: rsync the whole scratch dir home (widens the existing `stage_out`) instead of just
  `rm -rf`'ing it, *then* delete on the Arm — the donor box still ends up with nothing, same as today.
  Held on the Head for 30 minutes, then deleted.
- Two size caps: skip the recovery copy entirely for a Spawn whose scratch exceeds **16GB** (falls
  back to today's immediate `rm -rf`, no copy attempted); a **50GB** total pool cap across everything
  currently held, oldest evicted first when a new copy would exceed it.
- Separate artifact from `cuttle outputs` (declared, permanent) — a new command downloads the
  recovery-window copy while it's still open, distinct from the permanent output tar.
- Surfaced in two places: proactively at submission time (output-folder field, CLI and Dashboard —
  "anything outside a declared output dir is recoverable for 30 min after the Spawn ends") and
  reactively the moment it's discovered (`cuttle outputs` on a miss — tells you the window's still
  open + time left, if it is). Dashboard also shows time remaining on a finished Spawn with a real
  download button — full parity with the CLI, not just informational.

## Output retention: 7-day TTL + share the Recipe storage allowance -- [effort: high, blocked]

**Blocked on the Recipe backend, which does not exist yet** (`head/web/src/api.ts:198-199` says so
outright — Recipes today are entirely frontend-mocked, no DB table, no `/recipes` route, no server-side
quota check; the 20GB "wall" in `RECIPE_ALLOWANCE_BYTES` only exists client-side against mock data).
Do this once Recipes get a real backend (Phase 5), not before.

Declared `$CCC_OUT` outputs (`cuttle outputs <id>`) are permanent today — nothing ever deletes them.
Two changes, once Recipes are real:
- **7-day TTL**, with a warning to the User before/as an output expires.
- **Share the per-User 20GB Recipe allowance** with Recipes — but asymmetrically: a Recipe stays the
  hard-wall citizen (submission fails past the wall, User deletes one manually, unchanged from today);
  an output is the soft/evictable citizen, auto-evicted **oldest-first** to make room, for either a new
  output or a new Recipe save. Precedent for oldest-first eviction under a pool cap already exists in
  this same doc's Recovery Window section (50GB scratch pool) — reuse that shape, not a new one.

Not yet designed beyond this: exact warning timing/surface, and how output size gets tracked (nothing
computes or stores it anywhere today — new bookkeeping regardless of when this is built).