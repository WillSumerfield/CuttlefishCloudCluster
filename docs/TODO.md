# Durable TODOs

## Untriaged — launch-readiness grill (2026-07-15)

Found by exploring the actual `head/api` + `head/web` code against `temp/ROADMAP.md`, `docs/TODO.md`,
and `CONTEXT.md` — each item below was confirmed absent from all three (not deferred, not resolved,
just never written down). Direction below is a first pass, not a locked design; each item gets its own
grill before landing in `temp/ROADMAP.md`'s new **Phase 7 — Loose Ends**.

- **[med] No backup/restore story for cluster state.** Widened 2026-07-15 from head.db alone: `arms.json`
  (the munge-owned Arm registry — owner names, admitted status) and **Headscale's own sqlite DB** (node
  keys, preauth keys) are equally un-backed-up. Losing Headscale's state means every Arm must physically
  re-enroll, so backing up head.db alone (Users, sessions, Karma ledger, spawn/attribution history)
  still leaves a Head-disk failure evicting the whole fleet. → Phase 7.
- **[med] The Head API doesn't log.** Confirmed: `main.py`, `spawns.py`, `slurm.py`, `enroll.py`, `admin.py`,
  `karma.py` have zero logging calls and no custom exception handlers; only `tick.py` (the Karma meter)
  logs anything. `/healthz` is a static `{"ok": true}` with no DB/Slurm reachability check. → Phase 7.
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
- **[med] No end-user-facing docs.** `CONTRIBUTING.md`/`CONTEXT.md` are dev-facing; nothing explains
  logging in, submitting a Spawn, or reading Karma standings to a non-admin User, and nothing tells a
  prospective donor what enrolling their box actually means (runs arbitrary code as the Mantle, holds
  the cluster-wide munge key — see the security items above). Wanted: both a User quickstart and a
  donor consent note. The quickstart should also state plainly that folder upload needs a
  Chromium-based browser (File System Access API / `webkitdirectory` support; decided 2026-07-15 not to
  engineer around Firefox/Safari for now — document the requirement instead), and that the Tide/Demand
  charts are in **UTC**, not the viewer's local time (`temp/KARMA.md`'s "Resolved" list). → Phase 7.
- **[low — mostly shipped in Phase 6] CLI install/setup UX needs a real pass** (merges 2026-07-15's "CLI install/setup UX" finding with
  the older "Polish: nicer `cuttle setup` UX" item, now deleted below). MVP setup just echoes shell
  commands + apt spew. Wanted: a progress TUI (step list with spinners/checks; rich/textual), quiet by
  default; a full install log at `~/.ccc/log/setup-<ts>.log` (verbose output captured there, not
  dumped to the terminal) with install info printed directly to the user instead of buried in it; a
  clear final summary (node state, versions, config path); prompts grouped up front and validated, then
  run unattended; and — new this pass — a first-run prompt that walks a fresh user through *how* to set
  up (Head / Arm / client — see next item), and fixing `setup_arm`'s install-order bug (apt install
  runs mid-conversation with the Head's enrollment gates for no reason — see that item further down).
  → Phase 7.
- **[done: Phase 6 — `setup --client`] No "client-only" install path.** Today `cuttle setup` only knows `--head`/`--arm`; a person who
  only wants to log in, browse the Dashboard, and submit Spawns from the CLI — never donating hardware —
  has no clean onboarding (the roadmap's `--head <url>`-in-`~/.ccc/config` pure-submitter path exists
  in the design but isn't a named, installable mode). Wanted: a real client-only mode. → Phase 7.
- **[done: Phase 6 — `build-installer`] No `cuttle` installer** (merges 2026-07-15's finding with the older "Polish: `cuttle` on PATH" item,
  now deleted below). Today `cuttle` lives only in the project venv (`cli/core/.venv/bin/cuttle`) —
  users must call the full path or activate the venv. A client-only User (previous item) has no repo to
  clone in the first place, which makes this more than polish for that audience. Wanted: a real install
  that puts `cuttle` on PATH — symlink into `~/.local/bin`, a `pipx`/`uv tool install`, or a one-line
  curl bootstrap. → Phase 7.
- **[med] Warning/error message quality, both clients.** No specific bug filed (unlike the `/passwd` 401
  confusion already fixed, docs/TODO.md's Slice-4 note) — a general pass wanted over CLI and Dashboard
  error/warning copy before non-expert Users rely on it. → Phase 7.


> The items below were found in the Phase-5 frontend grill (2026-07-14, `temp/FRONTEND.md`). **Return**
> was a missing design and went to Phase 2 of the roadmap. The rest exist in **both** clients, so none is
> "a frontend job". ~~Cancel~~ and ~~the admin `--node` selector~~ were verified fixed on the
> `phase1-substrate` WIP branch (2026-07-15 grill: `LiveQueue.tsx`/`SpawnRow.tsx`/`api.ts:cancelSpawn`;
> `QueuePage.tsx:78` passes `isAdmin` into `SubmitComposer`) and dropped from this list.

## ~~`stderr` is unreachable from every client~~ — DONE (Phase 8)

Added `GET /spawns/{id}/stderr` beside `/logs` (stdout); both plain-text, empty (not 404) when the file
isn't there yet. `cuttle logs` now prints **both** under `=== stdout ===` / `=== stderr ===` headers
(so a crash's traceback — in `err`, with `out` empty — is never hidden), and `cuttle logs -f`
interleaves the two live, each line tagged `[out]`/`[err]`.

Scope correction found on the way: the **Dashboard log pane doesn't exist yet** (`SpawnPage.tsx` is a
Placeholder), so this was API + CLI only. The placeholder already promises "stdout and stderr,
separately", and the two endpoints serve exactly that for when the pane is built.

## No password recovery exists → Phase 5 — [effort: med]

`cuttle user` has only `add` / `list` / `remove` (the roadmap's Q5 promised a `reset`; it was never
built), there is no email, and `/passwd` requires the *current* password. A User who forgets their
password therefore cannot be recovered — the Admin's only move is `user remove` + `user add`, which
**deletes their Karma balance and orphans every Arm they own** (owner is a bare username string in
`arms.json` with no FK, so those Arms end up pointing at a User that no longer exists).

Fix: an admin-mediated reset that issues a fresh temp password + `must_change_password=1`, exactly like
provisioning. Same shape as `user add`, no new concept.

Related, same cause: `is_admin` is only settable **at creation**, so promoting an existing User has the
identical delete-and-recreate problem. (The Ph5 grill decided **not** to add grant/revoke-admin to the
Dashboard, so this one can stay a CLI-only gap — but it must not be *fixed* by recreating the account.)

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

## ~~`head/web`'s build has no typecheck pass~~ — DONE (Phase 8)

`build` is now `tsc && vite build`, so a type error (e.g. the admin-selector prop bug) fails the build
instead of shipping silently. Added a standalone `typecheck: tsc --noEmit` for editor/manual runs.
Code typechecks clean today, so this was config-only.

## ~~`cuttle history`/`queue` print unbounded output~~ — DONE (Phase 8)

`history`/`queue` take `--limit N` (`--limit 0` = all); default caps the human table to the newest/
front 50 while `--json` stays uncapped (explicit `--limit` honoured either way). Cap is CLI-driven over
an optional server `limit` param on `GET /spawns` + `/spawns/history` (absent = unbounded, so the
Dashboard read is unchanged). Truncation prints a hint line; never in JSON.

## ~~Test suite: `StarletteDeprecationWarning` (httpx2)~~ — DONE (Phase 8)

Swapped the root test harness's dev dep `httpx` → `httpx2` (the client `starlette.testclient` now
prefers). Warning gone, 226 pass. Plain `httpx` still arrives transitively via `cuttle`'s runtime dep,
so the CLI is untouched.

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

## ~~W&B project + entity should be per-Spawn, not per-machine~~ — DONE (⑦)

`cuttle run --project/--entity` inject `WANDB_PROJECT`/`WANDB_ENTITY` per-Spawn (CLI flag → config
default → project 'ccc' / entity account-default; empty entity is omitted, not passed). API key stays
in config. The `examples/train.py` `entity="Will@ARIA"` hack is gone. Reminder kept for users: wandb
wants the entity **slug** (`wandb.ai/<slug>/<project>`), not the display name.

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

## `cuttle teardown --arm` does not exist → Phase 2 — [effort: med]

*Grouped 2026-07-15 with the two items below, as the Arm-lifecycle-reliability bucket of Phase 2.*

`teardown_local` is the only teardown. A donor who wants to leave has no supported path: stop and
disable slurmd, drop `/etc/default/slurmd`, leave the mesh (`tailscale down` + wipe CCC state),
remove the `ccc0` ufw rule, and undo the WSL Apptainer config if it was applied. The Head half
(`cuttle arm remove`) already exists and is what evicts the node from the mesh and Slurm — an Arm
tearing itself down cannot do that for itself, so the two commands are complements, not substitutes.

## ~~`preflight()`'s GPU check passes on WSL for the wrong reason~~ — DONE (Phase 8)

New `system.nvidia_smi()` resolver: PATH first, else the absolute `/usr/lib/wsl/lib/nvidia-smi`
(`config.WSL_NVIDIA_SMI`) when `is_wsl()` and it exists, else the bare name so a driver-less native box
still fails loudly. Both call sites route through it — `preflight()` **and** `gpu_vram_mb()`, which had
the identical latent bug (it bakes the node's `vramN` feature at registration and would break from any
non-interactive context on WSL). So the GPU checks no longer depend on being run from a login shell.

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

## Remote scratch orphans on SIGKILL → Phase 2 — [effort: low]

④ wipes a remote Spawn's node-local scratch (`/tmp/ccc-<jobid>`) with a bash `trap ... EXIT`. That
fires on normal exit and ordinary signals, but **not** SIGKILL — OOM-killer, `scancel -9`, and (Phase
2) a Retract all bypass it, orphaning the scratch dir on the donor. Fix with a Slurm epilog that
`rm -rf`s the job's scratch, or a boot-time sweep of `/tmp/ccc-*`. Pairs naturally with Phase-2 Retract
(which needs an epilog anyway). **Severity dropped (slice 3):** scratch moved from the (home-less)
Mantle's home to `/tmp`, so an orphan is now swept by the OS at reboot rather than accumulating
forever. Until the epilog lands, a hard-killed Spawn still hogs donor disk until the next reboot.

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
