# Cuttlefish Cloud Cluster (CCC) Design Specification

## 1. System Architecture & Node Taxonomy
The CCC is a distributed GPU/CPU computing network: donate spare compute, run sandboxed experiments on
it, and settle the exchange through a Karma token economy.

Arms run **Ubuntu 24.04** — natively, or under **WSL2** on Windows. `slurmd` and the container runtime
always run in that Linux layer, so there is a single Linux code path everywhere. GPU support is
**NVIDIA-only**.

### The Head (Control Plane)
*   **Purpose:** Central control plane running the cluster scheduler orchestrating resource allocations, tracking active jobs, and maintaining queue fairness.
*   **Tech Stack:** Slurm Workload Manager (`slurmctld`). No `slurmdbd`/MariaDB — Karma sets Slurm
    priority itself and needs no accounting database (§5). Head-side only, Linux.

### Permanent Arms (Dedicated Server Nodes)
*   **Purpose:** High-availability, permanently attached multi-GPU servers for heavy, non-volatile compute workloads.
*   **Tech Stack:** Ubuntu 24.04, NVIDIA driver + `nvidia-container-toolkit`.

### Volatile Arms (Edge Nodes)
*   **Purpose:** Desktop computers or physical robot controllers used locally that donate spare compute to the cluster but can instantly reclaim 100% of their resources.
*   **Volatility is not yet built.** Every enrolled Arm is a permanent one: the `--volatile` flag and
    Slurm preemption (`PreemptMode=REQUEUE`/`SUSPEND`) are design only, waiting on the hardening
    phase. **Retract/Return (§5) is built and applies to every Arm**, so an owner can already reclaim
    their machine without the volatility flag existing.

### The Gateway (Secure Access)
*   **Purpose:** Zero-trust, authenticated network entryway protecting the cluster and enforcing **built-in Head authentication** (admin-provisioned username/password). Required for all human usage.
*   **Tech Stack (locked):** **WireGuard mesh** (Tailscale, or self-hosted **Headscale**) as the
    cluster fabric: every node — Head and all Arms — joins one flat encrypted network with automatic
    NAT traversal, so `slurmd`↔`slurmctld` and staging behave like a LAN. **Node enrolment** is
    authorized by an Admin-minted one-time token (which also names the Arm's owner); **human access +
    job submission** are gated by **built-in auth on the Head** (own user table, argon2 + sessions) —
    no external IdP. TLS for the human surface: no CA will sign a cert for a box with no public domain
    name, so the Head signs its own, and the CLI/browser trust-on-first-use pin it (SSH-`known_hosts`
    style). Public reachability (a tunnel or DDNS + a real cert) is deferred — the Head is assumed
    reachable over the LAN or the CCC mesh.

---

## 2. Resource, Sandbox, & Licence Management

### Partitioning & Heterogeneous Scheduling
*   **Purpose:** Separates CPU and GPU resources so they can be requested independently, bundled, or combined across multiple distinct node types. Handles overallocation gracefully.
*   **Tech Stack:** Slurm Generic Resource (GRES) scheduling configured via `slurm.conf`. GPU
    support is **NVIDIA-only** (native Linux driver, or Windows via WSL2 CUDA passthrough, both wired
    through `nvidia-container-toolkit` + Apptainer `--nv`). A non-NVIDIA machine may still join as a
    CPU-only Arm.

### Pods (Isolated Environments)
*   **Purpose:** Sandboxes jobs to prevent overallocation crashes, stops users from erasing external host memory, and preserves node-locked software profiles.
*   **Tech Stack (locked):** **Apptainer** (formerly Singularity). Rootless and daemonless, so the job
    runs as a plain Slurm-launched process with a clean kill; maps host IDs easily and inherits host
    node-locked licence profiles; single-file `.sif` images; GPU via `--nv`. Requires
    unprivileged user namespaces on the Arm's Linux layer (native/WSL2). Resource caps enforced
    via Slurm `cgroups` on the Linux layer, not a Docker daemon.

### Arm Features & Hardware Licence Management
*   **Purpose:** Routes a Spawn that needs a heavy install or node-locked software licence to the
    exact Arm(s) that carry it.
*   **Tech Stack:** A **Feature** is a name in a catalog built into CCC (`config.FEATURE_NAMES`, e.g.
    `vlearn`, `isaaclab`) — not an admin-defined string, because CCC has to know what the name means:
    at minimum placement + `$<NAME>_PATH`, and for some names the runtime wiring (env vars, binds,
    cache) a Spawn needs as well. Declared by an Arm's owner at `setup --arm`, it becomes a Slurm Node
    Feature; a Spawn requests one with `--feature`, which the Head turns into a `--constraint` AND-clause
    the same way it turns `--vram` into one. A **Licence** (permission to run the software) is bound to
    the machine, not the User: it lives in one directory per Arm, owned by a dedicated `ccc-licence`
    account, and is bound at the Spawn's **Guise** home inside the Pod — so the copy that runs is
    always the Arm's own activated copy. See root `CONTEXT.md` — Feature, Licence, Licence account,
    Activation Spawn.

### Shared Storage (Stage-in / Stage-out)
*   **Purpose:** Gets a Spawn's project onto the Arm that runs it, and its results back, without a
    live shared filesystem.
*   **Tech Stack:** No NFS/CephFS, no live mount — every Arm stages. The gitignore-aware project dir
    is staged in over an rsync daemon bound to the Head's mesh address, into node-local scratch, then
    a designated `$CCC_OUT`/`outputs` dir is staged back at the end. Per-Spawn, a fresh copy (no
    persistent cache yet). Still open: dedup/caching for sweeps and live log streaming for remote Arms.
*   **The last hop is automatic** (`cuttle.watch`): results do not stop at the Head. `cuttle run` records
    what this box is owed and a single detached sweeper brings each Spawn's `$CCC_OUT` down into
    `~/ccc/outputs/spawn-<id>-outputs/` as it finishes (`--dest` to choose elsewhere) — one predictable
    place rather than results scattered across every directory anyone submitted from, and nothing is
    lost to a reboot,
    since the on-disk entry is the intent and any later `cuttle` command re-arms a sweeper for it. The
    Head is unchanged by this: retention stays a pure function of time and **Allowance**, so the same
    result is still fetchable by hand, from a second machine, for its 7 days.

---

## 3. Automation, Recipes, & Experiments

### Recipe (Saved Project Config)
*   **Purpose:** The saved metadata structure mapping a project's location, its auto-detected software dependencies, its user configuration, and its baseline hardware requirements.

### Spawns (Active Experiments)
*   **Purpose:** A single, traceable execution of a Recipe passing unique hyperparameters (`config.yaml`) into the container.

### `cuttle` and the Dashboard
*   **Purpose:** Two clients over one Head API, at operation parity: submit, follow, and manage
    Spawns without hand-writing Slurm syntax.
*   *`--time` and `--vram` are required, with no default* — a Spawn's cost can't be priced without a
    declared length, and a defaulted `--vram` would make every untouched submission eligible for the
    fleet's biggest card alone. There is no static analysis of the submitted script; a User states
    what they need.
*   *Environments:* A project declares its dependencies in the file its own tooling already writes
    (`uv.lock`/`pyproject.toml`, `requirements.txt`, or `environment.yml`); the Head builds that
    environment before the Spawn is placed and keeps none of it.
*   **Tech Stack:** `cuttle` is a single **Python** CLI (`typer`) — recipes, job submit, and W&B
    injection — with an interactive wizard over the same flags for `setup`; no separate host-native
    launcher. The **Dashboard** is a React/Vite SPA over the FastAPI Head API (live node + Spawn state,
    submission, account and admin pages), served on the CCC mesh + LAN, and is the **primary** surface
    for everyday use.

### Interactive / Testing Mode (Tide Pool)
*   **Purpose:** Lets a user drop directly into a live Pod on an allocated Arm to run code interactively, debug, and probe hardware behaviour before committing a full Spawn. Motivated by heterogeneous hardware not behaving identically (differing GPU archs, driver versions, node-locked licences) — a run that works on one node may fail on another.
*   **Behaviour:** Allocates resources like a normal Spawn but attaches an interactive shell inside the Pod (container) instead of executing a batch script. Bills Karma while attached.

---

## 4. Telemetry

### System Logging & Metric Integration
*   **Purpose:** Captures live training statistics, flags code errors, and measures hardware health (power usage, temperature, VRAM) over time per individual Spawn.
*   **Tech Stack:** **Weights & Biases (W&B) Core API Engine**. `cuttle` injects the W&B env into the
    sandboxed container at runtime — `WANDB_API_KEY` from config, plus per-Spawn `WANDB_PROJECT`,
    `WANDB_ENTITY`, and `WANDB_NAME` (`cuttle run --project/--entity`).

### Head-box Monitor & Rogue-Actor Detection
*   **Purpose:** Watches the Head's own box for trouble — disk space, process liveness, and hardware
    utilization happening *outside* Slurm's scheduler (an un-logged local process, a stray job) — and
    pings the operator.
*   **Tech Stack:** A standalone script (`ccc-monitor`), installed by `cuttle setup --head` and run by
    a systemd timer, checking `nvidia-smi`/`squeue` for GPU or CPU activity above 10% sustained over
    several checks while nothing is scheduled, and emailing the operator over SMTP when a check fires.
    Scoped to the Head's own co-located Arm today; per-Arm fleet-wide monitoring (Prometheus/Grafana,
    per-user alerting, W&B-triggered NaN/freeze pings) is not yet built.

---

## 5. The Karma Economy
Karma exists to make **donating hardware rational** — FairShare cannot pay people for supply, so it
cannot grow a cluster — and to give the operator price/payout/admin levers FairShare doesn't have.

### Karma ranks; it never gates
There is **no floor, no bankruptcy, and no balance at which work is refused.** Karma is *purely
relative*: a user with little of it loses **contention** to users with more, and loses nothing at all
when nobody is competing — an idle GPU is worth nothing, so refusing a poor user's 3am job would burn
capacity to punish him and move no work to anyone else. Karma has *no absolute mode*; it is not a
currency you can run out of.

*   **Balance** — an unbounded signed score centred on **0**, where every user starts.
*   **Share** — the effect a balance has: a **saturating** (sigmoid) function of it. Hoarding has
    diminishing returns; debt has diminishing punishment. Neither end runs away, so a whale can't own
    the cluster and last place isn't a life sentence.
*   **Decay** — balances drift back to 0 **within a month**. Debt is forgiven, hoards evaporate, and
    standing tracks *recent* conduct. The curve is flat then steep rather than a plain exponential:
    a fortnight-old event still counts for most of its face value, a month-old one for almost nothing.
    Past a month an event is deleted outright, which is what keeps the ledger bounded.
*   **Persistence:** balances, ledger, and demand history are SQLite tables in the Head API's own
    `head.db`. The API is already the single writer and already the only thing that knows which user
    submitted which Spawn, so escrow is atomic with the Spawn insert — no second daemon, no IPC.

### Token flows — a closed transfer
Every flow is a **transfer**: the submitter is debited and the **owner of the Arm it ran on** is
credited *the same Karma*. Two consequences fall out for free, rather than being mechanisms:

*   **Σ balances ≡ 0, always.** Everyone starts at 0 and transfers preserve the sum, so there is no
    supply to inflate or deflate. A new user joins at exactly average, diluting nobody. Decay makes 0
    an *attractor*, so anything burned or conjured is reabsorbed without policing.
*   **Running on your own Arm is free** — you pay yourself. Donating a machine buys you unmetered use
    of it *plus* income when others use it. That is the donation incentive, and it needs no special rule.

Karma spent on a **commons (admin-owned) Arm** has no owner to pay and is simply **destroyed** —
consuming the lab's shared kit lowers your standing relative to those who don't, which is exactly right.

### The admin is outside the economy
The admin is a role for **running** the cluster, not for using it. They earn no Karma (any Arm they own
is a commons Arm, so its Karma is destroyed rather than credited), pay none, and hold no Balance at all:
their Spawns are never escrowed, never settled, and never re-ranked. A person who wants to *use* the
cluster does so as an ordinary user, with an ordinary account.

*   It costs almost no code. The tick already skips any Spawn it cannot attribute to a chargeable person
    (the enrollment benchmark, which is sbatched outside the API); an admin's Spawn simply joins that set.
*   An **Override aimed at an admin is refused**, and that is load-bearing rather than tidy: Karma
    conjured into an account excluded from the standings would still count toward `Σ balances`, and the
    invariant the whole ledger is shaped around would quietly stop holding. Admins never reach the
    ledger, which is what keeps it true.
*   ⚠ The consequence is that an admin's Spawns sit at nice 0 and therefore **outrank every user's, for
    free**. Deliberate — the role is trusted by construction and can Queue-bump regardless — but it means
    doing research from the admin account would silently exempt that work from the economy.

### Price = Power × Demand
The cost of a device-second is **what the device can do** × **how badly it's wanted**. Capability and
scarcity, measured separately. Several devices allocated = several separate purchases.

*   **Power** — throughput, **measured at enrollment** by a benchmark Spawn the Head submits down the
    normal Slurm path, *inside a Pod*. It therefore sees the GPU as real Spawns will — WSL's
    virtualized `/dev/dxg`, a throttled laptop card, a stale driver, a x4 PCIe lane — none of which a
    spec sheet knows, and all of which are the norm on donated personal machines. **Power is not
    optional:** a meter that counts only seconds pays a slow card *more* for the same work, so
    *donating your worst hardware would pay best* — an inversion aimed straight at Karma's purpose.
*   **Demand** — device-seconds *asked for* (running **and queued**) ÷ device-seconds that exist. A GPU
    used for an hour with someone queued behind it the whole time was demanded for **two**. Utilization
    reads ~100% on every card the moment the cluster is busy and stops discriminating exactly when price
    matters most; Demand keeps climbing with the queue behind it.
    *   It has a **floor of 1** on any card actually running work — a running Spawn counts *itself* — so
        a second is never sold below its Power, and the cluster's quietest hours are not its cheapest to
        exploit.
    *   And a **ceiling** (`DEMAND_MAX = 8`), far above the point where utilization has gone blind.
        Demand is set by whoever is *queuing* but paid by whoever is *running*, so uncapped it is a
        griefing vector: queue 500 Spawns, let everyone running be billed at the spike, cancel, and pay
        **nothing**. Because Balance drives `nice` for a month, that would let one user bench another for
        a month — a **gate**, which this model does not have. Past ~8× the signal is saturated anyway (the
        card is plainly scarce), while the *billing* consequence kept growing without bound.
*   **Eligibility** — users declare *needs* (`--vram`, Arm **Feature**s), never a machine; only
    the **admin** may name one. A queued Spawn adds demand to **every device it could run on**, spread
    evenly — so scarce big-VRAM cards, wanted by big jobs *and* small ones, price highest.
*   **The Tide** — the *forecast* of demand by hour of day, over a rolling month. It does **not** set
    the price (demand does, live); it is what lets a user plan a cheap overnight run, and it is what an
    escrow is quoted from.
*   **The meter counts seconds *held*, not seconds *busy*.** The scarce good is exclusive access: a
    Spawn squatting a 4090 at 2% utilization has denied it to everyone else regardless. Metering
    utilization would make squatting free. Allocation-pricing already rewards efficiency, because
    finishing sooner costs less.

### Escrow → Settlement
Karma is **reserved** when a Spawn queues and only **moves** when it ends.

*   **Escrow** (at submit) = declared `--time` × devices × Power × the Tide's forecast demand, quoted
    from the **priciest Arm the Spawn could land on** — which is also the one Slurm prefers, since node
    `Weight ∝ 1/Power`. It suppresses the submitter's Share **immediately**. Without it the economy is
    trivially gamed: balances only move at settlement, so a user could queue 500 Spawns and hold
    top-of-queue for hours while nothing had settled. Escrow makes **flooding the queue instantly cost
    you your own priority**.
*   **The hold only ratchets up.** It opens as that forecast, and the **meter** — the running total of
    what the Spawn has really cost so far — raises it if reality overtakes it. Share subtracts
    `max(hold, accrued)`, so a Demand spike is felt in the submitter's standing within 30s rather than
    as a surprise at settlement, and an under-quoted hold cannot be exploited. It never falls before
    settlement: no Spawn can quietly cheapen its own submitter's standing while it runs.
*   **Settlement** (at end) = *actual* seconds held × Power × the demand it actually ran under. It is
    triggered by the Spawn's **absence from the queue**, which is the only end-of-life signal there is:
    a killed Spawn writes no end-time, Slurm forgets jobs after `MinJobAge`, and `sacct` would need the
    slurmdbd we deliberately do not run.
*   Over-declaring `--time` therefore costs **only queue position** (refunded at settle); under-declaring
    gets the Spawn **killed** by Slurm. Honest estimates are self-enforcing from both sides — so
    `--time` is **required, no default**, as is `--vram`.
*   **Early-failure rule:** a failed Spawn simply settles for what it consumed, and is not requeued.
    No special case is needed; it falls out.

### Queue intervention
*   **Singular Run Guarantee:** the economy **never** kills or blocks a running Spawn. It only re-ranks.
*   **Poor standing is a handicap, not a gate:** priority is `priority/multifactor`, and the nice range
    Karma spends equals the **Age** weight — so the worst standing in the cluster costs a Spawn at most
    one `PriorityMaxAge` of *apparent queue age*. Slurm treats it as though it arrived later than it did.
    This is **not** a starvation bound (Age saturates at `PriorityMaxAge`, and a rival can max its own Age
    out too). Starvation is bounded **economically**: spending Karma is what makes you poor, so no User can
    both hog the cluster and stay ahead of the queue.
*   **Admin Override:** admin-only add/remove/**set** of a user's balance. The only flow that isn't a
    transfer (it creates or destroys Karma); Decay absorbs the imbalance.
*   **Queue bump:** admin-only shove of one queued Spawn up the queue, via Slurm priority directly.
    It **bypasses the economy** and touches no balance — implementing a bump as a Karma grant would
    make that user win every *other* race for a month too, corrupting the ledger to solve a scheduling
    problem.

### Scheduler integration — no slurmdbd, no MariaDB
Karma **computes Share itself and sets Slurm priority directly**. It does not use Slurm FairShare, and
therefore needs **no accounting database, no `slurmdbd`, no `sacctmgr`**.

The **Mantle** makes this free. Raising a job's priority needs Slurm operator/root — but *lowering* it
(`Nice`) is the **job owner's** right, and every Spawn runs as `ccc`, so the Head **owns every job in
the queue**. It re-ranks the whole queue by nice with **zero privilege**: no sudo, no root helper, no
SlurmUser. Only relative order matters, so nice ∈ [0, 10000] derived from Share is sufficient.

Slurm also picks the *fastest free eligible* card on its own, via node `Weight ∝ 1/Power` — and will
not hold a job back waiting for a better card if a slower eligible one is idle.

### Retract (Panic Switch)
*   **Purpose:** Lets an Arm's owner claim back their own machine instantly, and hand it back with
    **Return**. Owner-scoped over the Head API from any box; the Admin has an `--any` escape hatch,
    Head-local.
*   **Mechanism: app-layer, not Slurm DRAIN.** Draining a *node* needs Slurm-operator privilege;
    Retract needs none, because every Spawn already runs as the **Mantle**, so ending one is the
    owner's own right. Retract SIGTERMs every Spawn running on the node — Slurm's proctrack cgroup
    delivers it to the whole job cgroup, so the containerized process sees it and the job's exit trap
    still reclaims scratch and stages out what it can. A 180-second grace follows; the Karma tick
    force-kills anything still alive. New placement is steered away with an ordinary `--exclude=` at
    submit time. **Return** only reopens the node for new placement — it does not reprieve a Spawn
    already caught.
*   **Billing: a Retracted Spawn settles at ZERO.** The submitter pays nothing for work someone else
    destroyed, and the owner earns nothing for destroying it. This closes a **Karma farm**: otherwise an
    owner could let Spawns run 55 minutes, Retract, and bank the Karma for work that returned nothing —
    with the victims paying for the privilege, and a merely *careless* owner farming it by accident
    every evening. Reclaiming is paid for by whoever chose to reclaim.
