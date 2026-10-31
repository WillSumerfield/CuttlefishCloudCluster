---
title: Isolation
---
Every User's work runs in a Podman container, on the [[Glossary#Head|Head]] or on someone else's
[[Glossary#Arm|Arm]]. Isolation is what keeps one User out of another's files, GPUs and processes,
and keeps everyone's code off the host. It's enforced by the kernel (ID ranges, cgroups, network
namespaces), not by asking jobs to behave. Donors are trusted people, so CCC doesn't try to protect
a Dive from the owner of the machine it runs on.

This page covers the two places User code runs: a [[Glossary#Dive|Dive]] on a node, and a
[[Glossary#Tidepool|Tidepool]] on the Head.

## What both share

- **Rootful Podman, unprivileged inside.** Each User gets a fixed range of 65,536 host IDs:
  `base = 2³¹ + (uid − 60000) × 65536`, where `uid` is the User's Guise uid. The container runs with
  `--uidmap 0:<base>:65536`, so root inside is an ordinary, unused uid on the host. The User is uid
  1000 inside, with sudo, so `apt install` works without touching the host.
- **Same range everywhere.** The formula is the same on the Head and on every Arm, so `rsync -a
  --numeric-ids` keeps ownership right when files move between them. No subuid files, no allocator.
- **Never** `--privileged`, never a host namespace.
- **One base image**, built on the Head and pulled by digest. A User's own changes live in their own
  layer on top. The base is read from a shared store, mapped per container by the kernel.

Rootless Podman was tried and rejected: it moves its container into `user@<uid>.service`, outside the
Slurm job, where it escaped the GPU fence, the memory limit and `scancel` (measured on a real Head).

## A Dive on a node

### Who it runs as

A Dive is a Slurm job owned by the User's **Guise**, a per-User Linux account (`ccc-u<uid>`) that
exists on every node, created by the Slurm prolog. Nobody logs in as a Guise. The Head API
(running as `ccc`) can only `sbatch` and `scancel` as Guises, never as root.

The job's only command is `sudo -n ccc-launch $SLURM_JOB_ID`. That's the one sudo rule a Guise has,
and it only accepts a number. `ccc-launch` then refuses unless:

- its own cgroup sits inside that job's step (`…/job_<id>/step_<n>/`), so it really is running in
  the job, on this node;
- the job's owner is the user who called sudo;
- the job is `RUNNING`.

It never takes settings from its arguments or environment. It reads the spec back from slurmctld's
copy of the batch script (`scontrol write batch_script`), which the Head wrote at submit. The image
must be a digest ref on the Head's registry.

### Processes and GPUs

The container runs with `--cgroup-parent` set to the job's step cgroup, so it lives inside the Slurm
job. Two things Slurm does *not* reach, so `ccc-launch` handles them:

- **GPUs.** The container's own device rules replace the job's, so Slurm's GPU fence doesn't carry
  over. `ccc-launch` reads which cards the job was given (`scontrol` GRES indices), maps them to GPU
  or MIG UUIDs, and passes only those as CDI devices (`--device nvidia.com/gpu=<uuid>`). Other cards
  aren't visible, and `mknod` is refused. A WSL Arm has exactly one GPU behind `/dev/dxg`, passed as
  `nvidia.com/gpu=all`.
- **Killing it.** `scancel` doesn't signal the container. `--init` and Podman's signal proxy pass the
  SIGTERM in so the shell can stop cleanly, and the Slurm epilog's `podman rm -f` is the real kill,
  before the node takes its next job.

CPU and RAM aren't capped per Dive (`ConstrainCores` and `ConstrainRAMSpace` are off).

### Network

The container gets its own network namespace (`--network private`, a NAT bridge). Its sshd is
published on the node's **mesh address** only, at the Dive's port. Users aren't on the Mesh, so they
reach it through a TCP relay on the Head. A Head Dive publishes the port on every address instead,
because there's no relay in front of it.

SSH into a Dive takes only the User's own keys, and presents their Tidepool's host key.

### Files

- Everything lives under the job directory `/var/lib/ccc/jobs/<id>` (0700). The epilog removes it.
- Folders come in over the Head's rsync daemon, which listens on the mesh address only. Each Dive
  gets its own modules, its own rsync user and a random secret. The User-data modules (main, deps,
  dotfiles) run as the User's mapped uid, so they can read only what the User could. The main folder
  module is the only writable one.
- Inside the container, dep folders and dotfiles are mounted read-only. Private SSH keys and
  credential files are never staged.
- `/ccc-cache` is a uv package cache **shared by every User** on that node (mounted with `idmap`).
  It holds public packages only.
- When the Dive ends, its container and its User image are removed. Nothing of the User's stays on
  the node.

### Back on the Head

While a Dive holds a main folder, that folder is bind-mounted read-only inside the User's Tidepool
(done with `nsenter` into the Tidepool container at the first stage-in). So there's only ever one
writer. A second Dive on the same main folder is refused.

## A Tidepool on the Head

Each User has one container, `tp-<user>`, kept running by `ccc-tidepool@<user>.service`.

| Fence | How |
| --- | --- |
| **Disk** | A fixed-size ext4 image per User (`fallocate`d up front), loop-mounted at `/var/lib/ccc/tidepools/<user>/`. It holds `home/`, the User's own Podman store (so apt installs count against it too) and the SSH host key. A full image is ENOSPC for that User only. |
| **IDs** | The User's ID range, as above. |
| **CPU and RAM** | `--cpus`, `--memory` and `--memory-swap` per container, under `ccc-tidepools.slice`, which has its own total `CPUQuota` and `MemoryMax`. The Head's Slurm node is shrunk by that total. An OOM hits only that User. |
| **GPU** | None. Tidepool containers get no GPU devices at all. GPU work is a Dive. |
| **Network** | One bridge per User, `ccc-tp-<user>`, created with `isolate=true`. One shared bridge would let Tidepools reach each other. sshd is published on `32000 + (uid − 60000)` on every address. Outbound internet works. |
| **SSH** | Key-only: no passwords, no PAM, no root login, no X11. Keys come from `head.db` (`ssh_keys`) plus the User's own `~/.ssh/authorized_keys`. |

The Head API itself can't read Tidepool files. Anything that touches them goes through the root
helper `ccc-tidepool`, and sudoers pins each verb to a regex (`templates.py`). The Dashboard's file
browser, for example, is `ccc-tidepool fs <user> …`, which runs `podman exec -u <user>` inside that
User's own container, so their permissions and symlinks apply. The API only ever passes the
signed-in User's own name.

Usernames must match `^[a-z][a-z0-9_-]{0,31}$`, and system names (`root`, `ubuntu`, `ccc`, …) are
refused, because the name becomes a Linux account inside the container and part of unit names and
paths on the Head.

### Sessions inside containers

`cuttle` works inside a Tidepool or Dive with an *ambient* session written into the container. It's
never Admin, and it can only read, apart from starting and ending the User's own Dives. A Dive's
session is revoked when the Dive closes; a Tidepool's is rotated each time the container starts.

## Related
- [[Contributing/index|Contributing]]: parent
- [[Architecture]]: where these pieces sit and how a Dive moves through them
- [[Security]]: the trust model, for Admins
- [[Arms]]: what a donor's machine runs
