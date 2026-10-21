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
cuttle run --time 30 --vram 16 --feature isaaclab
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
cuttle run --time 60 --vram 24 --feature isaaclab
```

Repeatable, if you need more than one.

**See what CCC arranges** for a Feature — the system libraries it builds, and which Python an
Environment has to be:

```sh
cuttle feature show vlearn
```

## Commands

| | |
|---|---|
| `cuttle feature list` | The Features CCC knows how to run. |
| `cuttle feature show <name>` | What CCC arranges for that Feature, and which Python it needs. |

## Behavior

### Two shapes of Feature

**`isaaclab` is the first shape**: a heavy install that is not everywhere. Asking for it buys you
placement on a machine that has it, plus `$ISAACLAB_PATH` in your environment pointing at where it
lives. You drive the software yourself from there. That is a complete answer for something a
submitter runs directly.

**`vlearn` is the second shape**: software whose licence is bound to the *machine*, so the copy that
runs has to be the Arm's own. It also carries a **runtime profile** — CCC arranges the package,
the bind mounts, the working directory, the caches and the environment variables your Spawn needs,
so your code contains none of it.

That last part has to be CCC's job rather than yours. `LD_LIBRARY_PATH` is read by the dynamic loader
once, at `exec` — nothing running inside your Spawn can set it for itself. Whatever arranges it has
to run outside your process.

### Profiled Features are refused for now

A runtime profile attaches the Feature's software to an [Environment](environments.md) — it probes
the interpreter for its ABI and site-packages — and nothing builds one in 2.0 until Dives. So a
request for a profiled Feature (`vlearn`) is refused at submit, before any GPU is held. A Feature with
no profile (`isaaclab`) is placement plus an install path and still works.

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

A Feature with a product key also needs a **Licence directory**: a path CCC creates, where the
machine's activated licence lives, and which every Spawn asking for that Feature reads. Setup will not
finish without one. Nothing activates a licence on Arm admit right now — activation is being rebuilt
as a Head-local job — so a licensed Feature cannot activate meanwhile.
