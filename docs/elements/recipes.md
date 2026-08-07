# Recipes

## What it is

A **Recipe** is a piece of work you saved: the code itself, plus how to run it — entry point,
arguments, how long it needs, how much VRAM, where its results go, and which dependency file its
[Environment](environments.md) is built from.

Because it holds the actual code, you can run a Recipe from any machine with nothing to hand. New
laptop, no checkout, no clone: `cuttle recipe run nightly` and it goes.

And it is **frozen**: it keeps running exactly what it captured — the code and the declaration —
until you deliberately update it. Your local directory can drift as far as you like and the Recipe does
not care.

Recipes are private to you. Nobody else can see or run yours.

## Typical use

**Save the thing you keep re-running:**

```sh
cuttle recipe save nightly train.py --time 60 --vram 24 -o checkpoints \
  --manifest pyproject.toml -- --epochs 5
```

Everything after `--` is frozen as the Recipe's arguments, the same way `cuttle run` passes them
through.

**See what you have, and what it is costing you in space:**

```sh
cuttle recipe list
```

**Refresh the code after you have changed it, keeping every setting:**

```sh
cuttle recipe update nightly
```

**Leave the arguments off** and be walked through it:

```sh
cuttle recipe save
```

## Commands

| | |
|---|---|
| `cuttle recipe save <name> <script>` | Freeze this directory into a Recipe. |
| `cuttle recipe list` | Your Recipes, and what they hold against your allowance. |
| `cuttle recipe update <name>` | Re-freeze around this directory's current code. |
| `cuttle recipe run <name>` | Run the snapshot as a Spawn. **Not wired up yet** — see below. |
| `cuttle recipe delete <name>` | Delete it and free the bytes. |

## Behavior

### Saving runs nothing

`cuttle recipe save` submits no Spawn, takes no queue place, and costs no Karma. It uploads and it
stops. This is the whole difference between a Recipe and a [Spawn](spawns.md): one is a thing you
keep, the other is a thing that happens.

### It is kept, and only you remove it

Everything else `cuttle` uploads is temporary — a Spawn's project is deleted the moment it stages in,
its results expire after 7 days. A Recipe is the exception: nothing on the Head ever deletes one.
They do not expire and they are never evicted to make room.

The cost of that is space. Recipes hold against a **32 GB allowance**, and your shelf only shrinks
when you say so. Saving past the ceiling **fails** — you delete one and try again — because a Recipe
is something you deliberately chose to keep, unlike a result, which arrives on its own from work
already done. (See [Storage](storage.md) for how the two differ at the ceiling.)

### Updating one

A bare `cuttle recipe update nightly` refreshes the **code** and changes nothing else. Every setting
keeps whatever the Recipe already holds unless you pass a flag for it. `--output` is the one to watch:
it *replaces* the declared outputs rather than adding to them.

The Recipe keeps running its old snapshot right up until the update lands. On the Head an update is
delete-then-save — the old copy comes off your allowance before the new one is charged — so what
matters for fitting is how much it **grows by**, not its total size. If it does not fit, the old
snapshot is left alone.

The freeze is a promise the *cluster* keeps, not one it keeps against you. An update simply replaces
what was there. Abandon one half-way and you have neither version.

### The declaration is frozen too

A Recipe holds the dependency file it names and the [Arm Features](arm-features.md) it asks for,
alongside the code — so re-running one is one command, with nothing to re-type and nothing to get
wrong. What it does *not* hold is a built [Environment](environments.md): that is made fresh by
whichever machine runs the Spawn, every time.

So the honest limit of the freeze: a loose `requirements.txt` can still resolve to something different
six months from now, because it is the *file* that is frozen. Pin what you need pinned — a `uv.lock`
or exact versions — exactly as you would anywhere else.

Rebuilding every run is cheap in practice. Each machine keeps the packages it has already downloaded,
so a repeat run of a Recipe on a box that has run it before installs from disk rather than the network.

A Recipe briefly kept its built Environment, when the cluster built those centrally. It cannot any
more, and should not: an Environment built on one machine could not be moved to another cheaply, which
is why it is now built where it runs.
