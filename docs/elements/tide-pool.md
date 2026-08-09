# Tide Pool

## What it is

A **Tide Pool** is a live shell on real cluster hardware.

It is for the times when you do not want to run a script — you want to poke at something. Check
whether the GPU is what you think it is, run a REPL, try three variations of a command, debug a
crash that only happens on the actual card.

Underneath it is an ordinary [Spawn](spawns.md): same queue, same eligibility rules, same Karma. The
difference is that instead of running your script, it drops you into a shell inside the sandbox and
waits.

**It bills for as long as you hold it.** Closing your laptop does not end it.

**It keeps nothing.** When the session ends, the directory you were working in is gone — only the
day-long [Recovery Window](storage.md#the-recovery-window) stands between you and losing it. If you
want a shell over a folder that is still there next time, that is a [Workspace](workspaces.md).

## Typical use

**Get a shell on a machine with a 24 GB card, for half an hour:**

```sh
cuttle run --tidepool --time 30 --vram 24
```

You wait for placement, and then you are in.

**Debug against your project's environment** — name a dependency file and the shell gets the
[Environment](environments.md) your Spawn would have run in, built the same way. This uploads the
working directory too, since the environment is built from what you send:

```sh
cuttle run --tidepool --time 30 --vram 24 --manifest pyproject.toml
```

This is the one place `--manifest` is optional. Leave it off and you get a bare shell with no
Environment at all — which is the right answer when you are poking at the machine rather than at your
code.

**Come back to one you left:**

```sh
cuttle run --tidepool <id>
```

**Stop paying for it:**

```sh
cuttle cancel <id>
```

## Behavior

### Disconnecting does not end it

A clean `exit`, a dropped Wi-Fi connection, Ctrl-C — none of them cancel the Tide Pool. It is exactly
like Ctrl-C on `cuttle run --follow`, which stops the log stream and not the Spawn.

It keeps billing Karma until either `--time` runs out or you run `cuttle cancel <id>`. Every way out
of the session prints that command on the way past.

### It ends abruptly at `--time`

An interactive Spawn gets **no grace period** at the limit — unlike a batch job, there is no
save-and-exit moment. So CCC interrupts your session with a warning before the cutoff (a quarter of
the total, at most 5 minutes and at least 30 seconds ahead) to give you time to get anything you care
about out.

### You are told which machine you got

Like any Spawn, you declare what you *need* — `--time`, `--vram`, `--feature` — and you are placed.
You cannot name the Arm you want. You are told which one you were given.

### Declaring outputs is optional here

`--output` is required for an ordinary Spawn and merely offered for a Tide Pool, with a warning if
you decline. Anything you leave behind in the working directory is still covered by the
[Recovery Window](storage.md#the-recovery-window) for a day after the session ends — but if you
made something you want to keep, declare it.

If you are reaching for the Recovery Window more than once, you want a [Workspace](workspaces.md)
instead: it keeps the whole directory, permanently, with no window to miss.

### If it never gets placed

The request times out after an hour queued and fails loudly rather than hanging forever.
