# Storage

## What it is

Four different things of yours can be sitting on the Head, and they behave differently. This page is
the whole of it:

| Thing | Kept for | Ceiling |
|---|---|---|
| An uploaded project | Until the Spawn stages it in | 4 GB per upload |
| A Spawn's declared results | 7 days from when it ended | 8 GB, all yours together |
| A [Recipe](recipes.md) | Forever, until you delete it | 32 GB, all yours together |
| The Recovery Window copy | 30 minutes from when the Spawn ended | 16 GB per Spawn |

Each ceiling is its own. Filling one never costs you any of another.

A Recipe holds more than the code you uploaded: after its first run it also keeps the
[Environment](environments.md) built from it, which carries its own interpreter and its own
dependencies and is usually the larger half. That is what makes a Recipe genuinely frozen, and it is
why the Recipe ceiling is the big one. `cuttle recipe list` shows the total, env included.

None of this is priced. Storage is never contended for, never costs [Karma](karma.md), and a full
allowance never affects who wins the queue.

**Nothing on the Head is an archive.** Your results are downloaded to your own machine
automatically, as each Spawn ends, and that is what makes the 7-day expiry safe.

## Typical use

**Get a Spawn's results again** (they were already downloaded once, automatically):

```sh
cuttle outputs 412
```

**Get back something you forgot to declare** — within 30 minutes of the Spawn ending:

```sh
cuttle recover 412
```

**See what your Recipes are costing you:**

```sh
cuttle recipe list
```

## Commands

| | |
|---|---|
| `cuttle outputs <id>` | Download a Spawn's declared results as a tar, into the current directory. |
| `cuttle recover <id>` | Download what it wrote *outside* those. 30-minute window. |

## Behavior

### Uploaded projects are not stored at all

The working directory you upload with `cuttle run` is kept only while the Spawn is *starting*. It is
deleted the moment it stages in, it belongs to no allowance, and it cannot be retrieved.

This is why a [Recipe](recipes.md) is *captured* and never *recovered*: by the time there is a result
worth admiring, the code behind it is already gone. If you want to keep the code, save a Recipe on
purpose.

### Results come to you, then expire there

When a Spawn finishes, its declared outputs are downloaded into `~/ccc/outputs/spawn-<id>-outputs/`
on your machine, whether or not you are watching — see [Spawns](spawns.md#where-your-results-go).

The Head's copy lives **7 days from when the Spawn ended**, not from when it was registered, and only
while your 8 GB has room. Past either, `cuttle outputs <id>` tells you so rather than pretending the
Spawn never existed.

### The two ceilings behave differently, on purpose

**A result is always accepted.** The Karma is already spent and the work is already done, so refusing
to store the output would be pointless cruelty. If it does not fit, your *other, older* outputs are
evicted to make room — oldest first, silently. Even a single result larger than the whole 8 GB still
lands.

**A Recipe save is refused.** Saving past the wall fails and you delete one and try again. The
difference is that a Recipe is something you deliberately chose to keep, whereas an output arrives on
its own from work already done.

Your 8 GB is yours. Filling it cannot evict anybody else's results.

### The Recovery Window

Your script wrote a checkpoint to `./out/` and you declared `--output results`. The Spawn ends. What
now?

You have **30 minutes**. `cuttle recover <id>` pulls down the entire working directory the Spawn ran
in — everything outside your declared outputs — up to 16 GB.

It exists for a mistake you did not know you were making, so it is never opt-in and never something
to plan around:

- it is capped, and a Spawn whose working directory exceeds the cap simply never gets one;
- it expires on its own;
- past the window it is gone, with nothing to appeal to.

Declare `--output` correctly. This is a net, not a floor.

(The word "hold" never means this. In CCC a hold is [Escrow](karma.md#escrow-and-why-over-estimating-is-safe).)
