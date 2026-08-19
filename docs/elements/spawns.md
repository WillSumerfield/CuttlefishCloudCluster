# Spawns

## What it is

A **Spawn** is one run of your code on the cluster.

You point `cuttle run` at a script in the directory you are standing in. CCC uploads that directory,
builds whatever Python you declared, finds a machine with a free GPU big enough, runs your script
inside a sandbox, and copies your results back down onto your own machine when it is done.

The thing worth internalising: a Spawn runs **once**. If your script crashes, or the machine it
landed on is taken back by its owner mid-run, that Spawn is over. Nothing silently retries somewhere
else. So "Spawn 412" always names one attempt — what it cost and what it produced both refer to the
same single run.

## Typical use

**Run a training script for an hour on a 24 GB card:**

```sh
cuttle run train.py --time 60 --vram 24 --output checkpoints --manifest pyproject.toml
```

**Pass arguments through to your own script** — everything after `--` is yours:

```sh
cuttle run train.py --time 60 --vram 24 -o checkpoints --manifest pyproject.toml -- --epochs 5 --lr 3e-4
```

**Watch it happen** rather than walking away:

```sh
cuttle run train.py --time 60 --vram 24 -o checkpoints --manifest pyproject.toml --follow
```

**Ask for a machine that has particular software on it** (see [Arm Features](arm-features.md)):

```sh
cuttle run sim.py --time 30 --vram 16 -o results --manifest pyproject.toml --feature isaaclab
```

**Leave the flags off entirely** and let it ask you:

```sh
cuttle run
```

Same submission either way — the prompts fill in exactly the arguments above, show you a preview,
and ask before submitting.

## Commands

| | |
|---|---|
| `cuttle run <script>` | Submit a Spawn. |
| `cuttle status <id>` | Queued, running, or done — and with what exit code. |
| `cuttle logs <id>` | Print its stdout and stderr. `--follow` to stream. |
| `cuttle queue` | Everything pending or running on the cluster, and whose it is. |
| `cuttle history` | Past Spawns, with their cost and outcome. |
| `cuttle cancel <id>` | End one of yours early. |
| `cuttle outputs <id>` | Download its results again. See [Storage](storage.md). |
| `cuttle recover <id>` | Get back what it wrote *outside* your declared outputs. One day. |

## Behavior

### The three things you must declare

`--time`, `--vram` and `--output` have no defaults, and this is deliberate in each case.

**`--time`** is your wall-clock limit in **minutes**. Your Spawn is killed at it. Karma is escrowed
against it, so over-estimating costs you queue position and nothing else — the hold is released and
you are billed for the seconds you actually held, not the ones you asked for. Nothing can price a
Spawn whose length nobody declared, which is why there is no default.

**`--vram`** is GPU memory in GB — the **total** across every card you asked for. Every card holding
its share becomes eligible, and the scheduler takes the fastest free set. It is a *need*, not a
machine — you never name the box you want. A default here would be actively harmful: defaulting to
the fleet's biggest card would make every unspecified job eligible for that one GPU alone, queued
behind everything else, at the highest price, for work that might need 2 GB.

### Asking for more than one card

`--gpus N` holds N cards at once. They are always on **one machine** — a Spawn never spans two
[Arms](arms.md) — and `--vram` is the total across them, so `--gpus 2 --vram 128` asks for two cards
of 64 GB or better, not two of 128.

You are billed for each card separately, for as long as you hold it, whether or not your code uses
it. Asking for a second card you never touch doubles your bill and buys nothing: nothing in CCC
splits work across cards for you. That is your script's job (`torchrun`, DDP, or your own device
placement), and `--gpus` only makes the cards visible to it.

A count no single Arm in the fleet has is refused at submit, naming what would fit — never queued
against a machine that does not exist. In the [Dashboard](../Dashboard.md) the same counts are simply
greyed out in the dropdown.

```sh
cuttle run train.py --time 60 --gpus 2 --vram 128 -o checkpoints --manifest pyproject.toml
```

**`--output`/`-o`** names a folder **in your own project** that your script writes results into,
relative to the project root. `-o runs` means the `runs/` your code already writes to — it needs no
changes, no environment variable, and no knowledge that CCC exists. Repeatable, and at least one is
required. The folder is always there when your script starts, and whatever you uploaded in it is still
in it, so `-o checkpoints` never hides a checkpoint you sent along to resume from.

Nested paths work (`-o out/ckpt`). A path starting with `/` is absolute *inside the Pod* instead — for
keeping something an Arm Feature writes outside your project entirely, such as under `/ccc/vlwd`. Two
outputs may not nest inside one another, and a relative and an absolute path that would be kept in the
same place (`out/x` and `/out/x`) are refused at submit.

### Where your results go

They come to you. As soon as the Spawn finishes, its declared outputs are downloaded into
`~/ccc/outputs/spawn-<id>-outputs/` — you do not have to be running `cuttle run`, or still logged
into that terminal, or even have the machine switched on at the time. If it was off, the download
resumes the next time you run `cuttle`.

`--no-download` turns that off for one Spawn; `--dest` puts them somewhere other than `~/ccc/outputs`.

This is the one thing the CLI does that the [Dashboard](../Dashboard.md) cannot, and the reason is
mundane: a browser cannot write into a folder on a machine with nobody sitting at it.

### What gets uploaded

The working directory you run from, honouring `.gitignore`. `--no-gitignore` uploads everything.
`--dir <path>` uploads a different directory instead — the script, `--manifest` and `--output` paths
are then all relative to *it*, while results still download to where you are.

The Head keeps that upload only while the Spawn is *starting* — it is deleted the moment it has
staged in. It is not backed by any allowance, and it is not recoverable. This is why a
[Recipe](recipes.md) cannot be reconstructed from a Spawn that has already run: by the time there is
a result to admire, the code behind it is gone.

### What happens when it goes wrong

- **Your script exits non-zero** — the Spawn is *settled, not requeued*. You pay for the seconds it
  actually consumed and it does not quietly run again.
- **Your dependency file will not build** — the Spawn never reaches a machine at all. It fails
  having held nothing, and is billed nothing. See [Environments](environments.md).
- **You asked for something the fleet can never do** — 80 GB of VRAM when the biggest card is 24, or
  a [Feature](arm-features.md) no enrolled Arm carries — it is *refused at submit*, not queued
  forever.
- **The machine it was on is retracted underneath it** — the Spawn dies, and **settles at zero**.
  You pay nothing for work someone else stopped, and the Arm's owner earns nothing for stopping it.
  See [Arms](arms.md#retract-and-return).
- **You wrote results somewhere you did not declare** — you have a day. `cuttle recover <id>`
  pulls back the whole working directory the Spawn ran in, up to 16 GB. See
  [Storage](storage.md#the-recovery-window).

### Following a Spawn

`cuttle status` and `cuttle logs` take a Spawn id; leave it off and you are asked which one, from
your recent Spawns. `cuttle queue` is the live cluster — everything pending and running, everyone's.
`cuttle history` is what the live queue has already forgotten, with the Karma each one actually cost.

### Ending one early

`cuttle cancel <id>` from anywhere, for your own Spawns. It **settles** for the seconds it really
held: you chose to stop it, so you pay for what you used. This is the opposite of a Retract, which
settles at zero — the difference is who decided.

An Arm's owner has no way to kill one particular Spawn running on their machine. What they have is
[Retract](arms.md#retract-and-return), which takes the whole machine back.

## Admin — on the Head

`cuttle run --node <name>` pins a Spawn to one named Arm. It is the single exception to "declare
needs, never machines", and it is admin-only for exactly that reason.

`cuttle cancel <id>` ends a Spawn belonging to someone else, when you run it **on the Head**. No flag:
the Head API is asked first, and its "not yours" is the ownership test rather than the end of the
command — on the Head that answer selects the operator's road (`sudo scancel`) instead. Pick from the
menu and the whole queue is offered, not just your own Spawns.

`--any` remains for the one case that road cannot be reached by asking: an id the ledger never
recorded. `DELETE /spawns/{id}` answers 404 for those and is never escalated past it, so reaping a
runaway the Head has no row for is what the flag is for. It is refused off the Head.
