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

## Clients (how humans reach the cluster)

Two clients, one **Head**. Both speak to the Head over the network; neither is a privileged path.

- **Dashboard** — the web front door, and the **primary** surface for everyday use: signing in, browsing
  cluster state, submitting **Spawns**, following them, and (for the **Admin**) running the cluster.
  Reached from a browser on the LAN or the **Mesh**.
- **CLI** — one cross-platform tool, the `cuttle` command. Two distinct duties:
  - *Client duties* (from any machine): the scriptable/power alternative to the **Dashboard** — login,
    job submission, and following work.
  - *Host duties* (only on the machine being changed): node bring-up and removal (`setup`/`teardown`).
    These are the one thing the **Dashboard** can never do, because they provision the box you run them
    on.

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

- **Recipe** — a **User**'s saved, re-runnable snapshot of a piece of work: the project code itself,
  plus how to run it (entry point, arguments, declared needs, experiment-tracking config). Private to
  its owner. Because it holds the code, a Recipe can be re-run from any machine with nothing to hand —
  and it is **frozen**: it keeps running what it captured until its owner deliberately updates it.
- **Spawn** — a single traceable execution of a piece of work on the cluster. It may come from a
  **Recipe** (re-running the snapshot) or be submitted ad-hoc (a project uploaded there and then); a
  Recipe is a convenience, not a prerequisite.
- **Tide Pool** — an interactive/testing session: an allocation that attaches a live shell inside a
  **Pod** on an **eligible** Arm, for debugging against real hardware. Bills Karma for as long as it is
  held. Like a **Spawn**, it declares *needs* and is *placed* — a User cannot name the machine
  (see **Eligibility**); it is told which Arm it got.
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

- **Karma** — a User's standing in the cluster: earned by donating hardware, spent by running work.
  Purely **relative** — it ranks Users against each other and *never gates*. There is no floor, no
  bankruptcy, and no balance at which work is refused: a User with little Karma simply loses
  **contention** to Users with more, and loses nothing at all when nobody is competing. Karma is not a
  currency you can run *out* of.
- **Contention** — two or more Users wanting the cluster at once. The only moment Karma has any
  effect: it decides who goes first. An idle cluster is Karma-blind.
- **Balance** — a User's Karma, an unbounded signed score centred on **0** (the default, and where
  every User starts). Positive = owed standing by the cluster; negative = owing it. It is *not* a
  quantity of anything: there is no supply, no pot, and no total to inflate or deflate.
- **Share** — the effect a Balance actually has: a saturating function of it, bounded between "always
  loses **contention**" and "always wins". Hoarding Karma has diminishing returns and debt has
  diminishing punishment — neither end runs away.
- **Transfer** — the core flow: a Spawn's submitter is debited and the **owner** of the Arm it ran on
  is credited *the same Karma*. Running on your own Arm is therefore free. Karma spent on a
  commons/admin-owned Arm (no owner to pay) is simply **destroyed**.
- **Decay** — every Balance drifts back toward 0 over time, absent market forces. Debt is forgiven and
  hoarded wealth evaporates, so standing reflects recent conduct rather than history. Also what makes
  the system self-correcting: any Karma destroyed or granted out of thin air is absorbed automatically.
- **Power** — what a device can *do*: its throughput, **measured** when its Arm enrolls, by running a
  benchmark **inside a Pod** — so it reflects the machine as Spawns will really meet it (a virtualized
  WSL GPU, a throttled laptop card, a stale driver), not what its spec sheet claims. Intrinsic and
  fixed. It is what stops the meter paying an owner *more* for being *slower*: a second is worth only
  what a second on that card actually buys.
- **Demand** — how badly a device is *wanted*: the device-time being **asked for** (running *and*
  queued) against the device-time that exists. A GPU busy an hour with someone waiting behind it the
  whole hour was demanded for *two* hours. Extrinsic and live. Unlike utilization it does not
  saturate — it keeps discriminating exactly when the cluster is most **contended**.
- **Eligibility** — the set of devices a Spawn may run on, derived from what it *declares it needs*
  (VRAM; hardware licences later) rather than which machine it wants. A queued Spawn adds **Demand**
  to every device it is eligible for — so scarce big-VRAM cards, wanted by big jobs *and* small ones,
  price highest. Users declare needs; only the **Admin** may name a machine outright.
- **The Tide** — the **forecast** of Demand: what an hour on a given device has typically cost, by
  hour of day. **High Tide** = the busy hours, **Low Tide** = the quiet ones. The Tide does not *set*
  the price (Demand does, live) — it lets a User plan around it, and it is what the **Escrow** is
  quoted from.
- **Escrow** — Karma reserved against a Balance the moment a Spawn is queued, quoted from the **Tide**
  and its declared duration. It suppresses the submitter's **Share** while the Spawn is outstanding —
  so flooding the queue immediately costs you your own queue position — and is released at
  **Settlement**. Escrow is a hold, not a charge.
- **Settlement** — the single moment a Spawn's Karma actually moves, when it ends: the **Escrow** is
  released and the real cost is **transferred** to the Arm's owner. The cost of a device is the
  seconds it was *held* (not the seconds it was *busy* — you paid for exclusive access) × its
  **Power** × the **Demand** it ran under. Several devices are several separate purchases.
  Over-reserving therefore costs a User nothing but queue position; under-reserving costs them
  nothing at all.
- **Retract** — a volatile Arm owner's instant reclaim of their machine (the panic-switch);
  drains the node and gives running work a short grace period to checkpoint and exit. A Spawn a
  Retract destroys **settles at zero** — its submitter pays nothing for work someone else killed, and
  the owner earns nothing for having killed it. Volatility is paid for by whoever chose it.
- **Early-failure rule** — a Spawn that fails is **settled, not requeued**: it pays for what it
  actually consumed and does not silently run again.
- **Cancel** — a **User** ending their own outstanding **Spawn** early (an **Admin** may cancel anyone's).
  Like any other ending it **settles** for the seconds it actually held — the submitter chose to stop, so
  the submitter pays. Distinct from **Retract**, which is the *Arm owner* reclaiming a machine out from
  under work that did not choose to die, and is therefore the only ending that settles at **zero**. An
  Arm's owner has no power to kill a single Spawn on their Arm; they have **Retract**.
- **Override** — an admin-only operation that adds/removes/sets a User's **Balance** directly. The only
  flow that isn't a **Transfer** — it creates or destroys Karma, and **Decay** absorbs the imbalance.
  Changes *standing*: persistent, and felt in every future **contention**.
- **Queue bump** — an admin-only shove of one queued **Spawn** up the queue, *without* touching anyone's
  Karma. Deliberately **bypasses** the economy rather than participating in it: it moves one job, once,
  and leaves no trace in the ledger. Distinct from **Override**, which would buy the same result by
  permanently distorting a User's standing.
