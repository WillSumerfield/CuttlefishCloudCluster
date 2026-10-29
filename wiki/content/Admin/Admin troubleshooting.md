---
title: Admin troubleshooting
tags: [leaf]
---
When something's wrong on the Head, start by finding which service is unhappy, then read its log.
Below are the commands for that, and fixes for the problems you're most likely to meet.

## Is everything running?

```bash
systemctl status ccc-head-api ccc-enroll-api ccc-karma ccc-rsyncd \
  headscale tailscaled-ccc slurmctld slurmd munge
systemctl list-timers 'ccc-*'
```

Read a service's log:

```bash
journalctl -u ccc-head-api -n 100       # the Dashboard, sign-ins, Dives
journalctl -u ccc-karma -n 100          # the Karma tick
journalctl -u ccc-monitor -n 50         # the health checks
```

A User's Tidepool:

```bash
systemctl status ccc-tidepool@alice
cuttle tidepool restart --user alice
```

The scheduler's view:

```bash
sinfo -N -o "%N %t %C %G"     # every node, its state and GPUs
squeue                         # what's queued and running
cuttle arm list                # every Arm and how far it got joining
```

Restarting a service is safe: `sudo systemctl restart ccc-head-api`. Signed-in Users stay signed in.

## When a command fails

`cuttle` keeps a full log of every run in `~/.ccc/logs/`, one file per run. That's where the detail is
when setup stops with a one-line error. Add `--debug` to any command for the full traceback:

```bash
cuttle --debug setup --head
```

## Common problems

### Setup refuses to start

It checks the box first and stops before changing anything. The message says what's wrong: not
Ubuntu 24.04, not x86_64, `nvidia-smi` doesn't work, or the GPUs aren't all the same model.

### Setup says a package version is gone

CCC installs exact versions of Slurm and a few other packages. If the repository has dropped one,
setup names the package, the version, and the file where the pin lives. Updating the pin is a code
change, and the cluster needs re-testing on the new version afterwards.

### Setup failed halfway

Re-running `cuttle setup --head` is safe, and usually finishes the job. If the box is in a muddle,
clear it and start again:

```bash
cuttle teardown --full
cuttle setup --head
```

### A "service down" email

Run the status command above and read that service's log. Most of the time a restart brings it back.
If it keeps dying, the log says why.

### A new Arm can't join

The donor's `cuttle setup --arm` hangs, then times out. Their machine can't reach the coordinator
address. See [[Mesh#When the Mesh is down|When the Mesh is down]].

### An Arm shows "enrolled, NOT ADMITTED"

`cuttle arm authorize` stopped waiting before the donor finished. Run `cuttle arm admit <name>`.

### Arms stopped taking Dives after a munge rotation

Expected. Each owner runs `cuttle arm sync-key` on their machine. `cuttle munge rotate` printed the
list.

### Setup says "Guises are held back" or "Private per-Dive /tmp is held back"

Some Arms haven't picked up a newer part of the setup yet. Setup names them. Have each owner re-run
`cuttle setup --arm` (or `cuttle update`) on that machine, then re-run `cuttle setup --head` here.

### A User is locked out

Too many wrong passwords block that username for 30 minutes. Either wait, or clear all the counts
with `sudo systemctl restart ccc-head-api`. If they've forgotten the password, `cuttle user reset
<name>`.

### You've lost the Admin password

```bash
cuttle user reset admin
```

It prints a one-time password; sign in and pick a new one.

### Adding a User fails for lack of space

Every Tidepool is reserved in full when the User is added. Free space in the Tidepool folder, or move
it to a bigger disk. Re-running setup with a smaller size only affects Tidepools made after that;
existing ones keep their size.

### Someone's Dive needs stopping

```bash
cuttle dive end <id>
```

On the Head this ends anyone's Dive. To stop a whole Arm taking work, use `cuttle arm retract <name>`
(and `cuttle arm return <name>` to reopen it).

### The Dashboard warns about its certificate

On a machine CCC was installed on, it shouldn't. Re-run `cuttle setup --client`
there. On a phone or a borrowed laptop the warning is expected: CCC never installed its certificate
authority there.

### The wiki is missing or out of date

The wiki is built during `cuttle setup --head` and `cuttle update`, into `/opt/ccc/head-api/wiki`.
Run `cuttle update` after pulling new pages. The build fetches Quartz from GitHub the first time, so
it needs internet.

## Related

- [[Admin/index|Admin]]: parent
- [[Head setup]]: services, paths, teardown
- [[Mesh]]: Arms and the coordinator
- [[Alerts and email]]: the health checks
- [[Users and access]]: passwords and lockouts
- [[Troubleshooting]]: problems Users run into
