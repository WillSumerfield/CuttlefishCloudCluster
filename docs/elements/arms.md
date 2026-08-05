# Arms

## What it is

An **Arm** is a machine donated to the cluster — a desktop with a spare GPU, a lab box, a laptop
under a desk. It runs Ubuntu 24.04, natively or under WSL2 on Windows, and the GPU has to be NVIDIA.

Every Arm has an **owner**: the User whose Karma goes up when someone else's work runs on their
machine.

The thing that makes donating safe is **Retract**. It is a panic switch, it is on every Arm, and it
is instant: your machine stops taking new work and whatever is running on it right now is ended. You
do not ask anyone, you do not wait for a queue to drain, and you do not lose the enrollment — the
Arm stays a member of the cluster, just closed.

To actually set a machine up, see [Set up an Arm](../setup/arm.md). This page is what happens
afterwards.

## Typical use

**Take your machine back, right now:**

```sh
cuttle arm retract my-desktop
```

**Give it back when you are done with it:**

```sh
cuttle arm return my-desktop
```

**See the fleet** — who owns what, and what state each one is in:

```sh
cuttle arm list
```

## Commands

| | |
|---|---|
| `cuttle arm retract <name>` | Stop new work and end what is running. Yours. |
| `cuttle arm return <name>` | Reopen it for new Spawns. |
| `cuttle arm list` | Every Arm: owner, and authorized/enrolled/admitted. |
| `cuttle arm sync-key` | *Run on the Arm.* Re-fetch the munge key after a rotation. See [Mesh](mesh.md#the-munge-key). |
| `cuttle arm authorize` | **admin** — see below. |
| `cuttle arm admit` | **admin** — see below. |
| `cuttle arm benchmark` | **admin** — see below. |
| `cuttle arm chown` | **admin** — see below. |
| `cuttle arm remove` | **admin** — see below. |

## Behavior

### Retract and Return

`cuttle arm retract` does two things at once: the Arm stops accepting new Spawns, and anything
running on it is signalled. A running Spawn gets `TERM` immediately, then a **3-minute grace period**
to exit cleanly, then it is force-killed.

A Spawn that a Retract destroys **settles at zero**. Its submitter pays nothing for work someone else
stopped, and *you earn nothing for having stopped it*. That second half is the point: without it,
Retract would be a way to farm Karma by yanking a card back mid-run.

`cuttle arm return` reopens the Arm. It does not resurrect a Spawn the Retract already caught —
that one is gone.

### Where you can retract from

Anywhere you are logged in. Retract and Return are ordinary network calls to the Head, like `cuttle
cancel`, so they run from your laptop, a Client box, the Head, or the Arm itself — and they work
whether or not the Arm is up, because the state that stops placement lives on the Head.

Two conditions: you need a session (`cuttle login` — enrolling an Arm does not create one), and the
Arm has to be **yours**. The Head checks ownership on every call and there is no admin exception to
it; an operator acting for a donor uses `--any`, which is refused off the Head.

A retracted Arm **never leaves**. It stays enrolled and stays on the mesh; it simply stops accepting
work. So returning it is entirely yours to do: no admin, no new enrollment token, no re-setup.
Retract and Return are a machine being lent and reclaimed — not one joining and leaving.

### You cannot kill one Spawn

There is no way for an Arm's owner to end a single job running on their machine. What you have is
Retract, which takes the whole machine back. This is deliberate: an owner deciding *which* of their
guests' jobs deserves to die is a power the cluster does not hand out.

### What your Arm earns

The Karma a Spawn costs is transferred from its submitter to you. What it costs depends on your
machine's **Power** — its measured throughput — and on **Demand**, how badly it is being wanted.
Running your own work on your own Arm is free, because you pay yourself. See [Karma](karma.md).

If the Admin owns an Arm it is a **Commons Arm**: there is no owner in the economy to pay, so the
Karma spent there is destroyed rather than transferred.

### Power is measured, not declared

When your Arm enrolls, CCC runs a matmul benchmark on it, inside a sandbox, and that number is your
Arm's Power for pricing and scheduling. It is measured rather than read off a spec sheet so it
reflects the machine as Spawns will really meet it: a virtualized WSL GPU, a throttled laptop card,
a stale driver. It is also what stops the meter from paying you *more* for being *slower*.

### The states an Arm can be in

`cuttle arm list` shows three, and they are sequential:

- **authorized** — the admin has minted a token for it. The machine itself has done nothing yet.
- **enrolled** — the donor redeemed the token; the box is on the mesh and has registered its hardware.
- **admitted** — Slurm knows its GPUs and will place work on it.

An Arm stuck at *enrolled* is one whose admission did not finish, usually because `cuttle arm
authorize` gave up waiting for the donor. `cuttle arm admit` finishes it.

### Declaring what your box has

At `cuttle setup --arm` you are asked, one by one, which of CCC's built-in
[Features](arm-features.md) this machine has and where each lives. Answer yes only for what is
really there — a Spawn that asks for a Feature can be sent nowhere else, so a wrong answer sends
someone's job to a machine that cannot run it.

If you re-run setup, what you declared last time is offered back so you can keep it unchanged.

## Admin — on the Head

**`cuttle arm authorize <name> --user <owner>`** is the whole of adding an Arm. It mints an
enrollment token, prints it, then waits for the donor to redeem it and admits them to Slurm when
they do.

> The token is a **secret**. It admits one machine, once, within the hour. Send it privately.

**`cuttle arm admit <name>`** finishes an admission that `authorize` gave up waiting for — it gives
an enrolled Arm its GRES line and reconfigures Slurm.

**`cuttle arm benchmark <name>`** re-measures an Arm's Power and re-prices it. Normally part of
`authorize`; run it by hand after a driver or GPU change, or if the original benchmark timed out
because the donor's box was not ready.

**`cuttle arm chown <name> --user <owner>`** re-points an Arm at a different owner. It touches
neither the mesh nor Slurm, so the donor's machine keeps running throughout.

**`cuttle arm remove <name>`** deletes an Arm from Slurm, the registry **and** the mesh. All three,
because a departed donor who keeps a live mesh address keeps the thing that authorizes fetching the
cluster's munge key. It asks you to type the Arm's name to confirm: re-adding it is a full
re-enrollment, not an undo.

`retract` and `return` both take `--any` on the Head, for acting on an owner's behalf.
