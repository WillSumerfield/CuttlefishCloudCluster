# Set up a client

A **client** is a box you submit work *from* and donate nothing. Your laptop, usually.

No scheduler, no mesh. It creates a couple of directories, teaches this box the cluster's name and
certificate, and logs you in.

It does need `sudo` once, and installs one package (`libnss3-tools`). Both are for the same thing: the
cluster is reachable at **`ccc.glados`** on every machine CCC installs on, and that name — plus the
certificate authority that makes it load without a browser warning — has to be written outside your
home directory. A box where you have no `sudo` cannot be set up as a client.

## Before you start

- An account on the cluster. The admin creates it and gives you a username and a one-time password.
- `sudo` on this box.
- The Head's address, if you are not installing from a bundle.

## 1. Install `cuttle`

**From a bundle** — if the admin built one on the Head, it comes pre-pointed at the cluster and
carries its certificate authority:

```sh
tar xzf cuttle-installer.tar.gz
cuttle-installer/install.sh
cuttle login
```

That is the whole of it. `install.sh` installs `uv` if you do not have it, installs `cuttle`, writes
the config, and asks for `sudo` once — to add `ccc.glados` to `/etc/hosts` and to trust the cluster's
certificate authority. A bundle carries the Head's LAN address, so it only works on that LAN.

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
cuttle login
```

`setup --client` asks for the Head's address, points this box at it, and then everything afterwards
says `ccc.glados` — that is the name in your config, in `cuttle dashboard`, and in the certificate.

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
- `--vram` — GPU memory in GB, totalled across the cards you asked for. Every card holding its share
  becomes eligible.
- `--output`/`-o` — a folder in your project your script writes results into, relative to the project
  root (`/`-prefixed for a path absolute inside the Pod). Repeatable.

`--gpus N` holds more than one card, all on one machine — see
[Spawns](../elements/spawns.md#asking-for-more-than-one-card).

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
cuttle teardown --client        # ~/.ccc, the cluster name, the trusted CA. Asks for sudo.
uv tool uninstall cuttle        # removes the tool and its Python deps
```

From a bundle, `cuttle-installer/uninstall.sh` does both.

## If something goes wrong

**`cuttle login` cannot reach the Head.** Check the address with the admin — a Head advertising a LAN
IP is only reachable from that LAN.

**A certificate error.** This box trusts one certificate authority — the cluster's. If the Head was
rebuilt from scratch (`teardown --all`), it minted a new one and nothing here recognises it: run
`cuttle setup --client` again, or take a fresh bundle.

**`ccc.glados` does not resolve.** The `/etc/hosts` line is missing — re-run `cuttle setup --client`.
The Head's IP still works for everything, so this never blocks you.

**The browser still says "not private".** The name and the certificate are per-machine, and a phone or
a borrowed laptop gets neither. On a box CCC did set up, check that `libnss3-tools` installed (Chrome
and Edge need it) and restart the browser. On a Chromebook the browser is not on the box CCC set up
at all — see below.

More at [Troubleshooting](../Troubleshooting.md).

## On a Chromebook

CCC installs into ChromeOS's Linux virtual machine, and the browser you actually use runs on ChromeOS
around it. That OS is a separate machine as far as CCC is concerned. It has neither `ccc.glados` nor
the cluster's certificate authority, and `sudo` in here cannot give it them.

`cuttle` works regardless — it resolves the name inside the VM — and `cuttle dashboard` always opens
something that loads.

Nothing can be automated: ChromeOS has no hosts file at any privilege, so `cuttle dashboard` always
opens the Head's **address** there. The certificate warning you can be rid of, by hand — CCC drops `ccc-ca.crt` in your Downloads (or in "Linux files", if you have not shared
Downloads with Linux), and ChromeOS imports it under Settings → Privacy and security → Manage
certificates → Authorities → Import.
