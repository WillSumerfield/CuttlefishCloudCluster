# Cuttlefish Cloud Cluster (CCC)

Donate a spare GPU. Run sandboxed work on everyone else's. Settle the exchange with a Karma
economy that ranks people rather than charging them.

CCC turns a handful of desktops, lab boxes and laptops into one Slurm cluster. Every person gets a
**Tidepool** — an always-on, CPU-only home on the central **Head** where all their files live. When
they need a GPU they start a **Dive**: CCC copies a folder to a free GPU (on a donated **Arm** or the
Head itself), drops them into a shell there in a container that runs as an identity that is not them,
and mirrors their work back to their Tidepool the whole time — whether or not they are still watching.

Machines run native **Ubuntu 24.04**. GPUs are **NVIDIA** only.

> Names are themed (cuttlefish) and canonical. [`CONTEXT.md`](CONTEXT.md) is the glossary; the
> **Wiki** (`wiki/`, served by the Head at `/wiki/`) is the documentation.

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

`--editable` matters on a Head, which builds the Dashboard, the Head API and the wiki from its own
source tree. A client does not care.

Then provision this box: `cuttle setup --client` (use the cluster), `--arm` (donate a machine), or
`--head` (stand up the cluster).

## Quickstart — use a cluster

This assumes a cluster already exists and your operator gave you an account.

> **You do not have to memorise any of this.** Run `cuttle` on its own and it opens a menu of what
> this box can do *right now*, then asks for whatever the thing you picked needs. The explicit
> commands below are the same actions, written out.

**1. Point this box at the Head and log in.**

```sh
cuttle setup --client   # asks for the Head's address, then logs you in
```

Your first login forces a password change. Check it worked with `cuttle whoami`.

**2. Put a project in your Tidepool.**

```sh
cuttle upload ./myproject    # lands at ~/myproject in your Tidepool
```

Or `cuttle tidepool` to shell in and `git clone` it there.

**3. Dive onto a GPU.**

```sh
cuttle dive myproject --time 4h --vram 16
```

CCC copies the folder to a free GPU and drops you into a shell. `--time` and `--vram` are needed once
per folder, then remembered in `cuttle.toml`. Leave with `exit` (the Dive keeps running), return with
`cuttle dive attach`, and end it with `cuttle dive end`.

## Quickstart — donate a machine

Ask the cluster's operator for an **enrollment token** (secret, works once, expires within the hour):

> `cuttle arm authorize <a-name-for-my-machine> --user <my-username>`

Then, on the machine you are donating:

```sh
cuttle setup --arm --token <the-token>
```

It joins the cluster's private network, installs Slurm and Podman, registers your hardware, and
benchmarks your GPU. You will be asked which heavy or licence-bound software this box has — say yes
only for what is really there.

That is it. The machine donates itself whenever it is switched on, and `cuttle arm retract <name>`
takes it back instantly, at any time, without asking anyone (`cuttle arm return <name>` lends it
again).

## Documentation

The **Wiki** is the documentation, for Users, donors, Admins and contributors alike. The Head serves
it at `/wiki/` to anyone signed in; the source is in [`wiki/content/`](wiki/content/). Its Sections:

| Section | For |
|---|---|
| **Start** | What CCC is, getting set up, the glossary. |
| **Using CCC** | Tidepools, Dives, files, Karma, environments, the CLI and Dashboard. |
| **Donating** | Lending a machine as an Arm. |
| **Admin** | Running the Head: setup, users, mesh, alerts, backup, security. |
| **Contributing** | How CCC is built, for people changing it. |

To read it locally: `cd wiki && ./setup.sh` once, then `npx quartz build --serve` → http://localhost:8080.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) — repo layout, Conventional Commits (scope = context),
branch and rebase onto `main` directly (no PRs).
