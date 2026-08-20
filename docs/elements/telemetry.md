# Telemetry

## What it is

Two unrelated things share this name, so: the one you care about is **experiment tracking**.

You give the Head your Weights & Biases key, once. From then on every [Spawn](spawns.md) you submit
logs to *your* W&B account — from the CLI, from the [Dashboard](../Dashboard.md), from a machine you
have never used before. You never put a key in your code, in an environment variable, or in a config
file, and you never have to remember which machine has one.

The other thing on this page is the Head's own health monitor, which is the operator's business.

## Typical use

**Store your key:**

```sh
cuttle wandb set
```

**Check which one is stored** (its last four characters — the key itself cannot be read back):

```sh
cuttle wandb show
```

**Send one Spawn to a particular project:**

```sh
cuttle run train.py --time 60 --vram 24 -o out --manifest pyproject.toml --project my-ablations
```

**Stop logging entirely:**

```sh
cuttle wandb clear
```

## Commands

| | |
|---|---|
| `cuttle wandb set` | Store your key on the Head. |
| `cuttle wandb show` | Whether one is stored, and its last four characters. |
| `cuttle wandb clear` | Forget it. |

## Behavior

### The Head holds the key, and only the Head

Your key is stored on the Head, under your account, encrypted **with your password**. It never
touches the disk of the box you typed it on. No other User can read it, no Admin can read it, and the
Head itself cannot read it except while you are logged in — see
[Security](../Security.md) for what that does and does not buy.

At submit time the Head injects it into your Spawn's sandbox. It uses **its own stored copy**, never
anything the client sent — a client cannot supply a key, and one that tries has it dropped.

What the client *does* get to choose is the run's identity: `--project` and `--entity`. That is the
whole of it.

### You cannot read your key back

Once stored, a key is **write-only**. `cuttle wandb show` and the Dashboard's Account page give you
its last four characters and nothing more, which is enough to tell the key you meant from some other
key. There is no reveal, no "print it in full", and no password prompt that would produce one.

That is not caution for its own sake — it is what lets the key be kept under your password instead of
under something the Head can reach on its own. If you need the key itself, get it from W&B; if you
are not sure the stored one is right, paste it again.

### Two things destroy it

**An Admin password reset.** Your key was encrypted with your old password, and a reset means nobody
has that password any more — including the Head. Your stored keys are deleted rather than left as
ciphertext nothing can open. After a reset, paste your key again.

**Nothing else.** Changing your own password with `cuttle passwd` keeps every key: the Head re-wraps
them under the new password while you are logged in. A [restore from backup](backup-and-recovery.md)
keeps them too.

### A Spawn pinned from the Head gets no key

`cuttle run --node`, which only an operator standing on the Head can run, submits with no login
session behind it — so there is nothing to decrypt your key with, and that Spawn runs with W&B
disabled. The command says so when you have a key stored. Submit through `cuttle run` or the
Dashboard to log metrics.

### No key means no logging, and you are told

If you have not set one, your Spawns run with W&B **disabled** — explicitly, rather than by
accident. This matters more than it sounds: an empty environment would make `wandb.init()` *fail*
rather than skip, so your script would die instead of running.

You are told at submit that your Spawns will log nothing, rather than discovering it afterwards.

### Your key does not leak sideways

A Spawn's environment is visible from inside its own [Pod](isolation.md) and nowhere else. One
Spawn cannot read another's environment, on the same machine or any other — which is exactly the
thing an injected API key would otherwise be vulnerable to.

## Admin — on the Head

`cuttle setup --head` installs **`ccc-monitor`**, a systemd timer that runs three checks every
5 minutes and emails you when one fires:

- **Disk** — the Head's root filesystem past 85% full.
- **Services** — any of the Head's services not active (the scheduler, the coordinator, the APIs, the
  Karma tick).
- **Rogue usage** — GPU or CPU above 10% busy on the Head with **no Slurm job running on that node**,
  sustained over three consecutive checks. That is compute happening outside the scheduler: someone
  running work on the Head by hand, or something that should not be there.

Each check alerts independently and at most once a day, so a stuck disk alert cannot suppress a later
rogue-usage one.

SMTP credentials are asked for during `cuttle setup --head` and can be skipped — the checks still run
and print to `journalctl -u ccc-monitor`, they just cannot email. Fill them in later by re-running
setup. See the [SMTP runbook](../SMTP-Runbook.md).

Rogue detection watches the **Head's own machine**. It does not watch donors' boxes: an Arm is a
machine CCC does not own, and watching one the way the Head watches itself is not something CCC does.
