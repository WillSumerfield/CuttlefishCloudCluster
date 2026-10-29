---
title: Tidepool
tags: [leaf]
---
Your [[Glossary#Tidepool|Tidepool]] is your own space on the [[Glossary#Head|Head]]: a folder that
holds all your work, plus a small Linux computer that is always running and that you can SSH into.
It has no GPU. Use it to edit code, organise data, run `git`, and anything else that doesn't need a
GPU. When you need one, you start a [[Dives|Dive]] from it.

## Getting in

```bash
cuttle tidepool
```

That's it. You land in a shell in your home folder. The prompt says where you are, for example
`in a tidepool · alice@mycluster`. On the way in, `cuttle` tells you how full your Tidepool is and
lists any Dives you have open.

Type `exit` to leave. Your Tidepool keeps running, and so does anything you left running in it
(use `tmux` if you want to come back to a live terminal later).

### Plain SSH, scp and VS Code

`cuttle login` sets up everything plain SSH needs:

- a key only for CCC, `~/.ssh/ccc_ed25519`, uploaded to the Head for you
- a host alias named after your cluster, in `~/.ssh/ccc.config`, with an `Include ccc.config` line
  added to the top of your `~/.ssh/config`
- the Tidepool's host key, pinned in `~/.ccc/known_hosts`, so you never get a "trust this host?"
  prompt

So if your cluster is called `mycluster`, all of these work:

```bash
ssh mycluster
scp results.csv mycluster:proj/
```

In VS Code, use Remote-SSH and pick the host `mycluster`. The Account page in the
[[Glossary#Dashboard|Dashboard]] shows the exact host name.

Your SSH agent is forwarded, so `git push` from your Tidepool can use the keys on your own machine.

Plain `ssh` can't do `cuttle upload` / `cuttle download` from inside (see [[Files]]). For that, come in
with `cuttle tidepool`.

### More than one machine

Run `cuttle login` on each machine you use; each one adds its own key. To let in a key you already
have:

```bash
cuttle tidepool keys add ~/.ssh/id_ed25519.pub
cuttle tidepool keys            # list them
cuttle tidepool keys remove 3   # by id
```

You can do the same from the Account page in the Dashboard.

## What's in it

- **Your home folder.** Everything you keep lives here: code, data, results, dotfiles, caches.
- **sudo.** You're root inside your own Tidepool, so `sudo apt install ffmpeg` works. It doesn't give you
  any power over the Head itself or anyone else's Tidepool.
- **zsh with a ready-made prompt.** Your own `~/.zshrc` is read after the default one, so you can
  change anything.
- **Tools.** Python 3.12, `uv`, `micromamba`, `git`, `git-lfs`, `tmux`, `vim`, `nano`, `htop`, `rsync`,
  build tools and the CUDA compiler (`nvcc`), among others. No Python packages are installed: see
  [[Environments]].
- **Internet access**, for `git clone`, `uv` downloads and so on.

## Limits

- **No GPU.** None at all, not even a small one. GPU work runs in a [[Dives|Dive]].
- **A fixed size.** Every User gets the same size, set by your Admin (200 GB unless they changed it).
  Run `cuttle tidepool status` or look at the Account or Files page to see how full yours is. When
  it's full, writes fail. Nothing else breaks, but you'll need to delete something.
- **A CPU and memory cap.** Your Tidepool can use a few CPUs and some memory (4 CPUs and 16 GB unless
  your Admin changed it). Going over the memory cap kills your own processes, not anyone else's.
- **Folders a Dive is using are read-only.** While a Dive holds a folder, you can read it in your
  Tidepool but not change it. It unlocks when the Dive ends. See [[Dives]].

## What stays and what goes

| You do this | Your home folder | Things you installed with `apt` | Running programs |
| --- | --- | --- | --- |
| Disconnect (`exit`, close the laptop) | kept | kept | keep running |
| `cuttle tidepool restart` | kept | kept | stopped |
| `cuttle tidepool reset` | kept | **lost** | stopped |

Restart is the fix for a stuck Tidepool. Reset rebuilds it from the clean base image. Use it if
you've broken your system packages. Both are on the Account page too.

When your Admin upgrades CCC, your Tidepool may be rebuilt from a newer base image. Your home is
kept, and CCC reinstalls your `apt` packages by name afterwards. A `reset` clears that list, so it
won't bring them back.

Packages you `apt install` inside a Dive come back to your Tidepool when the Dive ends. See
[[Environments]].

## Related

- [[Using CCC/index|Using CCC]]: parent
- [[Dives]]: GPU work starts from your Tidepool
- [[Files]]: getting files in and out
- [[Environments]]: what's installed and how to add more
- [[Dashboard tour]]: the Account page manages your Tidepool
- [[Getting started]]: `cuttle login` sets up SSH for you
