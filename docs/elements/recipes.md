# Recipes

## What it is

A **Recipe** is a piece of work you saved: the code itself, plus how to run it — entry point,
arguments, how long it needs, how much VRAM, where its results go, and which dependency file its
[Environment](environments.md) is built from.

Because it holds the actual code, you can run a Recipe from any machine with nothing to hand. New
laptop, no checkout, no clone: `cuttle recipe run nightly` and it goes.

And it is **frozen** — including what it runs *in*, not merely what it declared. It keeps running
exactly what it captured until you deliberately update it; your local directory can drift as far as
you like and the Recipe does not care.

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

The cost of that is space. Recipes hold against a **24 GB allowance**, and your shelf only shrinks
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

### Frozen code, unfrozen dependencies

A Recipe freezes what it *captured*, which is your dependency declaration — not what that
declaration resolves to. A Recipe holding a loose `requirements.txt` can build a genuinely different
[Environment](environments.md) months later. Pinning is yours to do, in the file you already keep.

### `cuttle recipe run` is not wired up

The Head answers `501`. Saving, listing, updating and deleting all work. Submit the project directly
with [`cuttle run`](spawns.md) in the meantime.
