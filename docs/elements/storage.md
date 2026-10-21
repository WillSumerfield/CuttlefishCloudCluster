# Storage

## What it is

Two different things of yours can be sitting on the Head, and they behave differently. This page is
the whole of it:

| Thing | Kept for | Ceiling |
|---|---|---|
| An uploaded project | Until the Spawn stages it in | 4 GB per upload |
| A Spawn's declared results | 7 days from when it ended | 32 GB, all yours together |

Each ceiling is its own. Filling one never costs you any of another.

None of this is priced. Storage is never contended for, never costs [Karma](karma.md), and a full
allowance never affects who wins the queue.

**Nothing on the Head is an archive.** Your results are downloaded to your own machine
automatically, as each Spawn ends, and that is what makes the 7-day expiry safe.

## Typical use

**Get a Spawn's results again** (they were already downloaded once, automatically):

```sh
cuttle outputs 412
```

## Commands

| | |
|---|---|
| `cuttle outputs <id>` | Download a Spawn's declared results into `~/ccc/outputs/spawn-<id>-outputs/`. |

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

### A result is always accepted

The Karma is already spent and the work is already done, so refusing
to store the output would be pointless cruelty. If it does not fit, your *other, older* outputs are
evicted to make room — oldest first, silently. Even a single result larger than the whole 32 GB still
lands.

Your 32 GB is yours. Filling it cannot evict anybody else's results.

