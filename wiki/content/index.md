---
title: CCC Wiki
tags: [node]
---
The Cuttlefish Cloud Cluster (CCC) is a shared pool of GPUs. People in the group lend it their machines,
and everyone takes turns using them, fairly. This wiki covers how to use it, how to lend it a machine,
how to run it, and how it's built.

## What you need to know

CCC is one central machine, the **Head**, plus any number of lent machines, the **Arms**.

The Head is always on. It keeps your files and runs the queue and the website. Each person gets their
own space on it, called a **Tidepool**: a folder for your work and a small always-on computer you can
SSH into. That computer has no GPU, so it's for editing, organising and quick jobs.

When you need a GPU, you start a **Dive**. You pick a folder from your Tidepool, and CCC copies it to
a free GPU on one of the Arms. You work there, and your changes flow back to your Tidepool as you go.
When the Dive ends, the GPU goes back to the pool. Your files are still safe on the Head.

Who goes next is decided by **Karma**. Using GPUs costs it and lending a machine earns it. If the queue
is busy, people who've used less go first.

### Quick reference

| Word | What it means |
| --- | --- |
| **Head** | The central machine. It holds everyone's files, the queue and the Dashboard. |
| **Arm** | A machine someone has lent to the cluster for its GPUs. Arms come and go, and your files never live on one. |
| **Tidepool** | Your own space on the Head: your files, plus a small always-on computer you can SSH into. |
| **Dive** | A GPU session. A folder from your Tidepool runs on a free GPU until you end it or its time runs out. |
| **Karma** | How CCC keeps things fair. Using GPUs costs Karma, lending a machine earns it. |
| **Dashboard** | The website on the Head. Shows the queue, your files, your Karma and the machines. |
| **`cuttle`** | The command-line tool. Starts Dives, attaches to them and moves files around. |

The [[Glossary]] explains these and every other term in more detail.

## Where to go next

- [[Start/index|Start]]: new here? Begin with what CCC is, getting set up, and the glossary.
- [[Using CCC/index|Using CCC]]: day-to-day work in your Tidepool and Dives: files, Karma, environments, the CLI and Dashboard.
- [[Donating/index|Donating]]: lending your machine to the cluster as an Arm.
- [[Admin/index|Admin]]: running the Head: setup, users, mesh, alerts, backup, security.
- [[Contributing/index|Contributing]]: how CCC is built, for people changing it.
