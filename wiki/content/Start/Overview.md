---
title: Overview
tags: [leaf]
---
CCC is a shared pool of NVIDIA GPUs. People in the group lend it their machines, and everyone takes
turns using them. You keep your work in one place on a central machine, and borrow a GPU only while
you need one.

## The pieces

**The Head** is the central machine. It's always on. It holds everyone's files, runs the queue, and
serves the website. See [[Glossary#Head|Head]].

**Arms** are the machines people have lent to the cluster for their GPUs. An owner can take theirs
back at any time, so Arms come and go. Your files never live on one. See [[Glossary#Arm|Arm]].

**Your Tidepool** is your own space on the Head. It's a fixed-size folder (your home directory) plus a
small computer that's always running, which you can SSH into. You have root there, so you can
`apt install` what you like. It has no GPU: it's for editing code, organising data, git, and quick
jobs. See [[Tidepool]].

**A Dive** is a GPU session. You pick a folder from your Tidepool (the *main folder*), and CCC copies
it to a free GPU, on an Arm or on the Head itself. You get a shell there. Changes you make in that
folder are copied back to your Tidepool every minute, and once more when the Dive ends. See [[Dives]].

**Karma** keeps things fair. Using GPUs costs Karma, and lending a machine earns it. When the queue is
busy, people who've used less go first. When nobody's waiting, Karma doesn't matter. See [[Karma]].

**The Dashboard** is the website on the Head. It shows the queue, your files, your Karma and the
machines in the cluster. See [[Dashboard tour]].

**`cuttle`** is the command-line tool you install on your own computer. It gets you into your
Tidepool, starts and ends Dives, and copies files to and from your machine. Run `cuttle` on its own
and it opens a menu. See [[CLI]].

## A normal day

Say you're training a model in `~/fractals`.

1. You open a terminal on your laptop and run `cuttle tidepool`. You're now in your Tidepool on the
   Head. You pull the latest code and tidy up some data.
2. You need a GPU, so you run `cuttle dive fractals --time 4h`. CCC copies `~/fractals` to a free
   GPU and drops you into a shell there.
3. You start training inside `tmux`, then close your laptop. The Dive keeps going.
4. Later you run `cuttle dive attach` to check on it. Your checkpoints have been copying back to
   `~/fractals` in your Tidepool all along.
5. When you're done, `cuttle dive end`. The GPU goes back to the pool, and your results are safe in
   your Tidepool.

While a Dive holds `~/fractals`, that folder is read-only in your Tidepool, so the two copies can't
clash. It becomes writable again when the Dive ends.

## Where to go next

- [[Getting started]]: set up `cuttle` and run your first Dive.
- [[Using CCC/index|Using CCC]]: everyday work: files, Karma, environments, the CLI and the Dashboard.
- [[Donating/index|Donating]]: lend your own machine to the cluster as an Arm.
- [[Admin/index|Admin]]: run the Head.
- [[Contributing/index|Contributing]]: how CCC is built.

## Related
- [[Start/index|Start]]: parent
- [[Getting started]]: the next step
- [[Glossary]]: every term in plain words
- [[Tidepool]]: your own space on the Head
- [[Dives]]: GPU sessions in detail
- [[Karma]]: how turns are shared out
