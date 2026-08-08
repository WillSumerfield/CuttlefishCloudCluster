# Cuttlefish Cloud Cluster (CCC)

Donate a spare GPU. Run sandboxed experiments on everyone else's. Settle the exchange with a Karma
economy that ranks people rather than charging them.

CCC turns a handful of desktops, lab boxes and laptops into one Slurm cluster. You submit a folder
and a script; it finds a machine, builds your Python, runs your code in a container as an identity
that is not you, and pulls the results back down to you when it is done — whether or not you were
still watching.

Machines run **Ubuntu 24.04**, natively or under **WSL2** on Windows. GPUs are **NVIDIA** only.

> Names are themed (cuttlefish) and canonical. [`CONTEXT.md`](CONTEXT.md) is the glossary;
> [`docs/`](docs/README.md) is the documentation.

## Install

`cuttle` is a Python CLI installed with [uv](https://docs.astral.sh/uv/).

**From a bundle** — if your cluster's operator built one, it comes pre-pointed at their Head:

```sh
tar xzf cuttle-installer.tar.gz
cuttle-installer/install.sh
```

**From the repo:**

```sh
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install --editable ./cli/core
```

`--editable` matters on a Head, which resolves its own source tree relative to the install. A client
does not care.

Then provision this box: `cuttle setup --arm` (donate a machine), `--client` (submit only), or
`--head` (stand up the cluster).

## Quickstart — donate a machine and run something on it

This assumes a cluster already exists. Ask its operator for an **enrollment token**:

> `cuttle arm authorize <a-name-for-my-machine> --user <my-username>`

Tokens are secret, work once, and expire within the hour.

> **You do not have to memorise any of this.** Run `cuttle` on its own and it opens a menu of what
> this box can do *right now*, then asks for whatever the thing you picked needs. The explicit
> commands below are the same actions, written out — for scripts, and for when you already know what
> you want.

**1. Join.** `cuttle` → **Set up this box**, which asks for the token. Or:

```sh
cuttle setup --arm --token <the-token>
```

It joins the cluster's private network, installs Slurm and Apptainer, registers your hardware, and
benchmarks your GPU. You will be asked which heavy or licence-bound software this box has — say yes
only for what is really there.

That is it. The machine donates itself whenever it is switched on, and **Pause your Arm** in the menu
— `cuttle arm retract <your-arm-name>` — takes it back instantly, at any time, without asking anyone.

**2. Log in.** `cuttle` → **Log in**, which is the pre-selected entry on a box that has never logged
in. Or:

```sh
cuttle login --head <head-address>
```

Your first login forces a password change.

**3. Run something.** From your project folder, `cuttle` → **Submit a Spawn**. It asks how long, how
much GPU memory, and which folder holds your results, then shows you the whole submission before it
sends anything. The same thing, typed:

```sh
cd my-project
cuttle run train.py --time 60 --vram 24 --output checkpoints --manifest pyproject.toml
```

Those three are required and have no defaults: minutes, GB of GPU memory, and the folder your script
writes into.

**4. Watch it, then find your results.** **Check a Spawn** and **Read a Spawn's output** in the menu,
or:

```sh
cuttle status <id>
cuttle logs <id> -f
```

Results download themselves into `~/ccc/outputs/spawn-<id>-outputs/` as the Spawn finishes — you do
not have to be watching, or even have the machine switched on.

## Documentation

**[`docs/`](docs/README.md)** routes to everything. The short version:

| | |
|---|---|
| [Elements](docs/Elements.md) | What CCC does, one paragraph each. Start here. |
| [Set up an Arm](docs/setup/arm.md) · [a client](docs/setup/client.md) · [the Head](docs/setup/head.md) | Getting a machine into a state. |
| [CLI](docs/CLI.md) · [Dashboard](docs/Dashboard.md) | The two clients, at parity. |
| [Architecture](docs/Architecture.md) · [Security](docs/Security.md) | How it works and what it defends. |
| [Troubleshooting](docs/Troubleshooting.md) | It didn't work. |

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) — repo layout, Conventional Commits (scope = context),
branch and rebase onto `main` directly (no PRs).
