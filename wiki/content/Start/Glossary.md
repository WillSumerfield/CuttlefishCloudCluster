---
title: Glossary
---
Every CCC word in plain language, with a link to the page that covers it. The first group are the
words you'll meet every day; the rest come up as you go.

## Head

The central machine of the cluster. It's always on, and it holds everyone's Tidepools, runs the
queue, and serves the Dashboard and this wiki. It usually has GPUs of its own too, which Dives can use
like any Arm's. More in [[Overview]].

## Arm

A machine someone has lent to the cluster for its GPUs. Its owner can take it back at any moment, so
Arms come and go, and your files never live on one. More in [[Arms]].

## Tidepool

Your own space on the Head: a fixed-size home folder plus a small computer that's always running,
which you can SSH into with `cuttle tidepool`. You have `sudo` there, but no GPU. Everyone gets one,
all the same size. More in [[Tidepool]].

## Dive

A GPU session. You name a folder in your Tidepool, CCC copies it to a free GPU and gives you a shell
there, and your changes copy back every minute. It lasts until you end it or its `--time` runs out.
Example: `cuttle dive myproject --time 4h --vram 16`. More in [[Dives]].

## Karma

How CCC keeps turns fair. Running Dives costs Karma, and lending a machine earns it when other people's
Dives run there. Karma never blocks you: it only decides who goes first when the queue is busy. More in
[[Karma]].

## Share

How much your Karma counts when you're competing for a GPU. It grows with your Balance but
levels off at both ends, so piling up Karma helps less and less, and so does sinking deep into debt.
More in [[Karma]].

## Tide

The usual price of an hour on each GPU, by hour of day. High Tide is the busy hours, Low Tide the quiet
ones. Use it to pick a cheaper time for a long Dive. [Open the Tide in the Dashboard](dashboard:/tide). More in
[[Karma]].

## Dashboard

The website on the Head. It shows the queue, your Dives, your files, your Karma and the machines in the
cluster, and lets you end Dives and manage your Tidepool. Open it with `cuttle dashboard`. More in
[[Dashboard tour]].

## cuttle

The command-line tool you install on your own computer. It gets you into your Tidepool, starts and
ends Dives, and copies files back and forth. Run `cuttle` on its own for a menu. It also works inside
your Tidepool and Dives. More in [[CLI]].

## Admin

The person who runs the Head: they create accounts, let new Arms join, and look after the cluster.
An Admin account sits outside the Karma system: it has no Karma and never waits behind anyone. Someone
who also wants to use the cluster normally does it with an ordinary account. More in
[[Admin/index|Admin]].

## User

Anyone with an account on the Head. Each User has one Tidepool, one Karma Balance, and the Dives and
Arms that are theirs. The Dashboard labels an ordinary User "Member". More in [[Users and access]].

## Donor

A User who lends a machine to the cluster, which makes it one of their Arms. Their Arm earns them Karma
whenever someone else's Dive runs on it, and running their own Dives on it is free. More in
[[Arm setup]].

## Mesh

The cluster's private, encrypted network linking the Head and every Arm. As a User you never join it:
your computer talks to the Head directly. More in [[Mesh]].

## Environment

The Python packages a Dive's code runs with. You name a dependency file in the main folder with `--env`
(a `pyproject.toml`, a requirements `.txt`, or an `environment.yml`), and CCC builds a `.venv` beside
it at the start of each Dive. More in [[Environments]].

## Main folder

The Tidepool folder a Dive works in. It's copied to the GPU, copied back every minute and once more at
the end, and read-only in your Tidepool while the Dive holds it. Only one Dive can hold a folder at a
time. More in [[Dives]].

## Dep folder

An extra Tidepool folder copied into a Dive as a read-only input, added with `--dep`. Nothing is
copied back from it, and several Dives can use the same one at once. More in [[Dives]].

## cuttle.toml

A file in a main folder that remembers that folder's Dive settings, such as `--time`, `--gpus` and
`--dep`. Flags you pass are saved there, so the next `cuttle dive myproject` reuses them. More in
[[Dives]].

## Queue

The list of Dives running now and waiting for a GPU, everyone's. When GPUs are busy, Karma decides who
goes next. See it with `cuttle queue` or [on the Dashboard](dashboard:/). More in [[Dashboard tour]].

## Outcome

How a finished Dive ended, shown in its history: **ended** (someone ended it), **timed out** (it ran
its full `--time`), or **lost** (it died early, for example because its Arm went offline). See
`cuttle history` or [the History page](dashboard:/history). More in [[Dives]].

## Balance

Your Karma as a number. Everyone starts at 0; it goes up when others use your Arms and down when you
use theirs, and slowly drifts back toward 0 over time. More in [[Karma]].

## Escrow

Karma set aside while a Dive is queued or running, based on how long you asked for. It isn't a charge:
when the Dive ends, it's released and you pay for the time you actually held the GPU. More in
[[Karma]].

## Feature

A piece of special software an Arm offers, such as `isaaclab` or `vlearn`. Ask for one with `--feature`
and your Dive only goes to Arms that have it. `cuttle feature list` shows them. More in
[[Arm features]].

## Retract

An Arm owner taking their machine back from the cluster, instantly. Dives running there end, and their
owners pay nothing for that time. `cuttle arm retract` does it; `cuttle arm return` lends the machine
again. More in [[Arms]].

## Enrollment token

A one-time secret an Admin makes so one new machine can join the cluster as an Arm. It names who owns
the machine and expires within the hour. More in [[Arm setup]].

## Cluster name

The short name your cluster was given when the Head was set up. Your computer reaches the Head through
it, and it's the name of your Tidepool's SSH alias: `ssh <cluster name>`. More in [[Tidepool]].

## Base image

The system every Tidepool and Dive starts from: Ubuntu 24.04 with Python, `uv`, CUDA compilers and
common tools. Whatever you `apt install` sits on top of it. `cuttle tidepool reset` takes you back to a
clean base image, keeping your home folder. More in [[Tidepool]].

## Related
- [[Start/index|Start]]: parent
- [[Overview]]: these words in context
- [[Getting started]]: using them for real
- [[Karma]]: Share, Tide, Balance and Escrow in depth
- [[Dives]]: main folders, dep folders and cuttle.toml
