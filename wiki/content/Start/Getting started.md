---
title: Getting started
---
This guide takes you from a brand-new account to your first GPU session and back.
You'll need a Linux computer (Ubuntu works) where you can use `sudo`.

## 1. Get an account

Ask your [[Glossary#Admin|Admin]] for an account. They create it on the Head with:

```bash
cuttle user add <your-username>
```

That prints a temporary password, which they pass on to you. You'll pick your own the first time you
sign in. Ask them for two more things while you're at it: the **Head's address**, and the
**installer bundle** (`cuttle-installer.tar.gz`).

You can only reach the Head from certain networks, usually your lab's network or campus wifi. Your
Admin will tell you which. Off those networks, nothing below will connect.

## 2. Install `cuttle`

Copy the bundle to your computer, then unpack and run it:

```bash
tar xzf cuttle-installer.tar.gz
cuttle-installer/install.sh
```

It installs [uv](https://docs.astral.sh/uv/) if you don't have it, then installs `cuttle`. It asks for
your `sudo` password so it can teach your computer the Head's name and trust the cluster's
certificate, so your browser opens the Dashboard without a warning.

If `cuttle` isn't found afterwards, open a new terminal (the installer prints the line to add if that
doesn't help).

You only need the bundle once. When your Admin upgrades CCC, `cuttle` notices the next time you run
it, says `Updating cuttle to the Head's build…`, installs the new version and carries on with your
command. You don't have to do anything.

## 3. Log in

What you run next depends on what the installer printed at the end.

If it said `next -> cuttle login`, your bundle already knows the Head:

```bash
cuttle login
```

If it said `next -> cuttle setup --arm ...` / `cuttle setup --client`, your bundle is a generic one. Run:

```bash
cuttle setup --client
```

It asks for the Head's address, sets your computer up to reach it, and then logs you in.

Either way, you type your username and the temporary password, then choose a new password. Logging in
also sets up SSH for you: it makes a key at `~/.ssh/ccc_ed25519` and lets it into your Tidepool.

Check it worked:

```bash
cuttle whoami
```

## 4. Open the Dashboard

```bash
cuttle dashboard
```

This opens the Dashboard in your browser. Sign in with the same username and password. Have a look
around: the Queue shows who's using the GPUs right now, and Account shows your Tidepool and your
[[Glossary#Karma|Karma]]. See [[Dashboard tour]] for the rest.

## 5. Enter your Tidepool

```bash
cuttle tidepool
```

You're now in your [[Glossary#Tidepool|Tidepool]], your own always-on computer on the Head. The
prompt starts with `in a tidepool`. Your home folder here is your Tidepool's storage: anything you put
in it stays. You have `sudo`, so `sudo apt install` works. There's no GPU here; that's what Dives are
for.

Type `exit` to leave. Your Tidepool keeps running, along with anything you left running in `tmux`.

The login also wrote an SSH alias named after the cluster, so plain `ssh <cluster name>`, `scp` and VS
Code's Remote-SSH reach your Tidepool too. More in [[Tidepool]].

## 6. Put a project in it

From your own computer, copy a project folder up:

```bash
cuttle upload ./myproject
```

It lands in your Tidepool as `~/myproject`. You could also `git clone` it from inside the Tidepool, or
drag it onto the Files page in the Dashboard. See [[Files]].

## 7. Start your first Dive

```bash
cuttle dive myproject --time 1h
```

This asks for a one-hour [[Glossary#Dive|Dive]] with `~/myproject` as its main folder. CCC copies the
folder to a free GPU, waits for it to be ready, and drops you into a shell there. The prompt now
starts with `in a dive`, and a line at the top tells you which GPU you got and when the Dive ends.

Try `nvidia-smi` to see your GPU.

Some things to know:

- `--time` is required the first time. It's saved in `myproject/cuttle.toml`, so next time
  `cuttle dive myproject` is enough. The longest you can ask for is 14 days (`14d`).
- If the queue is busy, you wait. Press Ctrl-C to stop waiting: the Dive stays in the queue, and
  `cuttle dive attach` gets you in once it starts.
- Your changes in `~/myproject` are copied back to your Tidepool every minute. Meanwhile that folder
  is read-only in your Tidepool.
- Need Python packages? Add `--env requirements.txt` (or `pyproject.toml`) and CCC builds a `.venv` for
  you. See [[Environments]].

## 8. Leave, come back, and end it

Typing `exit` leaves the Dive, but **doesn't end it**. It keeps its GPU, and keeps costing Karma,
until its time runs out or you end it. To go back in:

```bash
cuttle dive attach
```

When you're done:

```bash
cuttle dive end
```

It asks you to confirm, then frees the GPU. The main folder gets one last copy back to your Tidepool
and becomes writable there again. Run `cuttle dive status` any time to see your open Dives.

## Next steps

- [[Dives]]: dep folders, more GPUs, VRAM, and `cuttle.toml`.
- [[Files]]: moving files between your computer, your Tidepool and a Dive.
- [[Karma]]: how turns are shared out.
- [[CLI]]: every `cuttle` command. Or run `cuttle` on its own for a menu.

## Related
- [[Start/index|Start]]: parent
- [[Overview]]: the big picture first
- [[Tidepool]]: your own space on the Head
- [[Dives]]: GPU sessions in detail
- [[Files]]: upload, download and the Files page
- [[Troubleshooting]]: when something goes wrong
