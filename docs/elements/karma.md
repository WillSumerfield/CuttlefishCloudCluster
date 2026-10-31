# Karma

## What it is

**Karma is standing, not currency.** You earn it by donating a machine and spend it by running work.

The single most important thing about it: **Karma never gates**. There is no floor, no bankruptcy,
no balance at which the cluster refuses your job. A User with terrible Karma submits exactly like
anyone else. All that happens is that when two people want the cluster at the same moment, the one
with better standing goes first.

On an idle cluster, Karma does nothing at all. It is not a quantity you can run *out* of.

Everyone starts at **0**, and everyone drifts back toward 0 over time.

## Typical use

**See where you stand** — and where everyone else does, because a Balance on its own means nothing:

```sh
cuttle karma
```

**Your own history** — the Settlements and Overrides that got you here:

```sh
cuttle karma ledger
```

**Everything about you in one place** — identity, Balance, Escrow, Share, queue priority:

```sh
cuttle account
```

**Plan around the busy hours:**

```sh
cuttle tide
```

## Commands

| | |
|---|---|
| `cuttle karma` | Every User's standing. |
| `cuttle karma ledger` | Your own ledger. Yours alone. |
| `cuttle account` | Who you are and where you stand. |
| `cuttle tide` | How badly each Arm has been wanted, hour by hour. |
| `cuttle karma override` | **admin** — see below. |
| `cuttle karma bump` | **admin** — see below. |

`cuttle karma` shows everyone because Karma *ranks*; `cuttle karma ledger` shows only you because a
history needs nobody else's rows.

## Behavior

### The four numbers

**Balance** — your Karma. An unbounded signed score centred on 0. Positive means the cluster owes you
standing; negative means you owe it. It is not a quantity of anything: there is no supply, no pot,
nothing to inflate.

**Escrow** — Karma held against a Spawn that has not finished. It is a hold, not a charge.

**Share** — what your Balance actually *does*. A saturating curve between "always loses" and "always
wins", so hoarding has diminishing returns and debt has diminishing punishment. Neither end runs away.

**Nice** — the queue priority your Share implies. This is the entire output of the economy: one
number per queued Spawn.

### What a Spawn costs

Cost is settled at the moment a Spawn ends, and it is:

> **the seconds the device was *held* × its Power × the Demand it ran under**

*Held*, not busy — you paid for exclusive access to the card whether or not you kept it fed.

**Power** is what the device can do, measured by running a benchmark on it inside a sandbox when its
Arm enrolls — so it reflects the machine as your Spawn will really meet it: a throttled laptop card,
a stale driver. Not what the spec sheet claims. This is what stops the meter
from paying an owner *more* for being *slower*.

**Demand** is how badly that card is wanted right now: the device-time being asked for, running
*and* queued, against the device-time that exists. A GPU busy for an hour with someone waiting behind
it the whole time was demanded for *two* hours. It is capped, because Demand is set by the people
queuing and paid by the people running — without a ceiling, a queue could bill your Spawn arbitrarily.

Ask for several GPUs and they are several separate purchases — each billed at its own card's Power,
for as long as you hold it, whether or not your code ever touches it.

### Escrow, and why over-estimating is safe

When the Head *accepts* your Spawn — the moment it is queued, whether or not a card is waiting for
it — Karma is reserved against your Balance, quoted from the Tide and your declared `--time`.

That hold suppresses your Share while the Spawn is outstanding. This is what makes queue-flooding
self-punishing: submit 500 Spawns and you immediately cost yourself your own queue position, before
any of them have run.

A hold only ever **ratchets up**. It opens as a forecast, and if the real cost overtakes the
forecast, the hold rises to meet it. It never falls before settlement, so no Spawn can quietly
cheapen its own submitter's standing while it runs.

At settlement the hold is released and the *real* cost is transferred. So:

- **Over-reserving `--time` costs you queue position and nothing else.**
- **Under-reserving costs you nothing at all** — except that your Spawn gets killed at the limit.

### Where the Karma goes

It goes to the owner of the Arm your Spawn ran on. Same amount, debited from you and credited to them.

Two consequences fall straight out of that:

- **Running on your own Arm is free.** You pay yourself.
- **Karma spent on a Commons Arm is destroyed.** An Arm the Admin owns has no owner in the economy to
  pay, so the Karma simply ceases to exist.

### Decay

Every Balance drifts back toward 0. Debt is forgiven, hoarded wealth evaporates, and your standing
reflects recent conduct rather than your entire history — a month of good behaviour is worth roughly
what a month of good behaviour is worth, no matter what came before.

It is not a smooth bleed. Recent events keep close to full weight (a settlement is worth ~98% after
5 days, ~86% after 10), then fall off a cliff (~37% at 20 days, ~5% at 30). "Recent" means recent.

Decay is also what makes the economy self-correcting: any Karma conjured out of nothing by an
Override, or destroyed on a Commons Arm, is quietly reabsorbed.

### The Tide

The **Tide** is the forecast: what an hour on a given Arm has typically cost, by hour of day, over
the last 30 days. **High Tide** is the busy hours, **Low Tide** the quiet ones.

The Tide does not *set* prices — live Demand does. What it does is let you plan, and it is what your
Escrow is quoted from. `cuttle tide` draws it as a sparkline per Arm.

### Endings, and what each one costs

| Ending | Settles |
|---|---|
| Finished normally | For the seconds it held. |
| Your script failed | For the seconds it held. Settled, **not requeued**. |
| You cancelled it | For the seconds it held. You chose to stop; you pay. |
| An operator cancelled it | For the seconds it held. Same as your own cancel. |
| The Arm was **retracted** under it | **Zero.** |

An operator's cancel bills its submitter even though the submitter did not choose it, because those
seconds were really held on somebody else's card and that Arm's owner is owed them. Zeroing it would
either pay the owner nothing for a card they really lent, or mint the difference — and **Override** is
the only thing in CCC allowed to mint.

The retract row is the only ending that costs nothing, and the reason is that reclaiming is paid for
by whoever chose to reclaim. Its submitter pays nothing for work someone else killed, and the owner
earns nothing for having killed it. See [Arms](arms.md#retract-and-return).

## Admin — on the Head

The Admin is **outside the economy**. They earn no Karma, pay none, and hold no Balance; their Spawns
are never escrowed, never settled, and never lose contention. An Arm the Admin owns is a **Commons
Arm** and the Karma spent on it is destroyed. A person who wants to *use* the cluster does so as an
ordinary User, with an ordinary account.

**`cuttle karma override <user> <amount>`** adds or removes Karma from a User's Balance directly. It
is the only flow that is not a transfer — it creates or destroys Karma, and Decay absorbs the
imbalance over about a month. It changes *standing*, so it is felt in every future contention.

**`cuttle karma bump <spawn>`** shoves one queued Spawn up the queue without touching anyone's Karma.
It leaves no ledger row: it bypasses the economy rather than participating in it. Bump again to go
further.

Reach for `bump` when you want one job to go sooner, once. Reach for `override` only when you mean to
change what someone's standing *is* — using it to speed up a single Spawn buys that result by
permanently distorting a User's position.

`bump` runs on the Head rather than over the network because a bump is a negative `nice`, and Slurm
grants those only to an operator — an authority the LAN-facing API deliberately does not have.
