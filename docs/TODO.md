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

## Running a Recipe — [effort: medium]

All three of the Head's permanent consumers of disk are now bounded: uploads die at stage-in
(`projects.py`), outputs expire at 7 days against an 8GB **Allowance** (`retention.py`), and Recipes are
stored against a 24GB hard wall (`recipes.py`, `db` V12, `/api/recipes`). What is left is the one thing a
Recipe is *for*.

**`POST /recipes/{name}/run` answers 501.** Everything else is live on both clients — save, list, update,
delete, the wall (`cuttle recipe *` and the Recipes page). A Recipe already freezes everything a Spawn
needs, `outputs` included. What is missing is only the path itself: `spawns.submit_checked` has only ever
staged from an upload it was handed, and a Recipe is already on the Head, so running one needs a route
that copies `recipes/<user_id>/<name>/` into the project dir `projects.sweep` expects, carries the frozen
script/args/outputs/gpus/vram/minutes, and merges the Recipe's stored W&B config with the submitter's key.

Settled while building the wall, and worth keeping:
- A Recipe is **saved, never promoted**. Uploading to one runs nothing, so the wall never sits in the
  submit path and a full shelf can never block work (CONTEXT.md, **Allowance**). There is no
  save-as-Recipe on a finished Spawn, because `projects.sweep` deleted that code seconds after it staged.
- **Update is delete-then-save**, charged the delta — but weighed before a byte is written, so a refused
  update leaves the old snapshot intact and still runnable.
- Bytes are counted **uncompressed**, so the Head answers the same question the composer already asked
  from `File.size`.
- A deleted User's Recipes go **outright**: the rows CASCADE, `recipes.sweep_orphans` takes the files.

## Mouseover Text in Dashboard
We should move some raw text in the dashboard (namely, "Anything written outside these folders..." in the submission card), into mouseover text.
We should add a lot more useful mouseover text in general - the things that are said in other places when using the CLI. This text should also explain what
each thing is.

## Testing Arm Features and TUI Cleanup
The TUI is now too 'long' and arm features are untested. We need to run real hw tests to verify hw features work for IsaacSIm and VSim.

## Fully Implement Dashboard / Cleanup
Remove the demo pages, cleanup temp pages (Like karma's temp data), and plumb through all unfinished sections.

Known: the Account page's storage figure still reads `account/mock.ts`, not the User's real Recipe bytes
— so it shows mock usage against the real 24GB allowance. Recipes have a live backend now
(`GET /api/recipes` carries `size_bytes`); this is just unplumbed.

## Auto Download Outputs — [done 2026-08-02]

Built, on by default, CLI-only (`cuttle.watch`; the module docstring is the design). `cuttle run` writes
`~/.ccc/watch/<id>.json` and one flock'd detached sweeper extracts each finished Spawn's `$CCC_OUT` into
`./spawn-<id>-outputs/`. The entry — not the process — is the obligation, so a reboot costs nothing: the
root callback re-arms on any later `cuttle` command. `--no-download` opts out; `--follow` fetches inline.

Left deliberately undone, and each for a reason worth keeping:
- **The Head learns nothing.** No route, no column. Retention stays a pure function of time and
  **Allowance**, a fetched result is still fetchable from a second machine, and a Head that has not
  been redeployed cannot break the feature. The cost — fetched outputs still occupy Allowance — is
  accepted rather than fixed, because eviction that trusted a client's self-report would punish the
  honest client.
- **No Dashboard equivalent.** A browser cannot write to a folder unattended; the parity exception is
  noted in `CONTEXT.md`. Best available there would be "while this tab is open", which is a second,
  differently-behaved implementation to explain.
- **The Recovery Window stays manual.** The sweeper is awake inside the 30-minute window and could
  grab it, but auto-fetching it would make it something to plan around — which `CONTEXT.md` says it
  must never be.
