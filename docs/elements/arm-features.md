# Arm Features

## What it is

Some software cannot simply live on every machine in the cluster. It is a 40 GB install, or it is
licensed to one particular box rather than to you. A **Feature** is a name for one of those: a
capability an [Arm](arms.md) reports having.

There are two names today — `vlearn` and `isaaclab` — and the list is **built into CCC**. It is not
a tag anyone curates. CCC has to know what each name *means*: at minimum where the software is
installed, and for some, the whole runtime environment a Spawn needs to load it.

You ask for a Feature the same way you ask for VRAM: as a **need**, not a machine name.

```sh
cuttle run sim.py --time 30 --vram 16 -o results --feature isaaclab
```

That widens or narrows which Arms your Spawn can land on, and nothing else. You still never choose
the box.

## Typical use

**See what exists:**

```sh
cuttle feature list
```

Runs anywhere, needs no login — the catalog is a constant.

**Require one:**

```sh
cuttle run train.py --time 60 --vram 24 -o out --feature vlearn
```

Repeatable, if you need more than one.

**Get a starting `environment.yml`** for the system libraries a Feature's software links against:

```sh
cuttle feature show vlearn > environment.yml
```

Then add your own dependencies to it. See [`examples/vlearn/`](../../examples/vlearn/) for a worked
project.

## Commands

| | |
|---|---|
| `cuttle feature list` | The Features CCC knows how to run. |
| `cuttle feature show <name>` | A starter `environment.yml` for that Feature's system libraries. |

## Behavior

### Two shapes of Feature

**`isaaclab` is the first shape**: a heavy install that is not everywhere. Asking for it buys you
placement on a machine that has it, plus `$ISAACLAB_PATH` in your environment pointing at where it
lives. You drive the software yourself from there. That is a complete answer for something a
submitter runs directly.

**`vlearn` is the second shape**: software whose licence is bound to the *machine*, so the copy that
runs has to be the Arm's own. It also carries a **runtime profile** — CCC arranges the package,
the bind mounts, the working directory, the caches and the environment variables your Spawn needs,
so your script contains none of it.

That last part has to be CCC's job rather than yours. `LD_LIBRARY_PATH` is read by the dynamic loader
once, at `exec` — nothing running inside your Spawn can set it for itself. Whatever arranges it has
to run outside your process.

### Writing against a Feature

If a Feature has a runtime profile, `cuttle feature show` gives you the system libraries its shared
objects need, as an `environment.yml` you extend. It is fetched from the Head rather than generated
locally, so there is exactly one copy of that list and it is the same one CCC itself builds against.

Not every Feature has one — a Feature can be nothing but placement plus an install path, which needs
no Environment at all.

### Licences

A licence belongs to the *machine*, not to you and not to the software. An Arm has at most one set,
covering every Feature it offers, because a licence is bound to hardware and an Arm is one machine.

Your Spawn runs under the Arm owner's licence and never carries its own. This is why a licence-only
Feature still constrains where you can run even though every machine in the fleet could install the
software: the install is not the scarce thing, the licence is.

### What happens when it goes wrong

- **You asked for a Feature no enrolled Arm has** — refused at submit, immediately, the same as an
  impossible VRAM request. Not queued forever waiting for a machine that may never exist.
- **An Arm declared a Feature it cannot actually serve** — the Spawn fails with a sentence naming the
  Arm and what was wrong, written into your Spawn's stderr. It reads as a mismatch rather than as an
  `ImportError` you would blame on your own code.

### For Arm owners

At `cuttle setup --arm` you are asked, one Feature at a time, whether this box has it and where it
is installed. The path must be absolute — it is read back on the Arm by a job script whose working
directory is not one you can see from here.

Say yes only for what is really there. A Spawn asking for a Feature can be sent nowhere else, so a
wrong answer routes someone's job to a machine that cannot run it.

CCC checks the path against what the software actually opens, at the prompt, and re-asks if it does
not look right — rather than letting a stranger's Spawn die inside a shared library hours later, on a
box you are not watching.

What CCC needs from your install is copied once, at setup, into CCC's own state under
`/var/lib/ccc/features/`. Spawns bind that read-only. They never touch your directory: a Spawn runs
as a [Guise](isolation.md) with no relationship to you, and cannot traverse your home.

Features that have a product key ask for its path too. The key is mirrored into this box's own state
at `0640` and **never leaves the Arm** — the Head never sees it, and no ordinary Spawn ever does.
