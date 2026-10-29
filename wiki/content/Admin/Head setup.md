---
title: Head setup
---
The [[Glossary#Head|Head]] is the one machine that runs the cluster: the queue, everyone's
[[Glossary#Tidepool|Tidepools]], the [[Glossary#Dashboard|Dashboard]] and the [[Glossary#Mesh|Mesh]]
coordinator. You set it up once with `cuttle setup --head`. It also lends its own GPUs, so it's an
[[Glossary#Arm|Arm]] too.

## What you need

- A native **Ubuntu 24.04** box on **x86_64**. Setup refuses anything else.
- An **NVIDIA GPU** with a working driver (`nvidia-smi` has to run). If there's more than one card,
  they must all be the same model.
- `sudo`, and internet access during setup. It pulls packages from Ubuntu, NodeSource and NVIDIA, and a
  few pinned downloads from GitHub (Headscale, uv, micromamba, and Quartz for this wiki).
- Disk for the Tidepools. Every User gets the same fixed size, reserved in full when they're added,
  so pick a folder on a disk that can hold that size times the number of Users.
- An address that Arms can reach this box at **before** they're on the Mesh: its LAN IP if every
  donor is on your network, or a public IP or hostname if some aren't. See [[Mesh]].

## Install cuttle

Install `cuttle` from a checkout of the repo, and make it editable. The Head builds the Dashboard,
the Head API and this wiki from that checkout, so it has to point at it.

```bash
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install --editable ./cli/core
```

## Run setup

```bash
cuttle setup --head --address=<address Arms will reach this box at>
```

Or run `cuttle` on its own, choose **Set up this box**, then **The Head**. It asks for the address
for you.

Setup asks its questions first, then works through the steps without stopping:

1. **Arm Features**: which of CCC's built-in Features this box has, and where each is installed.
   See [[Arm features]].
2. **Tidepools**: the folder for the Tidepool images (default `/var/lib/ccc-tidepools`; it must be
   outside `/var/lib/ccc`), the size per User, the CPU and memory cap per User, and the CPUs and
   memory for all Tidepools together. Each question offers a default. That total is
   taken away from what this box offers to Dives.
3. **Cluster name**: lowercase letters, digits and `-`, default from the hostname. The Dashboard
   lives at `https://ccc.<name>` and each User's Tidepool SSH alias is `<name>`. It goes into the
   certificate, so changing it later means a teardown and a fresh setup.
4. **Coordinator address**, if you didn't pass `--address` and the box has more than one address to
   choose from. If the one you pick is a private LAN address, setup warns you that remote Arms won't
   be able to join.
5. **Email alerts**: SMTP details for the health monitor. Leave the host blank to skip. See
   [[Alerts and email]].

Then it installs and starts everything. It takes a while: it builds the container images and runs a
GPU benchmark on this box at the end.

Last of all it signs you in as the first Admin, `admin`. That account is created with a one-time
password that's never shown: setup uses it to log in and then asks you to choose your own. Don't
walk away before this step.

When it finishes it prints the Dashboard address, the same Dashboard by IP, the Head's Mesh IP, and
how to add an Arm.

### Check it

```bash
sinfo -N -o "%N %t %C %G"
```

The Head's own node should be listed, idle, with its GPUs.

## What gets installed

Services, all systemd units:

| Unit | What it does |
| --- | --- |
| `ccc-head-api` | The Head API, the Dashboard and this wiki, on port 443 |
| `ccc-enroll-api` | Hands Arms what they need to join. Mesh only, port 8081 |
| `ccc-karma` | The Karma tick |
| `ccc-rsyncd` | Copies Dive folders to and from Arms. Mesh only, port 873 |
| `ccc-tidepool@<user>` | One per User: their always-on Tidepool container |
| `ccc-registry-push`, `ccc-registry-pull` | The container image registry Arms pull from, port 5000 |
| `headscale` | The Mesh coordinator, port 8080 |
| `tailscaled-ccc` | CCC's own Mesh client, separate from any other Tailscale on the box |
| `slurmctld`, `slurmd`, `munge` | The scheduler, and the Head's own Arm |
| `ccc-backup.timer` | Nightly backup. See [[Backup]] |
| `ccc-monitor.timer` | Health check every 5 minutes. See [[Alerts and email]] |
| `ccc-mesh-firewall.timer` | Keeps the Mesh working next to a personal Tailscale. See [[Mesh]] |
| `ccc-registry-gc.timer` | Nightly cleanup of finished Dives' image layers |
| `ccc-arm-rollout.timer` | Updates out-of-date Arms to the Head's `cuttle`, every 5 minutes |

Where things live:

| Path | What's there |
| --- | --- |
| `/var/lib/ccc/head/head.db` | Accounts, sessions, Dives, Karma |
| `/var/lib/ccc/arms.json` | The Arm registry |
| `/var/lib/headscale/` | The Mesh coordinator's database and certificate |
| `/var/lib/ccc-tidepools/` | One image file per User (or the folder you chose) |
| `/var/lib/ccc/tidepools/<user>/` | Where each User's image is mounted |
| `/opt/ccc/api/.venv` | The Head API's Python environment |
| `/opt/ccc/head-api/web`, `/opt/ccc/head-api/wiki` | The built Dashboard and wiki |
| `/opt/ccc/cuttle-dist/` | The `cuttle` build laptops and Arms update to |
| `/opt/ccc/tls/` | The cluster's certificate authority and the Head's certificate |
| `/etc/ccc/cluster-name`, `/etc/ccc/tidepools.json` | Your answers to setup |
| `/etc/slurm/` | Slurm config, written by setup |
| `/var/backups/ccc/` | Backups |

If `ufw` is already on, setup opens the ports Users need (443, the Dive relay ports from 31000, and
the Tidepool SSH ports from 32001), the Mesh coordinator's port (8080) for Arms, and lets Mesh traffic
in. It never turns a firewall on for you.

## Changing a setting

Re-run `cuttle setup --head`. It's safe, and every question comes back pre-filled with your earlier
answer, so you only change what you need. The coordinator address is remembered too: a plain
re-run keeps it.

## Updating

After pulling new code into the checkout:

```bash
git pull
cuttle update
```

`cuttle update` re-does the install half of setup without asking anything: it rebuilds the Head API
environment, the Dashboard and the wiki, rewrites the unit files and Slurm config, and reissues the
certificate if needed. It never changes what you decided at setup.

It also builds `cuttle` itself from the checkout, and everyone else picks that build up:

- **Laptops** update themselves. The next time someone runs `cuttle`, it sees the Head has a
  different build, installs it and carries on. Nobody needs a new installer.
- **Arms** are updated by the Head. Each out-of-date Arm stops taking new Dives, finishes the ones it's
  running, then runs an update job and comes back. `cuttle update` ends by listing the Arms it's
  updating; `cuttle arm list` shows where each one stands.

A failed Arm update is retried twice. After that the Arm stays out of service, the Arms page says
**Update failed**, and you get an email (if [[Alerts and email|email]] is set up). Once the cause is
fixed:

```bash
cuttle arm update <name>      # or --all
```

An Arm set up before automatic updates fails its first one, saying its owner must run `cuttle update`
on it once.

## Tearing down

```bash
cuttle teardown
```

On the Head this works out the role by itself (`--local` says it explicitly). You type the hostname to
confirm, because it deletes `head.db`: every account, Balance and ledger row. A teardown followed by
a fresh setup is the normal way to rebuild a Head.

- Tidepool images are **kept** by default, and listed in `/etc/ccc-tidepools-kept.json`.
- `--all` also removes the container images, the cluster's certificate authority (every client has to
  trust the new one after the next setup) and every Tidepool image.
- `--keep-packages` leaves the apt packages installed, which makes the next setup faster.
- `--full` sweeps everything CCC ever installed, whatever role the box thinks it has. Use it on a
  half-installed or stuck box.
- Backups in `/var/backups/ccc` are never deleted.

## Related

- [[Admin/index|Admin]]: parent
- [[Users and access]]: adding people once the Head is up
- [[Mesh]]: picking the coordinator address, adding Arms
- [[Arm setup]]: the same idea, on a donor's machine
- [[Admin troubleshooting]]: when setup fails
- [[Architecture]]: how the pieces fit together
