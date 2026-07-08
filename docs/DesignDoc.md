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
*   **Purpose:** Zero-trust, authenticated network entryway protecting the cluster from hackers and enforcing Imperial College Single Sign-On (SSO) or Multi-Factor Authentication (MFA). Required for all usage.
*   **Tech Stack (locked):** **WireGuard mesh** (Tailscale, or self-hosted **Headscale**) as the
    cluster fabric: every node — Head and all Arms, including remote/NAT'd volatile desktops — joins
    one flat encrypted network with automatic NAT traversal, so `slurmd`↔`slurmctld` and storage
    mounts behave like a LAN. Node enrolment and human access gated by Imperial SSO/OIDC (an
    SSO/MFA auth layer, optionally Teleport, may sit on top for user-facing access).

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
*   **Open sub-topic:** exact stage-in/stage-out mechanics on Spawn start/stop — what gets copied,
    how inputs/outputs are declared, dedup/caching, and partial-result handling on Retract.

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
    *   Web dashboard (Open OnDemand) optional/deferred.

### Interactive / Testing Mode (Tide Pool)
*   **Purpose:** Lets a user drop directly into a live Pod on an allocated Arm to run code interactively, debug, and probe hardware behaviour before committing a full Spawn. Motivated by heterogeneous hardware not behaving identically (differing GPU archs, driver versions, node-locked licences) — a run that works on one node may fail on another.
*   **Behaviour:** Allocates resources like a normal Spawn but attaches an interactive shell inside the Pod (container) instead of executing a batch script. Bills Karma while attached.

---

## 4. Telemetry

### System Logging & Metric Integration
*   **Purpose:** Captures live training statistics, flags code errors, and measures hardware health (power usage, temperature, VRAM) over time per individual Spawn.
*   **Tech Stack:** **Weights & Biases (W&B) Core API Engine**. Environment variables (`WANDB_API_KEY`, `WANDB_RUN_GROUP`) are automatically injected by `cuttle` into the sandboxed container at runtime.

### Configurable Warnings & Webhooks
*   **Purpose:** Pings the user immediately via email, Slack, or Discord if code throws a `NaN` error, freezes, or if a volatile Arm is forcefully retracted.
*   **Tech Stack:** W&B Native Alerts combined with Slurm `Prolog`/`Epilog` hooks running automated `curl` webhook payloads.

### Rogue Actor & Bad Use Detection
*   **Purpose:** Tracks and monitors hardware utilization occurring *outside* of Slurm's scheduler to detect background hackers, un-logged local processes, or crypto-miners.
*   **Tech Stack:** Prometheus Node Exporter + Custom Python Cron Daemon. If physical CPU/GPU activity exceeds 10% while Slurm records 0 active jobs on that specific node, an immediate intrusion alert is fired to the lab administrators.

---

## 5. The Karma Economy

### The Equilibrium & Fair-Share Rule
The economy operates as a closed, proportional token market attempting to keep equal Karma across active users. Total circulating Karma (\(K_{total}\)) scales dynamically when new users join by minting a standard baseline quota (\(K_{base}\)) and normalizing old balances to avoid market deflation. 

*   **Persistence (locked):** wallets, balances, the transaction ledger, and pricing history live in a
    **SQLite** DB on the Head — simplest ops, sufficient at research scale. Caveat: SQLite is
    writer-serialized, so run in **WAL mode** and funnel all economy writes through a **single
    process** to avoid lock contention and double-spend.

### Token Flows
*   **Passive Host Income:** Volatile Arm owners passively earn Karma hourly based on hardware benchmarks. They get bonus passive income if someone else is actively running a job on their machine.
*   **Active Rental Surcharge:** If an external researcher borrows an idle Arm, the owner receives a direct surcharge tax drained from the borrower's wallet based on hardware speed, size, and execution duration.
*   **The Tide (Dynamic Hourly Pricing):** High Traffic hours (*High Tide*) multiply the hourly baseline resource costs based on moving averages of historic demand. Night runs (*Low Tide*) drop costs to the baseline minimum.
*   **The Congestion Penalty:** If users are waiting in the queue, or if active Spawns are currently being throttled below their preferred resource limits, the Karma burn-rate scales up exponentially.

### Queue Intervention & Phased Pausing
*   **Singular Run Guarantee:** Active, single-phase executions are **never** violently terminated via the economy layer to avoid code corruption. Instead, low Karma balances push the user's overall Slurm FairShare metric to zero via periodic hourly syncs (`sacctmgr modify user set RawUsage=X`), ensuring automatic balance of resources across all active experiments.
*   **Multi-Phase/Sweep Hops:** For multi-run Spawns (like W&B Sweeps), sequential stages are tied together using Slurm dependencies. Before a subsequent phase triggers, a validation script verifies balances. If a user is bankrupt, the next stage is automatically placed on an administrative `HOLD` until passive regeneration restores their account.
*   **Early-Failure Rule:** If a Spawn fails within one hour of starting, the user's Karma is **still** spent (balance decreases for the resources consumed), but the Spawn is treated as `done` and is **not** automatically requeued/retried.

### Admin Override (`override`)
*   **Purpose:** A privileged command, executable **only by admin users**, that directly adds or removes Karma from an individual's wallet — for corrections, grants, or penalties outside the normal economy flows.

### Retract (Panic Switch)
*   **Purpose:** A physical macro or desktop terminal script allowing a volatile Arm's true owner to claim back their machine instantly.
*   **Tech Stack Execution:** Triggers `scontrol update NodeName=Arm-X State=DRAIN Reason="Retract Engagement"`. Slurm halts incoming jobs and fires a `SIGUSR1` termination signal to any active remote runner, giving the container a configurable 60 seconds to save weights to Shared Storage and exit gracefully.