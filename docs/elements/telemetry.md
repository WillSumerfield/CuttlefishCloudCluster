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

**Check there is one** (masked to its last four characters):

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
| `cuttle wandb show` | Whether one is stored, masked. |
| `cuttle wandb clear` | Forget it. |

## Behavior

### The Head holds the key, and only the Head

Your key is stored encrypted on the Head, under your account. It never touches the disk of the box
you typed it on, and no other User can read it.

At submit time the Head injects it into your Spawn's sandbox. It uses **its own stored copy**, never
anything the client sent — a client cannot supply a key, and one that tries has it dropped.

What the client *does* get to choose is the run's identity: `--project` and `--entity`. That is the
whole of it.

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

Rogue detection is scoped to the **Head's own machine** and deliberately not extended to donors'
boxes. An Arm is a machine CCC does not own, and watching one the way the Head watches itself is a
separate decision that has not been made. Fleet-wide per-Arm telemetry is not built — see
[TODO](../TODO.md).
