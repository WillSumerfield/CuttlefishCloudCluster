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

## Python Environments — [effort: high]

**Designed 2026-08-02. Substrate built; two pieces left — see "Not built yet" at the end.**
Before this, every Spawn got the *same* Python: `apptainer exec --nv
--contain ... "$sif" python <script>` (`spawns.py:340`), where `$sif` is always `base.sif` — pinned
`pytorch/pytorch` + `wandb` (`config.py:45`, `templates.py:267`), rebuilt only if an operator deletes it
(`setup.py:414`). No pip/uv/conda anywhere in the submit path, no CLI flag, no Composer field. A User who
needs one extra package has no supported way to get it. Glossary term (**Environment**) is in
`CONTEXT.md`; this section is the design.

**Declared by the User, built by the Head.** The manifest lives in the project, in the file the User's
own tooling already writes: `uv.lock` + `pyproject.toml` if present, else `requirements.txt`, else
`environment.yml`. Auto-detected at the project root — but detection is a guess, so a User may name the
manifest explicitly, and may name **none** (a `pyproject.toml` in a vendored repo is not an instruction
to the cluster). `docs/DesignDoc.md:81` describes a hash-triggered update against a "persistent storage
slice"; there is no such slice and never was — that line is superseded by this section.

**uv layers, conda replaces.** The uv path is a venv with `--system-site-packages`: it inherits the
image's torch/cuDNN/wandb and builds only the *delta*, so the usual env is a few MB, not 2.5 GB. The
conda path is its own prefix with its own interpreter — it replaces everything, so CCC injects `wandb`
into it at build or the Head's promise that "a Spawn logs to its submitter's own account"
(`CONTEXT.md`) quietly stops holding. A manifest that names a package the image already ships wins,
**silently**: it is what the User declared, and CCC does not second-guess it.

**Built on the Head, before placement — in a Pod, as the Guise.** Building inside the allocation would
make a User pay **Karma** to hold a GPU while pip downloads; building on the Head costs nothing and
needs no per-Arm cache. But it puts user-supplied code (build backends, sdist `setup.py`, conda
post-link scripts) on the one machine holding the Mantle's key and every User's data — so the build runs
in `base.sif` under the submitter's **Guise**, reusing the isolation CCC already has rather than
inventing a second kind. Head egress is not a new assumption: `setup` already pulls `docker://` at build.

**Nothing is cached except downloads.** Built envs are never kept — rebuilt per Spawn, destroyed with
that Spawn's staging exactly like the uploaded project (`projects.py`). This deliberately keeps the Head
from growing a *fourth* permanent disk consumer beside projects, outputs (`retention.py`) and the
blocked Recipe wall, and keeps **Allowance** meaning what `CONTEXT.md` says it means — a ceiling on
things a User would *lose*, which a rebuildable env is not. What does persist is the package **download**
cache (`~/.cache/uv`, the micromamba pkgs dir): Mantle-owned, size-capped, not user-visible, not staged.
It is what makes "rebuild every time" affordable — a 50-Spawn sweep then costs 50 local relinks, not 50
re-downloads.

**One fixed bind path, so nothing needs relocating.** The env is built and run at the *same* absolute
path inside the Pod on both machines, via `--bind`. Venv shebangs and `pyvenv.cfg` keep pointing
somewhere real, and conda prefixes — which are notoriously non-relocatable — work unmodified. This is
strictly cheaper than `uv venv --relocatable` plus conda-pack-style prefix rewriting, which would be two
fixup mechanisms and one well-known source of subtle breakage.

**A Spawn now exists before Slurm does.** CCC has no states of its own today — `PENDING`/`RUNNING` come
straight from `squeue` (`slurm.py:218`) and the Karma tick derives from that poll. A building Spawn is
**Head-held**: Escrowed (Escrow opens when the Head *accepts*), visible in the queue with a reason, and
generating **no Demand** until it is a real Slurm job that could actually be placed. This is the same
held-Spawn concept the Beak needs (`docs/TODO.md`, "the Head holds Spawns"); build it once, use it twice.

**Failure is a Spawn, not a rejection.** A manifest that won't resolve can't be caught inside the submit
POST (a conda solve is tens of seconds, on top of a 4 GB upload), so the Spawn already exists. It fails,
with the build log as its `err`, and is billed nothing — it held no **Device**, so the Meter genuinely
reads zero. This is *not* a second settle-at-zero rule: **Retract** remains the only ending that
overrides the Meter. Both a build **timeout** and a built-**size** ceiling are cluster constants beside
`MAX_UPLOAD_BYTES`; breaching either takes the same path. Size matters because a built env is not
uploaded and so escapes the 4 GB cap, yet still gets rsync'd onto a donor's disk over the **Siphon**.

**Said out loud at submit, in both clients.** One line before acceptance — "environment: `uv.lock`, 37
packages on top of the cluster base", or "no dependency file found — running against the cluster base".
The worst case is a manifest one directory too deep: not a build failure at all, so nothing else would
catch it, and the User discovers it as an `ImportError` *after* paying for a GPU. Pairs with the mouseover
work below.

**Tide Pools get the same env, writable.** You debug against what your Spawn will actually get, and
`pip install` at the shell works and dies with the session — the env is already that session's own
node-local copy, so read-only would buy no safety.

**Recipes are unaffected, and "frozen" is narrowed.** A Recipe captures code, so it captures the manifest
for free. Frozen means the bytes captured, *not* what they resolve to: a loosely-named dependency builds
a different Environment months later. Pinning is the User's to do, with `uv.lock`, which is what
`uv.lock` is for. Recipes have no backend yet (see the Recipe wall above), so nothing to build here now.

**Arm Features are out of scope, deliberately.** `vsim`/`isaacsim` stay path passthrough via
`/etc/ccc/features.env` (`spawns.py:91`). IsaacSim ships its own interpreter; a script that wants it
launches it from `$ISAACSIM_PATH` itself. A Feature-declared interpreter would mean building against
something that only exists on *some* Arms, which breaks Head-side building outright. Consequence to state
plainly in the docs: for such a Spawn, a declared manifest is simply irrelevant.

**Numbers, and where the tooling lives.** Build **timeout 15 min** (clears a hung solve, spares a slow
one), built-env ceiling **8 GB** (a real torch conda env fits; matches `retention.py`'s per-User output
Allowance), download cache **20 GB**. `uv` and `micromamba` are single static binaries installed at a
Head-only path and `--bind`ed into the build Pod — `base.sif` is **untouched**, so no node rebuilds and
no Arm carries a solver for work only the Head does. Plain `uv sync`, no `--no-install-project`: uv
already treats a project with no `[build-system]` as *virtual* and installs only its dependencies (this
repo's own root `pyproject.toml:18` is exactly that), while a real src-layout package gets installed,
which is the only way its imports resolve.

**A Head restart mid-build fails the Spawn.** No resumption, no retry counter: any Head-held Spawn whose
build is not live is failed with the reason, and the User resubmits. The download cache makes that cheap
to redo, and it keeps the reconciliation to a single rule.

**Built so far.** `environments.py` (detection, build rendering, the build itself, cache pruning), the
JOB_SCRIPT wiring (`$py`/`$envbind`, the move out of `$proj`, the Tide Pool's PATH), `submit` building
before `files` is computed, the `apptainer` grant on the Guise sudoers rule, and `tests/test_environments.py`.
One pre-existing behaviour changed on purpose: `files` now lists **symlinks**, not just regular files —
a `--system-site-packages` venv's `bin/python` points into the Pod image, so `is_file()` alone reported
it missing and the env would have arrived on the Arm with no interpreter.

**Not built yet, and the first is a real divergence:**
- **The Head-held state.** The build currently runs *inside* `submit`, so a broken manifest is a
  **rejection** (4xx at submit), not the failed Spawn this design calls for. Consequence today: the
  submit request blocks for the length of the build, and a User sees an error instead of a Spawn in
  their history that explains itself. Fixing it needs a CCC-owned state before Slurm — the same one the
  Beak needs, so build it once for both. Everything *about* the build (where, as whom, what it produces)
  is already final; only when the Spawn comes into existence is not.
- **Installing `uv`/`micromamba` to `TOOLS_DIR` at `setup --head`**, and the submit-time echo in both
  clients (`describe()` exists and is tested; nothing calls it yet). Also `--manifest` on `cuttle run`
  and in the Composer — `submit(manifest=...)` is plumbed and defaults to auto-detect, so the parameter
  is there and unreachable from either client.

## Testing Arm Features and TUI Cleanup
The TUI is now too 'long' and arm features are untested. We need to run real hw tests to verify hw features work for IsaacSIm and VSim.

## Fully Implement Dashboard / Cleanup
Remove the demo pages, cleanup temp pages (Like karma's temp data), and plumb through all unfinished sections.

Known: the Account page's storage figure still reads `account/mock.ts`, not the User's real Recipe bytes
— so it shows mock usage against the real 24GB allowance. Recipes have a live backend now
(`GET /api/recipes` carries `size_bytes`); this is just unplumbed.

## Auto Download Outputs
When a spawn is submitted, the submitter should automatically download the outputs when the spawn finishes.
