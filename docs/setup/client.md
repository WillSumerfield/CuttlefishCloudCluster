# Set up a client

A **client** is a box you submit work *from* and donate nothing. Your laptop, usually.

No `sudo`, no packages, no scheduler, no mesh. It creates a couple of directories and logs you in.

## Before you start

- An account on the cluster. The admin creates it and gives you a username and a one-time password.
- The Head's address, if you are not installing from a bundle.

## 1. Install `cuttle`

**From a bundle** — if the admin built one on the Head, it comes pre-pointed at that Head and
pre-pinned to its certificate:

```sh
tar xzf cuttle-installer.tar.gz
cuttle-installer/install.sh
cuttle login
```

That is the whole of it. `install.sh` installs `uv` if you do not have it, installs `cuttle`, and
writes the config and certificate pin.

**From the repo:**

```sh
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install --editable ./cli/core
```

## 2. Set up and log in

Run `cuttle` with nothing after it:

```sh
cuttle
```

That opens a menu of what this box can do right now. On a box that has never logged in, **Log in** is
already selected — press enter and it asks for the Head's address, your username and your password.
**Set up this box** is in the same menu if you skipped `setup --client`.

Typed out, the same two steps are:

```sh
cuttle setup --client
cuttle login --head http://<head-address>:8082
```

After the first time, both the menu and a bare `cuttle login` remember where to go.

Your first login **forces you to change the temporary password**. You cannot skip it.

## 3. Run something

From your project folder, open the menu and pick **Submit a Spawn**:

```sh
cd my-project
cuttle
```

It asks for the three things a Spawn cannot be submitted without, then shows you the whole thing and
asks before it sends anything:

- `--time` — wall-clock minutes. Your job is killed at it.
- `--vram` — GPU memory in GB. Every card with at least this much becomes eligible.
- `--output`/`-o` — a folder under `$CCC_OUT` your script writes results into. Repeatable.

None have defaults. If you already know them, skip the prompting entirely:

```sh
cuttle run train.py --time 60 --vram 24 --output checkpoints --manifest pyproject.toml
```

Every command works this way — give it what you have, and it asks for the rest. Then:

```sh
cuttle status <id>       # queued, running, or done
cuttle logs <id> -f      # follow its output
cuttle queue             # what the whole cluster is doing
```

or **Check a Spawn**, **Read a Spawn's output** and **See the queue** in the menu.

**Your results come to you.** As soon as the Spawn finishes, its declared outputs download into
`~/ccc/outputs/spawn-<id>-outputs/` — you do not have to be watching, or even have this machine
switched on at the time. If it was off, the download resumes next time you run `cuttle`.

## Worth doing next

**Set your W&B key**, once, so every Spawn logs to your own account no matter which machine you
submitted from:

```sh
cuttle wandb set
```

**Check where you stand:**

```sh
cuttle account
```

**Read [Elements](../Elements.md)** — it is the one-paragraph-each tour of what the cluster actually
does.

**Open the Dashboard** — the same account and the same cluster, in a browser:

```sh
cuttle dashboard
```

It knows the address, so you never have to. See [Dashboard](../Dashboard.md).

## Uninstalling

```sh
cuttle teardown --client        # removes ~/.ccc. No sudo, nothing else touched.
uv tool uninstall cuttle        # removes the tool and its Python deps
```

From a bundle, `cuttle-installer/uninstall.sh` does both.

## If something goes wrong

**`cuttle login` cannot reach the Head.** Check the address with the admin — a Head advertising a LAN
IP is only reachable from that LAN.

**A certificate error.** A bundle-installed client is pinned to the certificate of the Head it was
built on. If that Head was rebuilt, you need a fresh bundle.

More at [Troubleshooting](../Troubleshooting.md).
