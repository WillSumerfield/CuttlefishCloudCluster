# CCC — Root Glossary

> **Cuttlefish Cloud Cluster (CCC).** Themed cuttlefish names are the *canonical* terminology
> (CLI, code, docs). This file defines the *concepts* and the repo's bounded contexts.
> `CONTEXT.md` is a glossary only — no implementation details, decisions, or specs.
> Per-context glossaries live in each top-level context dir (e.g. `karma/CONTEXT.md`).

## Deployment roles (things that run on a machine)

- **Head** — the cluster control plane. One logical role. Runs the scheduler/control plane and
  hosts the head-side of other domains (Karma, telemetry).
- **Arm** — a compute node that donates resources to the cluster. One role, with no permanent/
  reclaimable distinction to declare: every Arm is its owner's to reclaim at any moment, through
  **Retract**, so there is no kind of Arm that is more lent than another. Arms run on
  native **Ubuntu 24.04**.
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
- **Cluster name** — the one name a machine uses to reach the **Head**, in place of an address. It is
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
  cluster state, submitting **Spawns**, following them, and (for the **Admin**) running the cluster.
  Reached from a browser on the LAN or the **Mesh**.
- **CLI** — one tool, the `cuttle` command (Python; usable on a client that hosts no Arm). Two distinct duties:
  - *Client duties* (from any machine): the alternative to the **Dashboard**, at operation **parity**
    with it — login, job submission, and following work. Scriptable throughout; `setup` is additionally
    offered as a guided **wizard**. Parity covers what can be asked of the *cluster*, not what each
    client can do to the machine it runs on: it is the CLI that arranges the machine it runs on so a program already
    installed there — an editor, most obviously — can reach a live **Tide Pool**; a browser can
    describe that session but cannot hand it to anything outside itself.
  - *Host duties* (only on the machine being changed): node bring-up and removal (`setup`/`teardown`).
    These are the one thing the **Dashboard** can never do, because they provision the box you run them
    on.

## People & access

- **User** — a person with an account on the Head (admin-provisioned: username + password, argon2
  hash). A User's identity ties every Spawn they submit to them, marks any Arms they **own** (donate),
  and anchors their Karma wallet.
- **Admin / Maintainer** — the privileged User (the operator) who provisions accounts, mints
  **enrollment tokens** (naming each Arm's owner), and can **override** Karma. One person today.
  The role's authority has two sources and they are not interchangeable: the **account**, which is what
  a **Session** carries and which grants no power over anyone else's work; and the **Head** itself, where
  being present at the machine is the authorization for every privileged act. An Admin away from the Head
  is an ordinary **User** with a title.
  The Admin is **outside the economy** — a role for *running* the cluster, not for *using* it. They
  earn no **Karma** (an Arm they own is a **Commons Arm**, and its Karma is destroyed), pay none, and
  hold no **Balance**: their **Spawns** are never **Escrow**ed, never **Settled**, and never lose
  **contention**. A person who wants to use the cluster does so as an ordinary **User**, with an
  ordinary account.
- **Session** — a User's authenticated login carrying the authority to submit jobs and read their own
  data. It is the **Head**'s own record of that login; the token a client holds is only the User's copy
  of it. So a Session ends when the Head is told to end it, or when its own lifetime runs out — never
  merely because a client discarded its copy, which makes the Session unreachable *from that box* while
  leaving it live everywhere else. Distinct from **Mesh** membership, which authorizes *machines*, not
  people.
## Domains (bounded contexts that may deploy across roles)

- **Karma** — the token economy. Deploys on the Head. Covers wallets/balances, dynamic
  pricing, congestion effects, and the admin **override** (privileged add/remove of Karma). Sets
  Slurm priority directly — no FairShare, no accounting database.
- **Telemetry** — monitoring & alerting. Deploys on the Head: rogue-actor/out-of-scheduler detection
  on the Head's own machine, with operator email alerting.

## Job/experiment concepts

- **Spawn** — a single traceable execution of a piece of work on the cluster, submitted with a project
  uploaded there and then. A Spawn runs **once**: if the machine it was placed on
  fails, or its **Arm** is retracted underneath it, the Spawn fails with it rather than quietly starting
  over somewhere else. So "this Spawn" always names one attempt, never a series of them — what it was
  billed for and what it produced both refer to the same single run.
- **Name** — what a **Spawn** is called wherever a person meets it: the queue, the archive, an alert.
  A human's words, always, and never CCC's — a Spawn that names nothing is refused before it exists,
  on exactly the terms its **Environment** declaration is, because a cluster that invents names fills
  its own history with rows nobody can tell apart. So every Spawn and every **Tide Pool** is named at
  submit.
  A Name is a *label*, never an identifier: nothing is looked up by it, two Spawns may share one, and
  what tells them apart is the id they already have. Distinct from the entry script, which says what
  *ran* rather than what it was *for*, and which a Spawn goes on keeping beside its Name.
- **Tide Pool** — an interactive/testing session: an allocation that attaches a live shell inside a
  **Pod** on an **eligible** Arm, for debugging against real hardware. Bills Karma for as long as it is
  held. Like a **Spawn**, it declares *needs* and is *placed* — a User cannot name the machine
  (see **Eligibility**); it is told which Arm it got. On its own it keeps nothing: what it wrote dies
  with it.
  The shell it attaches is the **cluster's own**, and the same one for everybody: CCC ships one
  configured shell, and the plain editors a person needs to change a file, into every Pod, and carries
  no User's dotfiles to any of it. A Tide Pool is a place to debug
  against real hardware, not a machine a User moves into — so what greets them is furniture the cluster
  provides, and a User who wants their own arranges it within the session like anything else they write
  there. Nothing about it survives the session, on the same terms as everything else a Tide Pool holds.
  How a User *meets* that shell is not fixed, though: a Tide Pool is an ordinary remote-shell
  endpoint, so it can be entered through the **CLI**'s own terminal or by pointing the editor already
  running on the machine a User is sitting at. Neither is more real than the other — the work happens
  inside the **Pod** either way, and an editor attached from outside is a visitor to the session and
  never a way to keep what it held.
- **Mantle** — the cluster's *own* identity: who the **Head** is, to itself and to every **Arm**. It
  owns what the cluster owns — the account records, the Head's key, every **User**'s uploaded work and
  every **Spawn**'s results — and it is what the Head acts as when it does work of its own rather than
  a User's. It is nobody's: no User is ever the Mantle, and nothing a User submits runs as it.
- **Guise** — the identity a **Spawn** runs as on the machine that runs it. One per **User**, the same
  one wherever the cluster places their work, and taken on by a machine the first time that User's work
  lands there. It is what stops one User's Spawn from reading, killing or interfering with another's —
  a boundary the operating system enforces rather than one the cluster merely agrees to. A Guise is
  *not* an account a person can log into: it authenticates nobody, carries no authority, and is
  unrelated to a User's **Session**. Distinct from **Pod** (*what* a Spawn runs inside), from the
  **Mantle** (who the *cluster* is), and from **Attribution** (the Head's own record of whose it is).
  It is not confined to any one machine: anything the cluster does on a User's behalf that involves
  *their* code — including building their **Environment**, on the **Arm** that will run it — is done as
  their Guise, never as the **Mantle**. A User's code has no way to be the cluster, on any machine.
- **Siphon** — the one channel through which a **Spawn** draws its project in and sends its results
  home. Opened when the Spawn is submitted and closed once its transfers have finished, it reaches
  exactly that Spawn's own uploaded work and its own results, and nothing else the **Head** holds — so
  work running on a donated machine cannot read another **User**'s project or write into another
  Spawn's results, even though every **Arm** shares one **Mesh**. Being on the Mesh admits a *machine*;
  a Siphon authorizes one *Spawn*. Its closing is tied to that Spawn's last transfer and not to
  **Settlement**: a Spawn's final copy home can still be arriving well after it has been billed.
- **Pod** — the isolated *place* a job runs inside, on whichever machine is running it. Its walls are
  real, and they enclose the **whole** Spawn — the payload and the wrapper that stages its files in and
  out, not just the payload: a Spawn cannot see the host's filesystem, the machine's other processes, or
  any concurrent Spawn's working files, including their scratch. A Pod is the *walls*, never the
  contents. What is installed within one is that Spawn's **Environment**, its **Substrate**, and
  whatever else the Spawn goes on to install for itself — every Pod's own system is writable, and no
  Pod keeps what it holds. That system is the Spawn's alone, unreachable by every other Spawn on the
  machine, and discarded with the Pod. And a Pod is not only an
  **Arm**'s: work the **Head** does on a User's behalf is walled the same way, as the same **Guise**.
- **Environment** — the Python a **Spawn**'s code actually runs against inside its **Pod**. It is
  **self-contained**: it carries its own interpreter and inherits nothing from the machine or from the
  Pod's image, so the same declaration runs the same way everywhere and nothing the cluster changes
  underneath can alter what a Spawn already runs. A **User** declares one in their project, in the
  dependency file their own tooling already writes, and **every Spawn names the file it declares** —
  the cluster supplies no default and infers nothing, so a Spawn that names no declaration is refused
  before it exists. The requirement is on the *naming*, not on the contents: a file that declares
  nothing is still a complete declaration, and a User whose work needs no package answers by naming
  an empty one rather than by declining. CCC never guesses which file describes the work, because a
  dependency file in a repo is not always the one that does. A **Tide Pool** is the one exception, and may decline: it is a
  shell rather than a run, and one opened with no project has no file in it to name.
  The **Head** decides what a declaration *means* — which file, which shape, which commands — and the
  **Arm** the Spawn is placed on builds it, at job start, out of its own bandwidth. A built Environment
  is several GB and the mesh is not a LAN, so the declaration travels and the packages do not. It is
  built in a **Pod**, as that User's **Guise** — a declaration is code, and CCC runs no User's code as
  the **Mantle** on any machine.
  An Environment is kept by nobody: it dies with the Spawn that built it and the next run builds it
  again, so it is never something a User owns. What
  each Arm keeps instead is the *downloads*, cached node-locally, which is what makes rebuilding every
  time affordable.
  Everything decidable without building is still refused at submit, before the Spawn exists: no
  declaration, a named file that is not there, one nothing can classify, a node without the build tools.
  A **build** that fails is a failed Spawn — it holds a **Device** while it builds, so its **Karma** is
  spent rather than returned.
- **Substrate** — the system packages a **Spawn** declares, installed into its **Pod** beneath its
  **Environment**. What the Environment is to Python, the Substrate is to everything under it: the
  libraries and tools a build compiles against and a script shells out to, which no dependency file can
  name because they are not Python at all. Declared in a file in the Spawn's **own project**, named at
  submit exactly as an Environment's is — the cluster supplies no default and infers nothing here
  either — and *optional*, unlike an Environment, because most work needs none.
  It is **not** an **Arm Feature**, and the two are never confused: a Feature is heavy or licence-gated
  software that lives on particular **Arm**s and therefore decides which of them are **eligible**; a
  Substrate constrains placement not at all, because it is fetched wherever the Spawn lands.
  Laid down BEFORE the Environment is built, so a package that must compile against a system library
  finds it — which is the whole reason it is declared rather than merely installed once the Spawn is
  already running. A Spawn may still install more for itself afterwards; only what it *declared* is
  reproduced. Nothing about either survives the **Pod**.
  A name nothing can install is a failed Spawn on the same terms a failed **build** is, since no
  archive can be consulted at submit — but a file that is not there, or one holding something that is
  not a package name, is refused at submit like every other declaration that can be judged without
  building.
- **Attribution** — the Head's binding of each Spawn to the **User** who submitted it. It is what
  answers "whose Spawn is this?" for the queue, the Dashboard, and Karma billing — and it is the
  *authoritative* answer: a node knows whose work it runs (that is the Spawn's **Guise**), but only the
  Head knows who is billed for it, and only the Head can be asked about a Spawn that has ended.
- **Shared storage** — the unified cluster-wide filesystem (home dirs, code, base envs) mounted
  identically on every node. Distinct from the `shared/` code context.
- **Feature** — a named capability an Arm may report having. The catalog is **built into CCC**
  (`config.FEATURE_NAMES`: `vlearn`, `isaaclab`), not curated by an operator, because CCC knows what
  each name *means* — at minimum placement and `$<NAME>_PATH`, and where a name carries a runtime
  profile, the env vars, binds, cache and **Licence** wiring a Spawn needs as well. A name CCC did not
  know would be a Slurm constraint with nothing behind it. Declared by the Arm owner at
  `setup --arm` (and by the Head's operator for its own co-located Arm), never auto-probed.
  A Spawn requests one the same way it declares VRAM: as a *need*, not a machine name — expanding
  **Eligibility** exactly like VRAM does. The Head rejects a request no enrolled Arm can ever satisfy,
  same as an impossible VRAM ask.
  A Feature is what the *Arm* supplies, which is not always the software: it may be a heavy install
  that isn't everywhere, or the software plus its **Licence** where the copy that runs has to be the
  Arm's own. `isaaclab` is the first shape, `vlearn` the second.
  A Feature may carry a **runtime profile** (`ccc_head_api.features`), and `vlearn` does: CCC then
  arranges the package, binds, working directory, cache and env vars a Spawn needs, so the submitter's
  script contains none of it. It has to be CCC's job rather than a script's — `LD_LIBRARY_PATH` is read
  by the loader at `exec`, so nothing running inside the **Pod** can set it for itself. Without a
  profile a Feature is placement plus `$<NAME>_PATH`, which is a complete answer for software a
  submitter drives directly.
  A profiled Feature therefore **requires an Environment**, and a Spawn that requests one while
  declaring none is refused at submit like any other unsatisfiable ask: the profile attaches its
  software *to* the declaration — matching its interpreter, extending its library path — so with
  nothing declared there is nothing for it to attach to. This falls on the **Tide Pool** alone, since
  every other Spawn must declare anyway; a Tide Pool's freedom to decline is freedom from declaring
  for *itself*, not from what a Feature it asked for needs. An unprofiled Feature asks for none of
  this and stays available to a Tide Pool opened with nothing in hand.
  A profiled Feature also brings the **system libraries** its software links against. Those are CCC's
  to supply rather than the submitter's, because they are not Python packages and no **Environment**
  could install them — so a Spawn that requests the Feature gets them without declaring anything.
  Where a Feature *supplies the software itself*, a **User** may still declare that package — pointing
  at a local copy is often what makes their project resolve on their own machine — but what runs is
  always the Arm's: CCC replaces the declaration with an empty placeholder before building and removes
  it again on the Arm. Declaring such a package without also requesting its Feature is refused, since
  no placeholder would ever be replaced and the Spawn could only fail at `import`. Nor does it matter
  where they pointed — a copy beside the project is stood in for as readily as one within it, since the
  Arm's is what runs either way.
  A Feature's runtime is arranged in a place of CCC's own and never inside the Spawn's project — the
  working directory its software resolves its data against is CCC's to site, not the project directory
  it happened to be. Neither the runtime nor the placeholder that stands in for a supplied package
  writes over what the **User** uploaded, and their declared copy is left as they wrote it.
- **Licence** — permission to run a Feature's software, held by the *machine* rather than by the
  software or the **User**. An Arm has at most one set, covering every Feature it offers, because a
  licence is bound to hardware and an Arm is one machine. A Spawn placed there runs under the Arm
  owner's licence and never carries its own — so a Feature that is licence-only still constrains
  **Eligibility**, even though every eligible Arm could install the software. It lives in one directory
  per Arm, which is the home of the **Licence account**, and is bound at a **Guise**'s home inside the
  **Pod**.
- **Licence account** — `ccc-licence`, an account whose only purpose is *owning* the Licence directory,
  which it does at `2770 ccc-licence:ccc-guises`. That ownership is what keeps a Licence out of a
  human's home, which matters because the whole directory is bound into every Spawn that asks for a
  Feature. It does not perform the activation — a **Guise** does, inside the activation Spawn, which is
  why the directory is group-writable. `teardown` removes the account but never the directory — an
  activation can be a finite resource, and teardown+setup is the routine redeploy here.
- **Activation Spawn** — how a **Licence** comes to exist. A licence manager picks where to keep its
  state with `getpwuid(getuid())->pw_dir` and ignores `$HOME`, so the only way to choose that location
  is to choose which identity activates the software. CCC runs the vendor's activation as a **Guise**,
  in a **Pod**, with the Licence directory bound at that Guise's home — so the state lands there by
  construction, and nobody has to find or copy anything. Submitted as the Arm is admitted, beside the
  Power benchmark, and for the same reason: **an Arm joins with its declarations true or it does not
  join**. It is the one Spawn permitted to see the Arm's product *key*, and the flag that says so
  exists only on the Head-local admin path — `/submit` has no equivalent and never should.

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
- **Device** — the unit a **Spawn** actually holds, and the thing **Power**, **Demand**, **Eligibility**
  and **Settlement** are all reckoned per. A Device is either a whole GPU or a **Slice** of one; nothing
  else in the economy needs to know which.
  Every Spawn holds **at least one**: there is no Device-less work on this cluster, and work that needs
  no GPU is simply work that belongs somewhere else. A Spawn may hold **several**, but never across two
  **Arm**s — the Devices one Spawn holds are always on a single machine, so "several Devices" never
  means several donors.
- **Slice** — a division of one **carvable** GPU into several Devices, each holding its own memory and
  its own share of the card, isolated *by the hardware* rather than by agreement. Several Spawns can
  therefore hold one card at once without being able to reach — or starve — each other. A Slice is a
  Device in every respect: it is priced, held, and settled as one.
  **Unbuilt.** No Slice exists in CCC today, and neither does the **Beak** that would make one: every
  Device is a whole card, whether or not its hardware is **carvable**. What a Spawn does to the card it
  already holds — dividing it in hardware for its own processes, inside its own allocation — is not a
  Slice and is not this: nobody else can be placed on it, nothing is priced by it, and it is undone
  when the Spawn ends. The word stays reserved for the day the cluster itself carves.
- **Carvable** — a property of a GPU: whether its hardware can divide it into **Slice**s at all. Most
  cards cannot, and a GPU donated through a virtualization layer never can. So the fleet is permanently
  mixed, and a Spawn is never told which kind it got.
- **Beak** — the Head-side authority that decides how each **carvable** card is divided, and re-divides
  it as the queue changes. It shapes *supply* only: it never chooses which **Spawn** gets a **Slice**,
  it only decides which Slices exist. A Slice being held is untouchable, so the Beak works exclusively
  in the space nothing is using.
  It may leave a card **deliberately idle**. If filling free space would delay a Spawn of higher
  standing, the Beak holds that space empty and waits — so a short Spawn that can finish before the
  waiting one starts is carved for, and a long one is not. This is the only place in CCC where
  hardware is knowingly left unused, and it is what stops **Slice**s from becoming a way to shoulder
  ahead of the queue.
  An idle Slice holds no claim on anything: it is free space that merely happens to have a shape, and
  the Beak will unmake it for a Spawn of higher standing. Only a Slice being *held* is untouchable.
  And no Spawn is ever given a Slice larger than it asked for — the Beak reshapes rather than hand out
  Power nobody requested, so what a Spawn is billed for is always what it declared.
- **Power** — what a device can *do*: its throughput, **measured** when its Arm enrolls, by running a
  benchmark **inside a Pod** — so it reflects the machine as Spawns will really meet it (a throttled
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
  ceiling a queue could bill a **Spawn** arbitrarily.
  Any device actually running work has a Demand of at least 1 — so a second is never sold below its
  **Power**.
  Demand attaches to the **physical card**, never to a **Slice**, and is counted in whole-card-hours:
  a Spawn holding or queuing for a quarter of a card asks for a quarter of an hour. So how a card is
  carved can never move its price — size is carried by **Power**, scarcity by Demand, and the two
  multiply out at **Settlement**.
- **Eligibility** — the set of devices a Spawn may run on, derived from what it *declares it needs*
  (how many **Device**s, how much VRAM across them, **Feature**s) rather than which machine it wants.
  A queued Spawn adds **Demand**
  to every device it is eligible for — so scarce big-VRAM cards, wanted by big jobs *and* small ones,
  price highest. Users declare needs; only the **Admin** may name a machine outright.
  VRAM is declared as a **total**, not per-Device: a Spawn wanting more than one card holds is asking to
  be spread over several, and says so by naming the count. The cluster never picks that number on a
  Spawn's behalf — a second Device given silently would double what its submitter pays while their code
  went on using one, so the count is always the User's word. The two needs are satisfied **together** or
  not at all: enough Devices *and* enough VRAM on the same **Arm**, since a Spawn never spans two.
  On a **carvable** card what a Spawn declares stops merely *filtering* and starts *shaping*: it is
  eligible for **Slice**s that do not exist yet but that the **Beak** could carve for it. Eligibility
  is therefore about what the fleet *could* offer, not only what it happens to be offering now.
- **The Tide** — the **forecast** of Demand: what an hour on a given device has typically cost, by
  hour of day. **High Tide** = the busy hours, **Low Tide** = the quiet ones. The Tide does not *set*
  the price (Demand does, live) — it lets a User plan around it, and it is what the **Escrow** is
  quoted from.
- **Escrow** — Karma reserved against a Balance the moment a Spawn is queued — meaning the moment the
  **Head** accepts it, whether or not a Device is yet waiting for it — quoted from the **Tide**
  and its declared duration. It suppresses the submitter's **Share** while the Spawn is outstanding —
  so flooding the queue immediately costs you your own queue position — and is released at
  **Settlement**. Escrow is a hold, not a charge.
  A hold only ever **ratchets up**: it opens as a forecast, and if the **Meter** finds the real cost
  overtaking that forecast, the hold rises to meet it. It never falls before Settlement, so no Spawn
  can quietly cheapen its own submitter's standing while it runs.
- **Meter** — what an outstanding **Spawn** has cost *so far*: its seconds held, priced as they were
  held, accumulated while it runs. The Meter is the number **Settlement** actually moves — the
  **Escrow** was only ever the estimate of it.
- **Settlement** — the single moment a Spawn's Karma actually moves, when it ends: the **Escrow** is
  released and the real cost is **transferred** to the Arm's owner. The cost of a **Device** is the
  seconds it was *held* (not the seconds it was *busy* — you paid for exclusive access to it, whether
  it was a whole card or a **Slice**) × its **Power** × the **Demand** it ran under. Several Devices
  are several separate purchases.
  Over-reserving therefore costs a User nothing but queue position; under-reserving costs them
  nothing at all.
- **Retract** — an Arm owner's instant reclaim of their own machine (the panic-switch): the Arm stops
  accepting new work and running work is signalled, with a short grace period to exit. It is the
  cluster's *only* reclaim mechanism, and it is available on every Arm because there is no other kind
  of Arm: nothing is ever suspended in place or restarted elsewhere instead, since a **Spawn** runs
  once. A Spawn a Retract destroys **settles at zero** — its submitter pays nothing for work someone
  else killed, and the owner earns nothing for having killed it. Reclaiming is paid for by whoever
  chose to reclaim.
- **Return** — the counterpart of **Retract**: the owner giving a retracted Arm back to the cluster.
  A retracted Arm never leaves — it stays **enrolled** and on the **Mesh**, it simply stops accepting
  work — so returning it is the owner's to do, needs no **Admin**, and costs no new **enrollment token**.
  Retract/Return is a machine being lent and reclaimed, not a machine joining and leaving.
- **Early-failure rule** — a Spawn that fails is **settled, not requeued**: it pays for what it
  actually consumed and does not silently run again.
- **Cancel** — a **User** ending their own outstanding **Spawn** early (an **Admin** may cancel anyone's).
  Like any other ending it **settles** for the seconds it actually held, and its submitter pays them
  whoever ended it: those seconds were really held on someone else's machine, and that **Arm**'s owner is
  owed them. Distinct from **Retract**, which is the *Arm owner* reclaiming a machine out from
  under work that did not choose to die, and is therefore the only ending that settles at **zero**. An
  Arm's owner has no power to kill a single Spawn on their Arm; they have **Retract**.
- **Override** — an admin-only operation that adds/removes/sets a User's **Balance** directly. The only
  flow that isn't a **Transfer** — it creates or destroys Karma, and **Decay** absorbs the imbalance.
  Changes *standing*: persistent, and felt in every future **contention**.
- **Queue bump** — an admin-only shove of one queued **Spawn** up the queue, *without* touching anyone's
  Karma. Deliberately **bypasses** the economy rather than participating in it: it moves one job, once,
  and leaves no trace in the ledger. Distinct from **Override**, which would buy the same result by
  permanently distorting a User's standing.
