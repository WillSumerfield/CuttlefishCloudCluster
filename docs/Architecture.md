# Architecture

How CCC is put together, and why it is put together that way. This is the built system — for what is
designed but not built, see [TODO](TODO.md).

## The shape of it

```
        Dashboard (browser)          cuttle (CLI)
                    \                 /
                     \               /            LAN or CCC mesh
                      v             v
        ┌──────────────────────────────────────┐
        │                HEAD                  │
        │  slurmctld · Head API · Karma tick    │
        │  coordinator · auth · head.db        │
        │  stage-in/out · dashboard · SPA      │
        │  + its own co-located Arm            │
        └──────────────────────────────────────┘
                          |
                   the CCC mesh
              (private, encrypted, cluster-only)
             /            |             \
          Arm           Arm            Arm
      slurmd +      slurmd +       slurmd +
      Apptainer     Apptainer      Apptainer
```

**One Head.** It is the scheduler, the coordinator that admits machines to the mesh, the account
store, the economy, the dashboard, and the place projects and results live. It also donates its own
GPU — the Head runs a co-located Arm like any other.

**Many Arms.** Each runs `slurmd` and Apptainer, on Ubuntu 24.04, natively or under WSL2. `slurmd`
and the container runtime always run in the Linux layer, so there is a single Linux code path
everywhere. GPUs are NVIDIA-only, and required: a machine without one is refused at enroll, because
every Spawn holds at least one card. Several cards of the *same* model are welcome — a Spawn can ask
for more than one of them at a time — but a box mixing models cannot advertise one honest VRAM size,
so it is refused too.

**Two clients, at parity.** The [Dashboard](Dashboard.md) is the primary everyday surface; the
[CLI](CLI.md) does everything it does. Neither is a privileged path. The CLI additionally has *host*
duties — bringing a node up and tearing it down — which the Dashboard can never have, because those
provision the box you run them on.

## Networking: the mesh

Every Head and Arm joins one flat encrypted WireGuard network, with automatic NAT traversal, so
`slurmd`↔`slurmctld` and file staging behave like a LAN even when the Arm is behind three routers.
It is self-hosted **Headscale** on the Head with **Tailscale** clients on the nodes.

CCC runs its **own isolated instance** on its own socket, separate from any personal Tailscale on the
box, and deliberately declines three host-global things a second daemon would fight over: DNS,
firewall chains, and the route table. It addresses nodes by mesh IP, so it loses nothing by doing so.

Enrollment is by an Admin-minted one-time token that also names the Arm's owner. Mesh membership
authorizes a **machine** — never a person. See [Mesh](elements/mesh.md).

**The coordinator's address is the bootstrap hop.** An Arm dials it in order to *get* a mesh address,
so it cannot itself be a mesh address. Public reachability — a tunnel, or DDNS plus a real
certificate — is deferred; the Head is assumed reachable over the LAN or the mesh. See
[Set up the Head](setup/head.md#making-the-head-reachable).

## Who is allowed in

Authentication is **built into the Head**: its own user table, argon2 hashes, sessions. There is no
external identity provider, and no sign-up — the Admin provisions accounts.

TLS for the human surface runs under an authority the cluster mints for itself. No public CA will sign
for a box with no public domain name, so the Head is its own: every machine CCC installs on trusts that
root, which is why the Dashboard loads at `ccc.glados` — the cluster's name, carried in `/etc/hosts`
because CCC runs no DNS — without a browser warning. The CLI additionally pins the authority
`known_hosts`-style and accepts no other. A bundle built on a Head carries both, so a client installed
from it is pre-pointed and already trusting the cluster.

**The Head API has no administrative writes.** Every privileged operation — minting tokens, creating
Users, Karma override, queue bump, backup, munge rotation — runs on the Head, as the `ccc` account,
through `cuttle`. Nothing over the network can do them at all. See [Security](Security.md).

## Scheduling: Slurm, and no accounting database

Slurm does the placing. GPUs are Generic Resources (GRES) declared per node from what that node
reported at registration, so `gres.conf` describes hardware that actually exists.

**CCC runs no `slurmdbd` and no MariaDB, and does not use Slurm FairShare.** [Karma](elements/karma.md)
computes standing itself and sets Slurm priority directly, which is the whole reason no accounting
database is needed. `FairShare` stays at 0 throughout.

This is nearly free, and the reason is the Mantle. *Raising* a job's priority needs Slurm operator or
root privilege — but *lowering* it (`Nice`) is the job owner's own right, and every Spawn runs as
`ccc`. The Head therefore **owns every job in the queue** and can re-rank the whole thing by nice with
zero privilege: no sudo, no root helper, no SlurmUser. Only relative order matters.

Priority is `priority/multifactor`, and the nice range Karma spends is set equal to the **Age**
weight. So the worst standing in the cluster costs a Spawn at most one `PriorityMaxAge` of apparent
queue age — Slurm treats it as having arrived later than it did, and never as ineligible. The economy
re-ranks; it never kills or blocks a running Spawn.

Slurm also picks the fastest free eligible card on its own, through node `Weight ∝ 1/Power`, and will
not hold a job back waiting for a better card when a slower eligible one is idle.

## Sandboxing: Apptainer

Pods are **Apptainer** containers. Rootless and daemonless, so a job is a plain Slurm-launched process
with a clean kill; single-file `.sif` images; GPU through `--nv` and `nvidia-container-toolkit`.
Resource caps come from Slurm cgroups on the Linux layer, not from a container daemon.

It requires unprivileged user namespaces on the Arm's Linux layer. It maps host IDs easily and
inherits host node-locked licence profiles, which is what makes machine-bound
[Licences](elements/arm-features.md#licences) work at all.

Every Spawn runs as its submitter's **Guise** — a per-User identity on the machine, enforced by the
OS. Its walls enclose the whole Spawn, including the wrapper that stages files in and out. See
[Isolation](elements/isolation.md).

## Storage: staging, not a shared filesystem

There is **no NFS, no CephFS, no live mount**. Every Arm stages.

A Spawn's project — gitignore-aware — is staged in over an rsync daemon bound to the Head's mesh
address, into node-local scratch. At the end, the declared `$CCC_OUT` directories stage back. Fresh
copy per Spawn; there is no persistent cache yet.

**The last hop is automatic, and it does not stop at the Head.** `cuttle run` records what your box
is owed, and a single detached sweeper brings each Spawn's outputs down into
`~/ccc/outputs/spawn-<id>-outputs/` as it finishes. One predictable place, rather than results
scattered across every directory anyone ever submitted from — and nothing is lost to a reboot, since
the on-disk entry *is* the intent and any later `cuttle` command re-arms a sweeper for it.

The Head is unchanged by this: retention stays a pure function of time and Allowance, so the same
result is still fetchable by hand, from a second machine, for its 7 days. See
[Storage](elements/storage.md).

## Environments

Every Spawn names the file its own tooling already writes — nothing is inferred. The **Head resolves
that declaration** into the commands that build it, at submit; the **Arm the Spawn lands on runs them**,
at job start, inside a Pod, as the submitter's Guise.

An environment **inherits nothing**: the Pod image is Ubuntu plus sshd and CA certificates, with no
Python and no torch in it, so what gets built carries its own interpreter and its own dependencies.
That is what lets a project pin a Python version the cluster has never heard of, and what makes an
environment mean the same thing on every machine.

**Why it is not built centrally.** It was, until a built environment had to reach a remote Arm: 4.6 GB
over a mesh measured at 2.82 MB/s is ~29 minutes, against an enrollment benchmark's 10-minute wall, and
every torch Spawn on that box paid the same. The manifest is kilobytes and every donor's box has its
own link to PyPI, so the declaration travels and the packages do not. Each node keeps a capped package
cache, which is what makes rebuilding per Spawn affordable rather than merely possible.

The cost is real and deliberate: a device IS held while packages download, so the submitter is billed
for it, and a build that fails is a failed Spawn rather than a refused submission. Everything decidable
without building — no declaration, a file that is not there, a node without the tools — is still
refused at submit, before the Spawn exists. See [Environments](elements/environments.md).

## The economy, in one place

Karma exists to make **donating hardware rational**. FairShare cannot pay anyone for supply, so it
cannot grow a cluster; and it gives the operator price, payout and admin levers FairShare does not
have.

The core is one closed flow: every Spawn's cost is **transferred** from its submitter to the owner of
the Arm it ran on. Two properties fall out rather than being mechanisms — the sum of all balances is
always 0, so there is no supply to inflate and a new User joins at exactly average; and running on
your own Arm is free, because you pay yourself. That last one *is* the donation incentive, and it
needs no special rule.

Price is **Power × Demand** — capability and scarcity, measured separately. Power is measured at
enrollment by a benchmark Spawn submitted down the normal Slurm path, inside a Pod, so it sees the GPU
as real Spawns will: WSL's virtualized `/dev/dxg`, a throttled laptop card, a stale driver, an x4
PCIe lane. None of that is on a spec sheet, and all of it is the norm on donated personal machines.
It runs against one pinned torch built once on the Head and pulled by each Arm at enrollment, so two
cards measured a year apart are still comparable — the number is the card, not the software.
Power is not optional: a meter counting only seconds would pay a slow card *more* for the same work,
so donating your worst hardware would pay best.

The meter counts seconds **held**, not seconds **busy** — the scarce good is exclusive access, and a
Spawn squatting a 4090 at 2% utilization has denied it to everyone else regardless. Allocation
pricing already rewards efficiency, because finishing sooner costs less.

Balances, the ledger and demand history are SQLite tables in the Head API's own `head.db`. The API is
already the single writer and already the only thing that knows whose Spawn is whose, so escrow is
atomic with the Spawn insert: no second daemon, no IPC.

Settlement is triggered by a Spawn's **absence from the queue**, which is the only end-of-life signal
available — a killed Spawn writes no end time, Slurm forgets jobs after `MinJobAge`, and `sacct`
would need the `slurmdbd` deliberately not running.

The full user-facing account is in [Karma](elements/karma.md).

## Retract, mechanically

Retract is **app-layer, not Slurm `DRAIN`**. Draining a node needs Slurm-operator privilege; Retract
needs none, because every Spawn already runs as the Mantle, so ending one is the owner's own right.

It SIGTERMs every Spawn on the node — Slurm's proctrack cgroup delivers it to the whole job cgroup, so
the containerized process sees it and the job's exit trap still reclaims scratch and stages out what
it can. A 180-second grace follows, then the Karma tick force-kills anything still alive. New
placement is steered away with an ordinary `--exclude=` at submit time.

A retracted Spawn settles at **zero**, which closes a Karma farm: otherwise an owner could let Spawns
run 55 minutes, retract, and bank the Karma for work that returned nothing — with the victims paying
for the privilege, and a merely careless owner farming it by accident every evening.

## Where the code lives

See [`CONTRIBUTING.md`](../CONTRIBUTING.md#repository-layout).
