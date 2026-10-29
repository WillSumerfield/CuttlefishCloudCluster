---
title: CCC Wiki
tags: [node]
---
## Summary
How to use, donate to, run and build the Cuttlefish Cloud Cluster (CCC): Tidepools, Dives, Arms and the Head.

## Details
CCC pools GPUs that people lend it, and lets everyone in the group share them fairly. You get a home on
the cluster that's always on, and when you need a GPU you borrow one for a while.

### The main words
You'll see these everywhere. Each one is one or two sentences here, and the glossary has the rest.

- **Head**: the one machine that runs the cluster. It holds everyone's files, the queue and the
  Dashboard. It can have GPUs of its own too.
- **Arm**: a machine someone has lent to the cluster for its GPUs. Arms come and go; your files never
  live on one.
- **Tidepool**: your own space on the Head. Your files live here, and there's a small always-on
  computer (no GPU) you can SSH into any time.
- **Dive**: a GPU session. You pick a folder from your Tidepool, CCC copies it to a free GPU, and you
  work there. Changes come back to your Tidepool as you go. It ends when you end it or its time runs
  out.
- **Karma**: how the cluster keeps sharing fair. Using GPUs costs Karma, lending a machine earns it,
  and people who've used less get to go first in the queue.
- **Dashboard**: the website on the Head, where you can see the queue, your files, your Karma and the
  machines.
- **`cuttle`**: the command-line tool. You use it to start Dives, attach to them and move files
  around.

### Using this wiki
Pages are grouped into the Sections below. In the graph, colour shows the Section, bigger dots group
other pages, and the ringed dot is the page you're on.

## Contains
- [[Start/index|Start]]: what CCC is, getting set up, and the glossary
- [[Using CCC/index|Using CCC]]: working in your Tidepool and Dives: files, Karma, environments, the CLI and Dashboard
- [[Donating/index|Donating]]: lending a machine to the cluster as an Arm
- [[Admin/index|Admin]]: running the Head: setup, users, mesh, alerts, backup, security
- [[Contributing/index|Contributing]]: how CCC is built, for people changing it
