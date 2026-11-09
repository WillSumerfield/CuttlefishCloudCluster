# CCC — Root Glossary

> **Cuttlefish Cloud Cluster (CCC).** Themed cuttlefish names are the *canonical* terminology
> (CLI, code, docs). This file defines the *concepts* and the repo's bounded contexts.
> `CONTEXT.md` is a glossary only — no implementation details, decisions, or specs.
> Per-context glossaries live in each top-level context dir (e.g. `karma/CONTEXT.md`).

## Deployment roles (things that run on a machine)

- **Head** — the cluster control plane. One logical role. Runs the scheduler and control plane, holds
  every User's **Tidepool**, serves the **Dashboard** and the **Wiki**, authenticates people
  (**Sign-on**), and hosts the head-side of other domains (Karma, telemetry). It also lends its own
  GPUs, so it is an **Arm** as well.
- **Arm** — a compute node that donates resources to the cluster. One role, with no permanent/
  reclaimable distinction to declare: every Arm is its owner's to reclaim at any moment, through
  **Retract**, so there is no kind of Arm that is more lent than another. Arms run on
  native **Ubuntu 24.04**.

## Network concepts

- **Mesh** — the cluster's private encrypted network. Every Head and Arm is a member, and members
  address each other only by their **mesh address**. Each cluster has exactly one Mesh; two
  clusters' Meshes never see each other. Users' machines are never on it: they reach the Head
  directly.
- **Coordinator** — the Head-hosted service that admits machines to the Mesh and assigns each one a
  stable mesh address.
- **Enrollment** — a machine becoming an Arm of a cluster: authorized by the **Admin**, it joins the
  Mesh, then registers with the scheduler.
- **Registration** — an Arm's node agent announcing its own hardware to the scheduler at runtime,
  rather than being declared in advance by the Head.
- **Cluster name** — the one name a machine uses to reach the **Head**, in place of an address, chosen when the Head is set up. It is
  not a public domain and nothing outside the cluster resolves it: a machine has the name only because
  something put it there, normally CCC installing there. The name means whichever address that machine
  can actually reach the Head on, so a **Client** on the LAN and an Arm on the **Mesh** dial the same
  name and land on the same Head. Where CCC is installed inside a virtual machine, the OS around it is
  a separate machine for this purpose: it holds the name only if its owner puts it there, and on some
  platforms it cannot hold it at all. Anything without the name reaches the **Dashboard** by address
  instead.
- **Enrollment token** — a one-time, single-Arm, short-lived token the **Admin** mints to
  authorize one machine's enrollment. It also names the Arm's **owner** (a User, chosen by the Admin at
  mint), so the donor redeems it and declares nothing while the Head **auto-accepts** (no manual
  approval). It is the only thing that admits a new Arm; the Admin's own password never leaves the
  Head.

## Clients (how humans reach the cluster)

Two clients, one **Head**. Both speak to the Head over the network; neither is a privileged path.

- **Dashboard** — the web front door, and the **primary** surface for everyday use: signing in, browsing
  cluster state, starting **Dives**, following them, and (for the **Admin**) watching the cluster.
  Reached from a browser on the LAN or the **Mesh**. It has no admin *writes*: every privileged act is
  a `cuttle` command on the Head.
- **CLI** — one tool, the `cuttle` command (Python; usable on a client that hosts no Arm). Two distinct duties:
  - *Client duties* (from any machine): the alternative to the **Dashboard**, at operation **parity**
    with it — login, starting Dives, and following work. Scriptable throughout; `setup` is additionally
    offered as a guided **wizard**. Parity covers what can be asked of the *cluster*, not what each
    client can do to the machine it runs on: it is the CLI that arranges the machine it runs on so a program already
    installed there — an editor, most obviously — can reach a live **Tidepool** or **Dive**; a browser can
    describe that session but cannot hand it to anything outside itself.
  - *Host duties* (only on the machine being changed): node bring-up and removal (`setup`/`teardown`).
    These are the one thing the **Dashboard** can never do, because they provision the box you run them
    on.

## People & access

- **User** — a person with an account on the Head (admin-provisioned: username + password, argon2
  hash). A User's identity ties every Dive they start to them, marks any Arms they **own** (donate),
  and anchors their Karma wallet. Each User has exactly one **Tidepool**.
- **Admin / Maintainer** — the privileged User (the operator) who provisions accounts, mints
  **enrollment tokens** (naming each Arm's owner), and can **override** Karma. One person today.
  The role's authority has two sources and they are not interchangeable: the **account**, which is what
  a **Session** carries and which grants no power over anyone else's work; and the **Head** itself, where
  being present at the machine is the authorization for every privileged act. An Admin away from the Head
  is an ordinary **User** with a title.
  The Admin is **outside the economy** — a role for *running* the cluster, not for *using* it. They
  earn no **Karma** (an Arm they own is a **Commons Arm**, and its Karma is destroyed), pay none, and
  hold no **Balance**: their **Dives** are never **Escrow**ed, never **Settled**, and never lose
  **contention**. A person who wants to use the cluster does so as an ordinary **User**, with an
  ordinary account.
- **Sign-on** — the Head's built-in username/password authentication (its own user table, argon2
  hashes, **Sessions**) that gates human access and Dive submission. There is no external identity
  provider. Distinct from the **Mesh**, which authorizes *machines*, not people.
- **Session** — a User's authenticated login carrying the authority to start Dives and read their own
  data. It is the **Head**'s own record of that login; the token a client holds is only the User's copy
  of it. So a Session ends when the Head is told to end it, or when its own lifetime runs out — never
  merely because a client discarded its copy, which makes the Session unreachable *from that box* while
  leaving it live everywhere else. A Session opened *inside* a Tidepool or Dive is **ambient**: never
  Admin, and able only to read and to start/end that User's own Dives. Distinct from **Mesh**
  membership, which authorizes *machines*, not people.

## Domains (bounded contexts that may deploy across roles)

- **Karma** — the token economy. Deploys on the Head. Covers wallets/balances, dynamic
  pricing, congestion effects, and the admin **override** (privileged add/remove of Karma). Sets
  Slurm priority directly — no FairShare, no accounting database.
- **Telemetry** — monitoring & alerting. Deploys on the Head: rogue-actor/out-of-scheduler detection
  on the Head's own machine, with operator email alerting.

## Tidepool & Dive concepts

- **Tidepool** — a User's own space on the **Head**: exactly one per User, the same fixed size for
  everyone, plus an always-running CPU-only **Pod** (no GPU) the User can shell into at any time. Its
  `$HOME` is the User's whole filesystem on the cluster — code, data, results, dotfiles, caches — and
  the only place a User's files live. The User has root inside it (so `apt install` works) but no power
  over the Head or anyone else's Tidepool. GPU work is a **Dive** started from it. A Tidepool keeps
  running across disconnects, and a full one simply fails writes (ENOSPC) for that User alone.
  (1.x called an interactive session a "Tide Pool", two words; that meaning is gone.)
- **Dive** — a single GPU session on the cluster: it stages a User's **main folder** and any **dep
  folders** onto an **eligible** Arm (or the Head's own co-located Arm), builds its **Environment** if
  one is declared, and attaches a live shell inside a **Pod**. It bills **Karma** for as long as it is
  held. Like all work it declares *needs* and is *placed* — a User cannot name the machine (see
  **Eligibility**); it is told which it got. A Dive runs **once**: if the machine it was placed on
  fails, or its **Arm** is **Retract**ed underneath it, the Dive fails with it rather than quietly
  starting over somewhere else — so "this Dive" always names one attempt, never a series, and what it
  was billed for and what it produced both refer to the same single run. Its **main folder** is
  mirrored home continuously; nothing else it writes on the node survives it. Users meet the shell
  through the **CLI**'s own terminal or by pointing an editor already running on their machine at it —
  neither is more real than the other, since the work happens inside the **Pod** either way.
- **Main folder** — the one **Tidepool** folder a Dive works in. It is copied to the node, mirrored
  back to the Tidepool continuously and once more at the end, and **read-only in the Tidepool** while
  the Dive holds it, so there is only ever one writer. Only one Dive may hold a given main folder at a
  time.
- **Dep folder** — an extra **Tidepool** folder staged into a Dive as a **read-only** input. Nothing
  is mirrored back from a dep folder, it is not locked, and several Dives can use the same one at once.
- **Mantle** — the cluster's *own* identity: who the **Head** is, to itself and to every **Arm**. It
  owns what the cluster owns — the account records, the Head's key and every Dive's bookkeeping — and
  it is what the Head acts as when it does work of its own rather than a User's. It is nobody's: no User
  is ever the Mantle, and nothing a User runs runs as it.
- **Guise** — the identity a **Dive** runs as on the machine that runs it. One per **User**, the same
  one wherever the cluster places their work, and taken on by a machine the first time that User's work
  lands there. It is what stops one User's Dive from reading, killing or interfering with another's —
  a boundary the operating system enforces rather than one the cluster merely agrees to. A Guise is
  *not* an account a person can log into: it authenticates nobody, carries no authority, and is
  unrelated to a User's **Session**. Distinct from **Pod** (*what* a Dive runs inside), from the
  **Mantle** (who the *cluster* is), and from **Attribution** (the Head's own record of whose it is).
  It is not confined to any one machine: anything the cluster does on a User's behalf that involves
  *their* code — including building their **Environment**, on the **Arm** that will run it — is done as
  their Guise, never as the **Mantle**. A User's code has no way to be the cluster, on any machine.
- **Pod** — the isolated *place* a job runs inside, on whichever machine is running it (a rootful
  Podman container with its own unprivileged ID range). Its walls are real: a Dive cannot see the
  host's filesystem, the machine's other processes, or any concurrent Dive's working files. A Pod is
  the *walls*, never the contents. What is installed within one is that Dive's **Environment** and
  whatever else it goes on to install — every Pod's own system is writable, and no Pod keeps what it
  holds. A **Tidepool** is a long-lived Pod; a **Dive** is a Pod on the node it was placed on. Work the
  **Head** does on a User's behalf is walled the same way, as the same **Guise**.
- **Environment** — the Python a **Dive**'s code actually runs against inside its **Pod**. It is
  **self-contained**: it carries its own interpreter and inherits nothing from the machine or from the
  Pod's image, so the same declaration runs the same way everywhere. A **User** declares one by naming
  the dependency file their own tooling already writes (`--env`) — CCC never guesses which file
  describes the work, because a dependency file in a repo is not always the one that does. Naming none
  leaves bare Python (the base image carries no Python packages); a **profiled Feature** still requires
  one.
  The **Head** decides what a declaration *means* — which file, which shape, which commands — and the
  **Arm** the Dive is placed on builds it, at Dive start, out of its own bandwidth. A built Environment
  is several GB and the mesh is not a LAN, so the declaration travels and the packages do not. It is
  built in a **Pod**, as that User's **Guise** — a declaration is code, and CCC runs no User's code as
  the **Mantle** on any machine.
  An Environment is kept by nobody: it dies with the Dive that built it and the next run builds it
  again, so it is never something a User owns. What each Arm keeps instead is the *downloads*, cached
  node-locally, which is what makes rebuilding every time affordable. A **build** that fails is a failed
  Dive — it holds a **Device** while it builds, so its **Karma** is spent rather than returned.
- **Attribution** — the Head's binding of each **Dive** to the **User** who started it. It is what
  answers "whose Dive is this?" for the queue, the Dashboard, and Karma billing — and it is the
  *authoritative* answer: a node knows whose work it runs (that is the Dive's **Guise**), but only the
  Head knows who is billed for it, and only the Head can be asked about a Dive that has ended.
- **Feature** — a named capability an Arm may report having. The catalog is **built into CCC**
  (`vlearn`, `isaaclab`), not curated by an operator, because CCC knows what each name *means* — at
  minimum placement and `$<NAME>_PATH`, and where a name carries a runtime profile, the env vars,
  binds, cache and **Licence** wiring a Dive needs as well. A name CCC did not know would be a Slurm
  constraint with nothing behind it. Declared by the Arm owner at `setup --arm` (and by the Head's
  operator for its own co-located Arm), never auto-probed.
  A Dive requests one the same way it declares VRAM: as a *need*, not a machine name — expanding
  **Eligibility** exactly like VRAM does. The Head rejects a request no enrolled Arm can ever satisfy,
  same as an impossible VRAM ask.
  A Feature is what the *Arm* supplies, which is not always the software: it may be a heavy install
  that isn't everywhere, or the software plus its **Licence** where the copy that runs has to be the
  Arm's own. `isaaclab` is the first shape, `vlearn` the second.
  A Feature may carry a **runtime profile**, and `vlearn` does: CCC then arranges the package, binds,
  working directory, cache and env vars a Dive needs, so the submitter's script contains none of it. It
  has to be CCC's job rather than a script's — `LD_LIBRARY_PATH` is read by the loader at `exec`, so
  nothing running inside the **Pod** can set it for itself. Without a profile a Feature is placement
  plus `$<NAME>_PATH`, which is a complete answer for software a submitter drives directly.
  A profiled Feature therefore **requires an Environment**: the profile attaches its software *to* the
  declaration — matching its interpreter, extending its library path — so a Dive that requests one
  while declaring none is refused at submit, with nothing for it to attach to. A profiled Feature also
  brings the **system libraries** its software links against: those are CCC's to supply rather than the
  submitter's, because they are not Python packages and no **Environment** could install them.
  Where a Feature *supplies the software itself*, a **User** may still declare that package — pointing
  at a local copy is often what makes their project resolve on their own machine — but what runs is
  always the Arm's: CCC replaces the declaration with an empty placeholder before building and removes
  it again on the Arm. Declaring such a package without also requesting its Feature is refused.
- **Licence** — permission to run a Feature's software, held by the *machine* rather than by the
  software or the **User**. An Arm has at most one set, covering every Feature it offers, because a
  licence is bound to hardware and an Arm is one machine. A Dive placed there runs under the Arm
  owner's licence and never carries its own — so a Feature that is licence-only still constrains
  **Eligibility**, even though every eligible Arm could install the software. It lives in one directory
  per Arm, the home of the **Licence account**; its contents are merged into a **Guise**'s home inside
  the **Pod** for a Dive that asked for the Feature, so the software finds the machine's activation
  there.
- **Licence account** — `ccc-licence`, an account whose only purpose is *owning* the Licence
  directory, which keeps a Licence out of a human's home — the directory's contents are merged into the
  home of every Dive that asks for a Feature. `teardown` removes the account but never the directory, since an activation
  can be a finite resource and teardown+setup is the routine redeploy here.
- **Activation** — how a **Licence** comes to exist. A licence manager picks where to keep its state
  with `getpwuid(getuid())->pw_dir` and ignores `$HOME`, so the only way to choose that location is to
  choose which identity activates the software. CCC runs the vendor's activation once per machine,
  locally in a **Pod** during `cuttle setup --arm`, with the Licence directory as the running identity's
  home so the state lands there by construction. It is the one run permitted to see the Arm's product
  *key*, and nothing a User can submit asks for that.

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
- **Transfer** — the core flow: a **Dive**'s submitter is debited and the **owner** of the Arm it ran on
  is credited *the same Karma*. Running on your own Arm is therefore free. Karma spent on a
  commons/admin-owned Arm (no owner to pay) is simply **destroyed**.
- **Decay** — every Balance drifts back toward 0 over time, absent market forces. Debt is forgiven and
  hoarded wealth evaporates, so standing reflects recent conduct rather than history. Also what makes
  the system self-correcting: any Karma destroyed or granted out of thin air is absorbed automatically.
- **Device** — the unit a **Dive** actually holds, and the thing **Power**, **Demand**, **Eligibility**
  and **Settlement** are all reckoned per. A Device is either a whole GPU or a **Slice** of one; nothing
  else in the economy needs to know which.
  Every Dive holds **at least one**: there is no Device-less work on this cluster, and work that needs
  no GPU is simply work that belongs somewhere else (the CPU-only **Tidepool**). A Dive may hold
  **several**, but never across two **Arm**s — the Devices one Dive holds are always on a single
  machine, so "several Devices" never means several donors.
- **Slice** — a division of one **carvable** GPU into several Devices, each holding its own memory and
  its own share of the card, isolated *by the hardware* rather than by agreement. Several Dives can
  therefore hold one card at once without being able to reach — or starve — each other. A Slice is a
  Device in every respect: it is priced, held, and settled as one.
  **Unbuilt.** No Slice exists in CCC today, and neither does the **Beak** that would make one: every
  Device is a whole card, whether or not its hardware is **carvable**. What a Dive does to the card it
  already holds — dividing it in hardware for its own processes, inside its own allocation — is not a
  Slice and is not this: nobody else can be placed on it, nothing is priced by it, and it is undone
  when the Dive ends. The word stays reserved for the day the cluster itself carves.
- **Carvable** — a property of a GPU: whether its hardware can divide it into **Slice**s at all. Most
  cards cannot, and a GPU donated through a virtualization layer never can. So the fleet is permanently
  mixed, and a Dive is never told which kind it got.
- **Beak** — the Head-side authority that decides how each **carvable** card is divided, and re-divides
  it as the queue changes. It shapes *supply* only: it never chooses which **Dive** gets a **Slice**,
  it only decides which Slices exist. A Slice being held is untouchable, so the Beak works exclusively
  in the space nothing is using.
  It may leave a card **deliberately idle**. If filling free space would delay a Dive of higher
  standing, the Beak holds that space empty and waits — so a short Dive that can finish before the
  waiting one starts is carved for, and a long one is not. This is the only place in CCC where
  hardware is knowingly left unused, and it is what stops **Slice**s from becoming a way to shoulder
  ahead of the queue.
  An idle Slice holds no claim on anything: it is free space that merely happens to have a shape, and
  the Beak will unmake it for a Dive of higher standing. Only a Slice being *held* is untouchable.
  And no Dive is ever given a Slice larger than it asked for — the Beak reshapes rather than hand out
  Power nobody requested, so what a Dive is billed for is always what it declared.
- **Power** — what a device can *do*: its throughput, **measured** when its Arm enrolls, by running a
  benchmark **inside a Pod** — so it reflects the machine as Dives will really meet it (a throttled
  laptop card, a stale driver), not what its spec sheet claims. Intrinsic and
  fixed. It is what stops the meter paying an owner *more* for being *slower*: a second is worth only
  what a second on that card actually buys.
  A **carvable** card is measured once *per size of **Slice*** it can be divided into. A Slice's Power
  is measured like any other Device's and never derived from the whole card's, because dividing a card
  costs more than it takes — a quarter of a card is not a quarter of its Power, and only a benchmark
  knows by how much.
- **Demand** — how badly a device is *wanted*: the device-time being **asked for** (running *and*
  queued) against the device-time that exists. A GPU busy an hour with someone waiting behind it the
  whole hour was demanded for *two* hours. Extrinsic and live. Utilization pins to 100% the moment a
  card is busy *at all* and stops discriminating exactly when it matters; Demand keeps climbing with
  the queue behind it. It has a **ceiling**, but one far above the point where utilization has gone
  blind — because Demand is set by the people *queuing* and paid by the people *running*, and without a
  ceiling a queue could bill a **Dive** arbitrarily.
  Any device actually running work has a Demand of at least 1 — so a second is never sold below its
  **Power**.
  Demand attaches to the **physical card**, never to a **Slice**, and is counted in whole-card-hours:
  a Dive holding or queuing for a quarter of a card asks for a quarter of an hour. So how a card is
  carved can never move its price — size is carried by **Power**, scarcity by Demand, and the two
  multiply out at **Settlement**.
- **Eligibility** — the set of devices a **Dive** may run on, derived from what it *declares it needs*
  (how many **Device**s, how much VRAM across them, **Feature**s) rather than which machine it wants.
  A queued Dive adds **Demand**
  to every device it is eligible for — so scarce big-VRAM cards, wanted by big jobs *and* small ones,
  price highest. Users declare needs; only the **Admin** may name a machine outright.
  VRAM is declared as a **total**, not per-Device: a Dive wanting more than one card holds is asking to
  be spread over several, and says so by naming the count. The cluster never picks that number on a
  Dive's behalf — a second Device given silently would double what its submitter pays while their code
  went on using one, so the count is always the User's word. The two needs are satisfied **together** or
  not at all: enough Devices *and* enough VRAM on the same **Arm**, since a Dive never spans two.
  On a **carvable** card what a Dive declares stops merely *filtering* and starts *shaping*: it is
  eligible for **Slice**s that do not exist yet but that the **Beak** could carve for it. Eligibility
  is therefore about what the fleet *could* offer, not only what it happens to be offering now.
- **The Tide** — the **forecast** of Demand: what an hour on a given device has typically cost, by
  hour of day. **High Tide** = the busy hours, **Low Tide** = the quiet ones. The Tide does not *set*
  the price (Demand does, live) — it lets a User plan around it, and it is what the **Escrow** is
  quoted from.
- **Escrow** — Karma reserved against a Balance the moment a **Dive** is queued — meaning the moment the
  **Head** accepts it, whether or not a Device is yet waiting for it — quoted from the **Tide**
  and its declared duration. It suppresses the submitter's **Share** while the Dive is outstanding —
  so flooding the queue immediately costs you your own queue position — and is released at
  **Settlement**. Escrow is a hold, not a charge.
  A hold only ever **ratchets up**: it opens as a forecast, and if the **Meter** finds the real cost
  overtaking that forecast, the hold rises to meet it. It never falls before Settlement, so no Dive
  can quietly cheapen its own submitter's standing while it runs.
- **Meter** — what an outstanding **Dive** has cost *so far*: its seconds held, priced as they were
  held, accumulated while it runs. The Meter is the number **Settlement** actually moves — the
  **Escrow** was only ever the estimate of it.
- **Settlement** — the single moment a **Dive**'s Karma actually moves, when it ends: the **Escrow** is
  released and the real cost is **transferred** to the Arm's owner. The cost of a **Device** is the
  seconds it was *held* (not the seconds it was *busy* — you paid for exclusive access to it, whether
  it was a whole card or a **Slice**) × its **Power** × the **Demand** it ran under. Several Devices
  are several separate purchases.
  Over-reserving therefore costs a User nothing but queue position; under-reserving costs them
  nothing at all.
- **Retract** — an Arm owner's instant reclaim of their own machine (the panic-switch): the Arm stops
  accepting new work and running work is signalled, with a short grace period to exit. It is the
  cluster's *only* reclaim mechanism, and it is available on every Arm because there is no other kind
  of Arm: nothing is ever suspended in place or restarted elsewhere instead, since a **Dive** runs
  once. A Dive a Retract destroys **settles at zero** — its submitter pays nothing for work someone
  else killed, and the owner earns nothing for having killed it. Reclaiming is paid for by whoever
  chose to reclaim.
- **Return** — the counterpart of **Retract**: the owner giving a retracted Arm back to the cluster.
  A retracted Arm never leaves — it stays **enrolled** and on the **Mesh**, it simply stops accepting
  work — so returning it is the owner's to do, needs no **Admin**, and costs no new **enrollment token**.
  Retract/Return is a machine being lent and reclaimed, not a machine joining and leaving.
- **Early-failure rule** — a **Dive** that fails is **settled, not requeued**: it pays for what it
  actually consumed and does not silently run again.
- **End** — a **User** ending their own running or queued **Dive** (an **Admin** may end anyone's; the
  command is `cuttle dive end`). Like any other ending it **settles** for the seconds it actually held,
  and its submitter pays them: those seconds were really held on someone else's machine, and that
  **Arm**'s owner is owed them. Distinct from **Retract**, which is the *Arm owner* reclaiming a machine
  out from under work that did not choose to die, and is therefore the only ending that settles at
  **zero**. An Arm's owner has no power to end a single Dive on their Arm; they have **Retract**.
- **Override** — an admin-only operation that adds/removes/sets a User's **Balance** directly. The only
  flow that isn't a **Transfer** — it creates or destroys Karma, and **Decay** absorbs the imbalance.
  Changes *standing*: persistent, and felt in every future **contention**.
- **Queue bump** — an admin-only shove of one queued **Dive** up the queue, *without* touching anyone's
  Karma. Deliberately **bypasses** the economy rather than participating in it: it moves one job, once,
  and leaves no trace in the ledger. Distinct from **Override**, which would buy the same result by
  permanently distorting a User's standing.

## Documentation

- **Wiki** — the project's documentation, for every reader (Users, donors, Admins, contributors): a
  Quartz site built from `wiki/`, served by the Head at `/wiki/` to signed-in Users. _Avoid_: docs,
  notes, knowledge base.
- **Section** — one of the Wiki's top-level areas: Start, Using CCC, Donating, Admin, Contributing.
  _Avoid_: category, topic.
- **Node** — a Wiki page whose main job is to group other pages (its children); it may have content of
  its own. A role, not a fixed kind: a Leaf becomes a Node once pages hang off it. _Avoid_: hub, index
  page.
- **Leaf** — a Wiki page about one specific thing, with no children. _Avoid_: article, subpage.
- **Parent** — the single Node a page hangs off (a Section's is the home page); any other connection is
  a link in Relates To. _Avoid_: category, tag.
