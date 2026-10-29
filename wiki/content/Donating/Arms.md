---
title: Arms
tags: [leaf]
---
An Arm is a machine someone has lent to CCC for its GPUs: a gaming PC, a lab desktop, a spare server.
When someone starts a [[Glossary#Dive|Dive]], CCC puts it on a free GPU on one of the Arms. Every Arm
has an owner, the [[Glossary#Donor|Donor]], who earns [[Glossary#Karma|Karma]] while other people's
work runs on it.

## What runs on your Arm

Dives, one per GPU. Each Dive is a container holding a copy of someone's project folder. It runs as
long as they asked for, then ends and is cleaned up. Their files go back to the [[Glossary#Head|Head]],
not onto your machine for keeps.

A Dive can see:

- the GPU it was given, and no other card on your machine
- its own copy of the user's files
- a shared download cache that CCC keeps for building Python environments
- the internet

A Dive can't see your files, your home folder, your other GPUs or your own programs. It runs as an
ordinary unprivileged user on your machine, even though it looks like root inside the container.

It's your machine, so you can still see in. With sudo you could look at a running Dive's files.
Donors are trusted members of the group, so CCC doesn't try to hide them from you.

## What you earn

When someone else's Dive runs on your Arm, the Karma it costs goes to you. How much depends on two
things:

- **Power**: how fast your GPU is. Setup measures it with a benchmark, so a faster card earns more per
  hour.
- **Demand**: how busy the queue is. When people are waiting, every hour costs and earns more.

Your own Dives on your own Arm are free. You pay yourself. See [[Karma]] for how the whole economy
works.

## Taking your machine back

Need your GPU for a game, a deadline or a demo? **Retract** your Arm. It stops new Dives landing on
it and ends the ones running now. They get a 3-minute grace period to wrap up, then they're stopped.

From the Dashboard, [open Arms](dashboard:/arms) and press **Retract** on your card. Or from any machine you're
logged in on:

```bash
cuttle arm retract my-desktop
```

When you're done, give it back:

```bash
cuttle arm return my-desktop
```

or press **Return** on the card.

A few things to know:

- **Nobody pays and nobody earns** for a Dive you retract. The person whose Dive you stopped isn't
  charged, and you don't earn for it.
- **The other person loses little.** A Dive copies its work back to the Head about once a minute, so
  they lose at most the last minute.
- **Return doesn't bring a Dive back.** A Dive you retracted is over.
- **You can't end just one Dive.** Retract takes the whole machine back.
- **Your Arm stays in the cluster.** Retracted isn't removed: no new token, no new setup, no Admin
  needed to bring it back.

Switching the machine off or letting it sleep also takes it offline, but any running Dive is cut off
without the grace period. Retract first if you can.

## The Arms page

[Open Arms in the Dashboard](dashboard:/arms) to see every machine in the cluster.

**My arms** shows a card for each machine you own, with its GPU, its Power and a Retract or Return
button. It only appears if you own an Arm.

**All arms** lists every machine: name, owner, state, GPUs, VRAM, Power, and its address on the
cluster's private network.

The state is one of:

| State | What it means |
| --- | --- |
| **idle** | Ready and waiting for a Dive. |
| **allocated** | A Dive is running on it now. |
| **benchmarking** | Measuring its GPU speed, usually right after setup. |
| **retracted** | Its owner has taken it back for now. |
| **awaiting donor** | The Admin has made a token, but the owner hasn't run setup yet. |
| **awaiting admit** | Set up, but the Admin hasn't finished adding it. |
| **down** | Offline or broken. Switched off, asleep, or something went wrong. |

From the command line, `cuttle arm list` shows the same machines.

## Related
- [[Donating/index|Donating]]: parent
- [[Arm setup]]: lending a machine, updating it, removing it
- [[Arm features]]: special software on your Arm
- [[Karma]]: how earning and spending works
- [[Dives]]: what runs on an Arm
- [[Glossary#Arm|Arm]]: the term
