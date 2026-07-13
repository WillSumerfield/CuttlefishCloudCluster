# CCC — Root Glossary

> **Cuttlefish Cloud Cluster (CCC).** Themed cuttlefish names are the *canonical* terminology
> (CLI, code, docs). Names still being locked in — those below marked *(candidate)* are not final.
> This file defines the *concepts* and the repo's bounded contexts.
> `CONTEXT.md` is a glossary only — no implementation details, decisions, or specs.
> Per-context glossaries live in each top-level context dir (e.g. `karma/CONTEXT.md`).

## Deployment roles (things that run on a machine)

- **Head** — the cluster control plane. One logical role. Runs the scheduler/control plane and
  hosts the head-side of other domains (Karma, telemetry aggregation).
- **Arm** — a compute node that donates resources to the cluster. Single role with a
  **volatility** flag (`--volatile`):
  - *permanent* (default) — always-on, never reclaims.
  - *volatile* — reclaimable at any time by its owner; adds preemption + the reclaim panic-switch.
  Arms may run on **any OS** (Linux/Windows/Mac/other).
- **Gateway** — the authenticated network entryway that fronts all access to the cluster.
  Distinct from the **Mesh**: the Gateway fronts human/CLI access, the Mesh is node-to-node.

## Network concepts

- **Mesh** — the cluster's private encrypted network. Every Head and Arm is a member, and members
  address each other only by their **mesh address**. Each cluster has exactly one Mesh; two
  clusters' Meshes never see each other.
- **Coordinator** — the Head-hosted service that admits machines to the Mesh and assigns each one a
  stable mesh address.
- **Enrollment** — a machine becoming an Arm of a cluster: authorized by the **Admin**, it joins the
  Mesh, then registers with the scheduler.
- **Registration** — an Arm's node agent announcing its own hardware to the scheduler at runtime,
  rather than being declared in advance by the Head.
- **Enrollment token** — a one-time, single-Arm, short-lived credential the **Admin** mints to
  authorize one machine's enrollment. It also names the Arm's **owner** (a User, chosen by the Admin at
  mint), so the donor redeems it and declares nothing while the Head **auto-accepts** (no manual
  approval). It is the only thing that admits a new Arm; the Admin's own password never leaves the
  Head. (Supersedes the earlier public-invite + two-gate-approval model.)

## Front-end tool

- **CLI** — one cross-platform tool, the `cuttle` command, that is the single front door for humans:
  - node bring-up (`setup`, choosing role + flags; dispatches to per-context provisioning),
  - job submission and interactive/testing sessions,
  - recipe management.
  Communicates with the **Head** over the network (CLI → Head messaging).

## People & access

- **User** — a person with an account on the Head (admin-provisioned: username + password, argon2
  hash). A User's identity ties every Spawn they submit to them, marks any Arms they **own** (donate),
  and anchors their Karma wallet.
- **Admin / Maintainer** — the privileged User (the operator) who provisions accounts, mints
  **enrollment tokens** (naming each Arm's owner), and can **override** Karma. One person today.
- **Session** — a User's authenticated login (a token) carrying the authority to submit jobs and read
  their own data. Distinct from **Mesh** membership, which authorizes *machines*, not people.

## Domains (bounded contexts that may deploy across roles)

- **Karma** — the token economy. Deploys on the Head. Covers wallets/balances, dynamic
  pricing, congestion effects, fair-share synchronisation, and the admin **override** (privileged
  add/remove of Karma).
- **Telemetry** — monitoring & alerting. Spans Head + Arm: node-side exporters and
  rogue-actor/out-of-scheduler detection, head-side aggregation, experiment logging injection, and
  user-facing alerts/webhooks.

## Job/experiment concepts

- **Recipe** — a saved, reusable project configuration: location, detected
  dependencies, user config, and baseline hardware requirements.
- **Spawn** — a single traceable execution of a Recipe with specific hyperparameters.
- **Tide Pool** — an interactive/testing session: an allocation that attaches a live shell inside
  the job Pod on a chosen node, for debugging against that node's specific hardware. Bills Karma
  while attached.
- **Pod** — the isolated environment a job runs inside on an Arm.
- **Mantle** — the single service identity every Spawn *executes as*, identically on every node of
  the cluster. It is not the submitter: the cluster has no per-**User** identity on its nodes, so
  every Spawn — whoever submitted it — runs as the Mantle. Distinct from **Pod**: the Pod is *what a
  Spawn runs inside* (its isolated environment), the Mantle is *who it runs as*.
- **Attribution** — the Head's binding of each Spawn to the **User** who submitted it. Because Spawns
  all execute as the **Mantle**, this is the *only* link between a Spawn and a person — the thing that
  answers "whose Spawn is this?" for the queue, the Dashboard, and (later) Karma billing.
- **Shared storage** — the unified cluster-wide filesystem (home dirs, code, base envs) mounted
  identically on every node. Distinct from the `shared/` code context.

## Economy concepts

- **Karma** — the circulating token users spend to run work and earn by donating hardware.
- **Retract** — a volatile Arm owner's instant reclaim of their machine (the panic-switch);
  drains the node and gives running work a short grace period to checkpoint and exit.
- **Early-failure rule** — a Spawn that fails within one hour of starting still spends Karma but is
  treated as done (not auto-requeued).
- **Override** — an admin-only operation that directly adds/removes Karma from a user.
- **The Tide** — dynamic hourly pricing. **High Tide** = peak-demand hours (costs multiplied);
  **Low Tide** = off-peak (costs at baseline minimum).
- **Congestion Penalty** — extra Karma burn that scales up when users are queued or Spawns are
  throttled below their preferred resources.
