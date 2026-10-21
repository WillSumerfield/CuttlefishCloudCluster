# Storage

## What it is

Four different things of yours can be sitting on the Head, and they behave differently. This page is
the whole of it:

| Thing | Kept for | Ceiling |
|---|---|---|
| An uploaded project | Until the Spawn stages it in | 4 GB per upload |
| A Spawn's declared results | 7 days from when it ended | 32 GB, all yours together |
| A [Workspace](workspaces.md) | Forever, until you delete it | 32 GB, **divided into a Storage each** |
| The Recovery Window copy | A day from when the Spawn ended | 16 GB per Spawn |

Each ceiling is its own. Filling one never costs you any of another.

The Workspace allowance is the one you divide up yourself — see [Storage](#workspace-storage) below.

None of this is priced. Storage is never contended for, never costs [Karma](karma.md), and a full
allowance never affects who wins the queue.

**Nothing on the Head is an archive.** Your results are downloaded to your own machine
automatically, as each Spawn ends, and that is what makes the 7-day expiry safe.

## Typical use

**Get a Spawn's results again** (they were already downloaded once, automatically):

```sh
cuttle outputs 412
```

**Get back something you forgot to declare** — within a day of the Spawn ending:

```sh
cuttle recover 412
```

## Commands

| | |
|---|---|
| `cuttle outputs <id>` | Download a Spawn's declared results into `~/ccc/outputs/spawn-<id>-outputs/`. |
| `cuttle recover <id>` | Download what it wrote *outside* those, into `~/ccc/outputs/spawn-<id>-recovery/`. One-day window. |

## Behavior

### Uploaded projects are not stored at all

The working directory you upload with `cuttle run` is kept only while the Spawn is *starting*. It is
deleted the moment it stages in, it belongs to no allowance, and it cannot be retrieved.

### Results come to you, then expire there

When a Spawn finishes, its declared outputs are downloaded into `~/ccc/outputs/spawn-<id>-outputs/`
on your machine, whether or not you are watching — see [Spawns](spawns.md#where-your-results-go).

The Head's copy lives **7 days from when the Spawn ended**, not from when it was registered, and only
while your 32 GB has room. Past either, `cuttle outputs <id>` tells you so rather than pretending the
Spawn never existed.

### The two ceilings behave differently, on purpose

**A result is always accepted.** The Karma is already spent and the work is already done, so refusing
to store the output would be pointless cruelty. If it does not fit, your *other, older* outputs are
evicted to make room — oldest first, silently. Even a single result larger than the whole 32 GB still
lands.

**Creating a Workspace past the allowance is refused.** You delete one and try again. The difference
is that a Workspace is something you deliberately chose to keep, whereas an output arrives on its own
from work already done.

Your 32 GB is yours. Filling it cannot evict anybody else's results.

### Workspace Storage

The Workspace allowance is the only one you divide up yourself. Every other ceiling on this page is a
single pool your things fill until it is full; a [Workspace](workspaces.md) instead reserves a
**Storage** out of yours when you create it:

```sh
cuttle workspace new scratch --storage 16    # 16 GB of your 32, spent the moment it exists
```

Reserved, not used — an empty 16 GB Workspace costs you the whole 16 GB. That is the point of it:
what a session runs out of is that one Workspace's Storage, and never a pool it shares with everything
else you keep.

Creating one past the allowance is refused. Filling one is not: nothing is
evicted, the Workspace simply stops being saved and says so inside the session. You can resize at any
time, and never below what the tree already holds.

### The Recovery Window

Your script wrote a checkpoint to `./out/` and you declared `--output results`. The Spawn ends. What
now?

You have **a day**. `cuttle recover <id>` pulls down the entire working directory the Spawn ran
in — everything outside your declared outputs — up to 16 GB.

Long enough to be worth having: you find a wrong `--output` the next morning, not within the same half
hour. Not long enough to be a place to keep things.

It exists for a mistake you did not know you were making, so it is never opt-in and never something
to plan around:

- it is capped, and a Spawn whose working directory exceeds the cap simply never gets one;
- it expires on its own;
- past the window it is gone, with nothing to appeal to.

One more way a copy can end early: if you finish a great many Spawns in a day, your newest copies
displace your oldest. Only ever your own — another User's flood can never cost you a window, and yours
can never cost them one.

Declare `--output` correctly. This is a net, not a floor.

(The word "hold" never means this. In CCC a hold is [Escrow](karma.md#escrow-and-why-over-estimating-is-safe).)
