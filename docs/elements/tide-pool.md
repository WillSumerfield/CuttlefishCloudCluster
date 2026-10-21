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

**It keeps nothing.** When the session ends, the directory you were working in is gone — only
declared outputs come home.

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

**Open it in VS Code instead of a terminal:**

```sh
cuttle run --tidepool --time 30 --vram 24 --vscode
```

See [Editing in VS Code](#editing-in-vs-code) below.

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

### The shell ends abruptly at `--time` — your work does not

At the limit your shell closes mid-keystroke, with no chance to finish what you were doing. So CCC
interrupts the session with a warning before the cutoff (a quarter of the total, at most 5 minutes and
at least 30 seconds ahead), so the ending is not a surprise.

**Your declared outputs are not lost, though.** The Tide Pool gets a **grace period** at the limit —
ten minutes between the signal that ends it and the kill that enforces it — and it spends that window
sending declared outputs home, as any Spawn's do. Everything else is gone.

### You are told which machine you got

Like any Spawn, you declare what you *need* — `--time`, `--vram`, `--feature` — and you are placed.
You cannot name the Arm you want. You are told which one you were given.

### Declaring outputs is optional here

`--output` is required for an ordinary Spawn and merely offered for a Tide Pool, with a warning if
you decline. Anything you leave behind in the working directory is gone when the session ends — if
you made something you want to keep, declare it.

### Editing in VS Code

Every session gets an ssh alias on this machine — `ccc-tp-<id>` — and it is printed when you attach. Anything that speaks ssh can use
it:

```sh
ssh ccc-tp-58
code --remote ssh-remote+ccc-tp-58
```

`--vscode` does the last one for you, opening straight at your project directory instead of giving
you a terminal here. The session runs exactly as it would otherwise; you are simply not the one
holding it.

You never handle a key or a port. cuttle writes `~/.ssh/ccc.config` and adds a single `Include` line
to your own `~/.ssh/config` — it never edits an entry you wrote. That file is regenerated from your
live sessions every time you open one, so a Tide Pool that has ended leaves no alias and no key
behind.

The VS Code server (~150 MB) unpacks inside the Pod, on disk rather than in the small in-memory home,
and it goes with the Pod when the session ends. Extensions install per session.

### If it never gets placed

The request times out after an hour queued and fails loudly rather than hanging forever.
