---
title: Karma
---
[[Glossary#Karma|Karma]] is how CCC keeps the queue fair. Using GPUs costs Karma, and lending a
machine earns it. When the GPUs are busy and several people are waiting, people with more Karma go
a bit sooner. That's all it does.

## What Karma does and doesn't do

- **It only changes the order of the queue**, and only when people are competing for GPUs. On a quiet
  cluster it makes no difference at all.
- **It never blocks you.** There's no minimum. A negative balance is normal and fine: your Dives still
  run, they wait a little longer when it's busy.
- **The effect has a limit.** At worst, low Karma makes your Dive wait as if you'd joined the queue up to
  a day later than you did.
- **It fades.** Your balance drifts back toward zero over about a month, so a heavy week is forgotten
  and you can't stockpile Karma either.

## Earning and spending

When your Dive runs on someone's machine, the Karma it costs goes to that machine's owner. So:

- **Using a GPU costs Karma.** The cost is the time you *held* the GPU (busy or idle), times how
  powerful that GPU is, times how busy the cluster was at the time. A powerful card at a busy hour
  costs the most.
- **Lending a machine earns Karma** whenever someone else's Dive runs on it. See [[Arms]].
- **Your own machine is free.** A Dive on an Arm you own pays you back exactly what it costs.
- **Working in your Tidepool is free.** Only Dives cost anything.

1 Karma is roughly one TFLOP-hour of GPU work.

### While a Dive runs

When you queue a Dive, CCC sets aside an estimate of its cost, based on how long you asked for. This
is the **Escrow**. It isn't a charge. When the Dive ends, the estimate is released and you pay for the
time you really held the GPU. So asking for more `--time` than you need doesn't cost you more, but
the bigger estimate does lower your place in the queue while the Dive is open. It also means flooding
the queue with Dives pushes your own Dives back.

If an Arm's owner takes their machine back while your Dive is on it, you pay nothing for that Dive.

## Share

Your **Share** is what your balance is really worth in the queue, as a percentage between "always
loses" and "always wins". It changes less and less the further your balance moves from zero, so a huge
balance isn't much better than a good one, and deep debt isn't much worse than some.

## The Tide

The **Tide** shows how busy the cluster usually is at each hour of the day, based on the last month.
At high tide GPUs cost more, and at low tide less. Use it to pick a cheaper time for a long Dive.
It's a guide, not a quote: the real cost depends on how busy it is when your Dive runs.

```bash
cuttle tide
```

or [open the Tide in the Dashboard](dashboard:/tide).

## Seeing your Karma

```bash
cuttle karma          # everyone's standing: Balance, Escrow, Share
cuttle karma ledger   # your own history: every Dive that settled
cuttle account        # only you
```

[Open Karma in the Dashboard](dashboard:/karma) to see your balance, where you stand against everyone else, and
your history over the last 30 days. Each Dive's own cost is on its page in [History](dashboard:/history).

## Admins

[[Glossary#Admin|Admins]] are outside Karma completely. They don't earn it, don't pay it, have no
balance and never lose their place in the queue. Karma spent on a machine an Admin owns isn't paid to
anyone. Admins can also adjust someone's balance or move one Dive up the queue (see
[[Users and access]]).

## Related

- [[Using CCC/index|Using CCC]]: parent
- [[Dives]]: what costs Karma, and how the queue is ordered
- [[Arms]]: lending a machine earns Karma
- [[Dashboard tour]]: the Karma and Tide pages
- [[Glossary#Share|Share]]: in the glossary
- [[Glossary#Tide|Tide]]: in the glossary
