# Cuttlefish Cloud Cluster (CCC) Design Specification

## 1. System Architecture & Node Taxonomy
The CCC is a distributed GPU/CPU computing network serving both on-campus and remote lab members at Imperial College London.

> **Cross-platform strategy — Linux substrate everywhere.** Arms may be started on any OS, but the
> execution environment is always Linux. `cuttle setup` provisions that Linux layer under the hood:
> native on Linux, **WSL2** on Windows, a lightweight **Linux VM** (e.g. Lima/UTM) on macOS. `slurmd`
> and the container runtime always run inside that Linux layer, so there is a single Linux code path
> everywhere. Consequence: macOS Arms have **no NVIDIA/CUDA** access (no GPU passthrough to the VM;
> Apple Silicon is Metal-only) → Mac Arms are effectively CPU-only.

### The Head (Control Plane)
*   **Purpose:** Central control plane running the cluster scheduler orchestrating resource allocations, tracking active jobs, and maintaining queue fairness.
*   **Tech Stack:** Slurm Workload Manager (`slurmctld`), plus **`slurmdbd` + MariaDB** for accounting
    and FairShare (required by the Karma↔FairShare sync). Head-side only, Linux.

### Permanent Arms (Dedicated Server Nodes)
*   **Purpose:** High-availability, permanently attached multi-GPU servers for heavy, non-volatile compute workloads.
*   **Tech Stack:** Enterprise Ubuntu Server LTS, NVIDIA Data Center GPU Manager (DCGM).

### Volatile Arms (Edge Nodes)
*   **Purpose:** Desktop computers or physical robot controllers used locally that donate spare compute to the cluster but can instantly reclaim 100% of their resources.
*   **Tech Stack:** Slurm Preemption Daemon configured with `PreemptMode=REQUEUE` or `SUSPEND`.

### The Gateway (Secure Access)
*   **Purpose:** Zero-trust, authenticated network entryway protecting the cluster from hackers and enforcing **built-in Head authentication** (admin-provisioned username/password; MFA possible later). Required for all human usage.
*   **Tech Stack (locked):** **WireGuard mesh** (Tailscale, or self-hosted **Headscale**) as the
    cluster fabric: every node — Head and all Arms, including remote/NAT'd volatile desktops — joins
    one flat encrypted network with automatic NAT traversal, so `slurmd`↔`slurmctld` and storage
    mounts behave like a LAN. **Node enrolment** is authorized by an Admin-minted one-time token (which
    also names the Arm's owner); **human access + job submission** are gated by **built-in auth on the
    Head** (own user table, argon2 + sessions) — *not* Imperial SSO. TLS for the human surface is
    terminated by a tunnel (Cloudflare Tunnel / `tailscale funnel`) — a real cert without managed DNS.

---

## 2. Resource, Sandbox, & Licence Management

### Partitioning & Heterogeneous Scheduling
*   **Purpose:** Separates CPU and GPU resources so they can be requested independently, bundled, or combined across multiple distinct node types. Handles overallocation gracefully.
*   **Tech Stack:** Slurm Generic Resource (GRES) scheduling configured via `slurm.conf`. GPU
    support is **NVIDIA-only** (native Linux driver, or Windows via WSL2 CUDA passthrough, both wired
    through `nvidia-container-toolkit` + Apptainer `--nv`). AMD/Apple machines may join as CPU-only
    Arms.

### Pods (Isolated Environments)
*   **Purpose:** Sandboxes jobs to prevent overallocation crashes, stops users from erasing external host memory, and preserves node-locked software profiles.
*   **Tech Stack (locked):** **Apptainer** (formerly Singularity). Rootless and daemonless, so the job
    runs as a plain Slurm-launched process with a clean kill on Retract; maps host IDs easily and
    inherits host node-locked licence profiles; single-file `.sif` images; GPU via `--nv`. Requires
    unprivileged user namespaces on the Arm's Linux layer (native/WSL2/VM). Resource caps enforced
    via Slurm `cgroups` on the Linux layer, not a Docker daemon.

### Hardware Licence Management
*   **Purpose:** Routes jobs requiring node-locked software licences to the exact physical hardware they are registered to, preventing code compilation crashes.
*   **Tech Stack:** Slurm Node Features (`Features=matlab_lic` inside `slurm.conf`). Jobs query via `--constraint="matlab_lic"`. For containers, Apptainer automatically passes the true host hardware ID/MAC address to bypass network isolation blocks.

### Shared Storage (Unified Storage)
*   **Purpose:** Ensures all user directories, code files, and base environment snapshots are identical across every Arm on the network.
*   **Tech Stack (locked — tiered):** A central store (NFS or CephFS) is the single source of truth.
    *   **Permanent / LAN Arms:** mount it directly at `/mnt/ccc_share/` (uniform live view, classic HPC).
    *   **Remote / volatile Arms:** **no live mount.** `cuttle` **stages in** the job's inputs to local
        disk at start and **stages out** results/checkpoints at end (rsync / object pull over the mesh).
        Robust to disconnects and WAN latency; Retract checkpoints to local disk then best-effort pushes.
*   **Resolved (Phase 1 ④):** the gitignore-aware project dir is staged in over an rsync daemon bound
    to the Head's mesh address, into node-local scratch, then a designated `$CCC_OUT`/`outputs` dir is
    staged back at the end. Per-Spawn, fresh copy (no persistent cache yet). Still open: dedup/caching
    for sweeps, live log streaming, and partial-result handling on Retract.

---

## 3. Automation, Recipes, & Experiments

### Recipe (Saved Project Config)
*   **Purpose:** The saved metadata structure mapping a project's location, its auto-detected software dependencies, its user configuration, and its baseline hardware requirements.

### Spawns (Active Experiments)
*   **Purpose:** A single, traceable execution of a Recipe passing unique hyperparameters (`config.yaml`) into the container.

### `cuttle` Auto-Detection Layer
*   **Purpose:** A lightweight, user-facing TUI/GUI and Python CLI that abstracts all Slurm syntax, allowing users to save full configuration "Recipes".
*   *Static Code Analysis:* Automatically reads the target script. If strings like `cuda` or `.to('cuda')` are found, it auto-allocates 1 GPU. If `DistributedDataParallel` is found, it enforces a single-node Multi-GPU safety cap to block slow inter-network routing across individual Arms. Users can specify fixed max memory limits or leave it dynamic under a safety cap.
*   *Environment Auto-Updates:* Hashes the project's `requirements.txt` or `environment.yml` on launch. If a hash mismatch is detected, it triggers a quiet update script inside the user's persistent storage slice before runtime to update the environment.
*   **Tech Stack (locked — split):** The `cuttle` tool is two layers matching where code runs:
    *   **Host-native launcher** — a single static **Go** binary installed on
        the raw OS with no prereqs. Bootstraps the Linux layer (WSL2/VM), joins the mesh, registers the
        Retract hotkey, and drives stage-in/stage-out.
    *   **Python core** — recipes, job submit, static-analysis auto-detect, and W&B injection (`typer`),
        running inside the Linux layer where Python is trivial.
    *   Web dashboard — a read-only React/Vite SPA over a FastAPI Head API (live node + Spawn state),
        served on the CCC mesh + LAN; grows submission/management in later phases. (Open OnDemand was
        considered and dropped in favour of a purpose-built SPA.)

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

### Configurable Warnings & Webhooks
*   **Purpose:** Pings the user immediately via email, Slack, or Discord if code throws a `NaN` error, freezes, or if a volatile Arm is forcefully retracted.
*   **Tech Stack:** W&B Native Alerts combined with Slurm `Prolog`/`Epilog` hooks running automated `curl` webhook payloads.

### Rogue Actor & Bad Use Detection
*   **Purpose:** Tracks and monitors hardware utilization occurring *outside* of Slurm's scheduler to detect background hackers, un-logged local processes, or crypto-miners.
*   **Tech Stack:** Prometheus Node Exporter + Custom Python Cron Daemon. If physical CPU/GPU activity exceeds 10% while Slurm records 0 active jobs on that specific node, an immediate intrusion alert is fired to the lab administrators.

---

## 5. The Karma Economy
*Rewritten 2026-07-14 (grill). Karma exists to make **donating hardware rational** — FairShare cannot
pay people for supply, so it cannot grow a cluster — and to give the operator price/payout/admin levers
FairShare doesn't have. The previous design (a closed token market with minting, bankruptcy, and three
separate demand multipliers) is superseded; see "What was deleted" at the end.*

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
*   **Decay** — balances drift back to 0 with a **one-month** time constant. Debt is forgiven, hoards
    evaporate, and standing tracks *recent* conduct.
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
    used for an hour with someone queued behind it the whole time was demanded for **two**. Unlike
    utilization this **does not saturate** — utilization reads ~100% on every card the moment the
    cluster is busy, and so stops discriminating exactly when price matters most.
*   **Eligibility** — users declare *needs* (`--vram`; hardware licences later), never a machine; only
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

*   **Escrow** (at submit) = declared `--time` × devices × Power × the Tide's forecast demand. It
    suppresses the submitter's Share **immediately**. Without it the economy is trivially gamed:
    balances only move at settlement, so a user could queue 500 Spawns and hold top-of-queue for hours
    while nothing had settled. Escrow makes **flooding the queue instantly cost you your own priority**.
*   **Settlement** (at end) = *actual* seconds held × Power × the demand it actually ran under.
*   Over-declaring `--time` therefore costs **only queue position** (refunded at settle); under-declaring
    gets the Spawn **killed** by Slurm. Honest estimates are self-enforcing from both sides — so
    `--time` is **required, no default**, as is `--vram`.
*   **Early-failure rule:** a failed Spawn simply settles for what it consumed, and is not requeued.
    No special case is needed; it falls out.

### Queue intervention
*   **Singular Run Guarantee:** the economy **never** kills or blocks a running Spawn. It only re-ranks.
*   **Starvation is impossible by construction:** priority is `priority/multifactor`, so the **Age**
    factor keeps accruing. The worst-Karma user loses every race — and eventually runs anyway.
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
*   **Purpose:** A physical macro or desktop terminal script allowing a volatile Arm's true owner to claim back their machine instantly.
*   **Tech Stack Execution:** Triggers `scontrol update NodeName=Arm-X State=DRAIN Reason="Retract Engagement"`. Slurm halts incoming jobs and fires a `SIGUSR1` termination signal to any active remote runner, giving the container a configurable 60 seconds to save weights to Shared Storage and exit gracefully.
*   **Billing: a Retracted Spawn settles at ZERO.** The submitter pays nothing for work someone else
    destroyed, and the owner earns nothing for destroying it. This closes a **Karma farm**: otherwise an
    owner could let Spawns run 55 minutes, Retract, and bank the Karma for work that returned nothing —
    with the victims paying for the privilege, and a merely *careless* owner farming it by accident
    every evening. Volatility is now paid for by whoever chose it.

### What was deleted (and why)
*   **Minting / `K_total` / `K_base` / "normalize to avoid market deflation"** — solves a problem that
    cannot occur. Transfers + a zero start pin the mean at 0 by construction; there is no supply.
*   **Bankruptcy and the bankrupt-sweep `HOLD`** — the one mechanism that implied an absolute zero, in a
    system where Karma is purely relative. Karma never gates.
*   **The Congestion Penalty**, and **the Tide as a price multiplier** — both were measuring the same
    thing as demand. Keeping all three would have multiplied one congestion event into a bill three
    times, through three separately-tuned knobs.
*   **FairShare sync (`sacctmgr ... RawUsage`), slurmdbd, MariaDB** — laundering a priority we already
    know through an accounting DB, with RawUsage's own decay fighting ours.
*   **Passive/idle host income** — owners earn only when their machine is *used*, so Karma is minted
    against real work and an idle cluster mints nothing. (Trade accepted: donate a GPU nobody schedules
    and you earn nothing from it.)