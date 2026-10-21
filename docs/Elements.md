# Elements

What CCC does, one paragraph each. Rows marked **admin** are things only the operator does, and
only while sitting on the Head.

## Using the cluster

### [Spawns](elements/spawns.md)

A Spawn is one run of your code on the cluster. You upload a directory, say how long it needs and
how much GPU memory, and name the folders your results land in; CCC finds a machine, runs it in a
sandbox, and pulls the results back down to you when it finishes — whether or not you were still
watching. A Spawn runs *once*: if it fails, or the machine it was on is reclaimed, it does not
quietly start again somewhere else. It can hold several cards at a time, but always on one
machine — never spread across two donors.

### [Recipes](elements/recipes.md)

A Recipe is a Spawn you saved: your code plus how to run it, frozen and re-runnable from any machine
with nothing to hand. Saving one runs nothing and costs no Karma. It keeps running exactly what it
captured until you deliberately update it.

### [Environments](elements/environments.md)

The Python your code runs against. Every Spawn gets the same base; if you need more, you declare it
the way you already do — `uv.lock`, `pyproject.toml`, `requirements.txt`, `environment.yml` — and CCC
finds the file and builds it *before* your Spawn is placed, so no GPU is held while packages download
and the build costs you nothing.

### [Tide Pool](elements/tide-pool.md)

An interactive shell on real cluster hardware, for when you need to poke at something rather than run
it. It bills Karma for as long as you hold it, and closing your laptop does not end it. It keeps
nothing when the session ends.

### [Workspaces](elements/workspaces.md)

A working directory the cluster keeps — the Tide Pool's durable form. Open it and you are where you
left off; what you install, download or half-finish is still there next time. Holding one is free;
visiting one costs Karma like any Spawn. Each reserves a **Storage** out of a 32 GB allowance you divide
up yourself.

### [Karma](elements/karma.md)

Standing, not currency. You earn it by donating a machine and spend it by running work — but it never
gates anything. There is no balance at which CCC refuses your job; low standing only means you lose to
someone else when you both want the cluster at the same time. On an idle cluster it does nothing at
all. **admin:** `override`, `bump`.

### [Storage](elements/storage.md)

Where your things live and how long they last: uploaded projects (until the Spawn starts), results
(7 days, 32 GB), Recipes and Workspaces (indefinitely, 32 GB each), and the one-day Recovery Window that catches what you
forgot to declare.

## Donating a machine

### [Arms](elements/arms.md)

An Arm is a machine donated to the cluster. Its owner earns the Karma that work on it costs, and can
take it back instantly at any time — a Retract stops new work and ends what is running, and the Spawn
it killed settles at zero, so nobody pays for work someone else stopped. **admin:** `authorize`,
`admit`, `benchmark`, `chown`, `remove`.

### [Arm Features](elements/arm-features.md)

Some software is too heavy to put everywhere, or is licensed to one machine rather than to you. An
Arm's owner declares which of these their box has, and a Spawn asks for one the same way it asks for
VRAM — as a need, never as a machine name.

## How it holds together

### [Users & access](elements/users-and-access.md)

Accounts, logging in, sessions, and what makes an Admin different (they are outside the economy
entirely). **admin:** `user add/list/remove/reset`.

### [Isolation](elements/isolation.md)

What stops one User's Spawn from reading, killing or interfering with another's — Pods, Guises and
Siphons, enforced by the operating system rather than agreed to by convention.

### [Mesh](elements/mesh.md)

The private encrypted network every Head and Arm sits on, how a machine is admitted to it, and why
enrollment tokens are secret and short-lived. **admin:** `munge rotate`.

### [Telemetry](elements/telemetry.md)

The Head-side watch for work running outside the scheduler. **admin:** the alert mailer.

### [Backup & recovery](elements/backup-and-recovery.md)

**admin, entirely.** Snapshotting and restoring the Head's state: accounts, Karma, Arms, mesh.
