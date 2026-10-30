---
title: Dives
---
A [[Glossary#Dive|Dive]] is a GPU session. You pick a folder from your [[Tidepool]], CCC copies it to
a free GPU, and you get a shell there. You work in that copy, and your changes flow back to your
Tidepool every minute. The Dive keeps its GPU until you end it or its time runs out.

## A first Dive

Say your project lives in `~/fractals` in your Tidepool and you want one GPU for four hours:

```bash
cuttle dive fractals --time 4h --vram 16
```

`cuttle` takes a quick snapshot of your Tidepool's system, queues the Dive, waits for a GPU, and drops
you into a shell inside `~/fractals` on whichever machine it landed on. The first line tells you
where you are:

```
Dive 58 on alexpc · GPU 0 (RTX 4090) · ends 14:30 · ~/fractals mirrors back to your Tidepool every minute
```

The prompt changes to `in a dive · alice@alexpc`, so you can always tell a Dive shell from a Tidepool
shell. Now run your code:

```bash
python train.py
```

When you're done, end it so the GPU goes back to the pool:

```bash
cuttle dive end 58
```

You can also start a Dive from inside your Tidepool. There, `cuttle dive` with no folder uses the
folder you're in, and `cuttle` moves you over to the Dive once it's ready.

## What you choose

You can also [start a Dive from the Queue](dashboard:/). In **Start a Dive**, browse or type your
Tidepool folder. Its saved settings load automatically for you to review and edit. For a new folder,
you must choose a time limit and total VRAM; GPU count starts at one. GPU count, read-only folders,
an environment file and Features are all visible in the form. Starting saves your choices to `cuttle.toml`,
just like the CLI, and opens the Dive page with progress and connection instructions. You connect
from your terminal or editor; the Dashboard does not open a browser shell.

| Flag | What it does | Default |
| --- | --- | --- |
| `MAIN` (the folder) | The folder to work in. It's copied over, and changes come back. | the current folder, inside your Tidepool |
| `--time` | How long the Dive may run: `90m`, `4h`, `2d`, `1d12h`. At most `14d`. | none: you must give it once |
| `--gpus` | How many GPUs, all on one machine. | 1 |
| `--vram` | Total GPU memory you need, in GB, across all your GPUs. | none: you must give it once |
| `--dep` | Another folder to bring along, read-only. Repeat it for more. | none |
| `--env` | A dependency file in the main folder to build a Python environment from. See [[Environments]]. | none |
| `--feature` | A special capability an Arm must have. See [[Arm features]]. | none |
| `--vscode` | Open the Dive in VS Code instead of a terminal. | off |

Folder names are from your Tidepool home: `fractals`, `~/fractals` and `/home/alice/fractals` all mean
the same folder.

### cuttle.toml remembers your settings

The first time you start a Dive on a folder, CCC writes your choices into `cuttle.toml` in that
folder:

```toml
# cuttle dive settings. `cuttle dive` here reuses them; flags override and rewrite this.
deps = ["datasets/mnist"]
gpus = 1
time = "4h"
vram = 16
env = "pyproject.toml"
```

Next time, `cuttle dive fractals` is enough. Any flag you give overrides the file and is saved back
into it. You can also edit the file by hand.

### Main folder and dep folders

- The **main folder** is your working copy. Changes there are copied back to your Tidepool every
  minute, and once more when the Dive ends. Files you delete in the Dive are deleted in your Tidepool
  too.
- **Dep folders** (`--dep`) are read-only inputs, like a shared dataset or a library you're working on.
  Nothing comes back from them.

While a Dive holds a main folder, that folder is **read-only in your Tidepool**, so the two copies
can't fight. You can still read it, and the Files page marks it "locked". It unlocks when the Dive
ends. You can't start a second Dive on the same main folder until the first one ends, but dep folders
can be shared by as many Dives as you like.

### What gets copied

Only the main folder, the dep folders, your system packages (anything you `apt install`ed) and a few
dotfiles (`.zshrc`, `.gitconfig`, `.vimrc`, `.tmux.conf`, your SSH config and public keys, and a few
more). Never your private keys or credential files. Your SSH agent is forwarded instead, so
`git push` still works.

Some folders are never copied: `.venv`, `venv`, `__pycache__`, `.cache`, `node_modules`,
`.mypy_cache`, `.pytest_cache` and `.ipynb_checkpoints`. To skip more, put a `.cuttleignore` file in
the folder. It uses rsync's filter syntax, one pattern per line:

```
data/raw/
*.ckpt
```

A Dive can copy at most **25 GB** in total. If yours is bigger, CCC refuses it and tells you which
folders are the big ones.

## Waiting in the queue

If no GPU fits right now, your Dive waits. `cuttle` shows "queued" while it waits, then "staging your
folders" while it copies them. Pressing Ctrl-C stops the waiting but keeps your Dive in the queue. Come
back to it later with `cuttle dive attach`.

To see where you are:

```bash
cuttle queue       # every Dive on the cluster, running and waiting
cuttle dive status # only yours
```

or open the [Queue](dashboard:/) in the Dashboard, which numbers the waiting Dives in order.

### Who goes first

Mostly, whoever has waited longest. [[Karma]] adjusts that order: someone who has used a lot of GPU
time lately is treated as if they'd joined the queue later, by up to a day. On an idle cluster Karma
makes no difference at all.

A Dive also only goes where it fits. Asking for a lot of VRAM, several GPUs or a rare
[[Arm features|Feature]] means fewer machines can take it, so it may wait longer even when other GPUs
are free. When several machines are free, the fastest one is picked first.

Your Dive may run on one of the [[Arms]] or on the Head's own GPUs. It works the same either way.

## Attaching and detaching

Leaving a Dive never ends it. `exit`, a dropped Wi-Fi connection or closing your laptop all leave
it running, holding its GPU, and still using your Karma. To get back in:

```bash
cuttle dive attach 58
```

With only one Dive open, `cuttle dive attach` is enough. Use `tmux` inside the Dive for anything that
should keep running while you're away.

Each Dive also gets its own SSH alias, your cluster name plus the Dive's number (for example
`mycluster-58`), for VS Code or `scp`. `cuttle dive attach 58 --vscode` opens it in VS Code for you.

## Ending

A Dive ends in one of three ways:

- **You end it**: `cuttle dive end 58`, or the End button on the Queue or the Dive's page in the
  Dashboard.
- **Its time runs out.** A few minutes before (a quarter of the total, between 30 seconds and 5
  minutes), your attached shell prints a warning. Then the Dive ends.
- **Something goes wrong underneath it**: the machine is switched off, drops off the network, or its
  owner takes it back.

Every time, your main folder gets one last copy back to your Tidepool. Everything else on that
machine is wiped: files outside the main folder, the built environment, anything left in `/tmp`.

### Outcomes

Finished Dives show up in [History](dashboard:/history) (or `cuttle history`) with one of these outcomes:

- **ended**: someone ended it. You pay for the time it held the GPU.
- **timed out**: it ran to the end of its `--time`.
- **lost**: it stopped early without anyone ending it, usually because the machine went away. You pay
  for what it used, and CCC never restarts it on its own.
- **settling**: it has finished moments ago, and CCC is still working out the cost. This takes about half a
  minute.

If an Arm's owner takes their machine back while your Dive is on it, your Dive gets a short grace
period and then stops, and you pay nothing for it.

## The Dive page

Click any Dive in the Queue or History to open its page (`/dive/<id>`). It shows who started it, the
main folder, which machine it ran on, GPUs, VRAM, time limit, when it started and finished, and its
cost (shown as "still metering" while it runs). For your own Dive it also shows how to connect while
it's live, and an End button.

## Related

- [[Using CCC/index|Using CCC]]: parent
- [[Tidepool]]: where a Dive's folders come from and go back to
- [[Environments]]: Python dependencies and installs in a Dive
- [[Files]]: copying files into a running Dive
- [[Karma]]: what a Dive costs and how it affects the queue
- [[CLI]]: all the `cuttle dive` commands
- [[Dashboard tour]]: the Queue, History and Dive pages
- [[Arm features]]: what `--feature` asks for
