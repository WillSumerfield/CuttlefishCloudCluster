# Troubleshooting

Symptom, cause, fix. Grouped by who you are.

## Submitting work

### "at least one --output/-o is required"

Not a bug. Name the folder(s) **in your project** your script writes results into — `-o runs` is the
`runs/` your code already writes to, relative to the project root:

```sh
cuttle run train.py --time 60 --vram 24 --output checkpoints --manifest pyproject.toml
```

There is no default and none is offered — a Spawn with no declared output has nowhere retrievable for
its results to land. See [Spawns](elements/spawns.md#the-three-things-you-must-declare).

### My Spawn was refused at submit, with no id

You asked for something the fleet can never satisfy — more VRAM than the biggest card, or a
[Feature](elements/arm-features.md) no enrolled Arm carries. Refusing is deliberate: the alternative
is queuing forever for a machine that may never exist.

Also refused, before anything is uploaded: **no `--manifest`** (every Spawn names the dependency file
its [Environment](elements/environments.md) is built from — there is no default), or a `--manifest`
naming a file that is not in your project.

### My Spawn got an id, then failed immediately

Usually the [Environment](elements/environments.md) build. `cuttle logs <id>` has the resolver's
complaint. A failed build costs nothing — the Spawn never reached a machine.

### My results are missing

They download automatically to `~/ccc/outputs/spawn-<id>-outputs/`. If they are not there:

- **The Spawn is still finishing.** The last copy home can arrive after the Spawn is already billed.
- **You submitted with `--no-download`.** `cuttle outputs <id>`.
- **This machine was off when it ended.** Run any `cuttle` command; the download resumes.
- **You wrote them somewhere you did not declare.** `cuttle recover <id>`, within a day of the
  Spawn ending. See [the Recovery Window](elements/storage.md#the-recovery-window).

### `cuttle outputs` says the results are gone

Seven days from when the Spawn ended, or your 32 GB filled up and older outputs were evicted to make
room. Nothing on the Head is an archive — the automatic download is what makes that safe. See
[Storage](elements/storage.md).

### `cuttle recipe run` returns 501

Not wired up yet. Submit the project directly with `cuttle run`. Saving, listing, updating and
deleting all work.

### My Spawn died at exactly `--time`

That is what `--time` is. It is a kill, not a warning. Over-estimating costs you only queue position
— the hold is released and you pay for the seconds you actually held.

### My Spawn is stuck behind everyone else's

Check `cuttle karma` — someone with better standing wins contention. Karma never *gates*, so it will
run; it is behind, not blocked. If the cluster is idle and it is still not running, look at
eligibility: an unusual `--vram` or `--feature` may leave only one machine able to take it.

### `wandb.init()` failed, or my runs are not logging

If you have never run `cuttle wandb set`, your Spawns run with W&B explicitly **disabled** and you
are told so at submit.

If you *had* a key and it stopped working, an Admin most likely reset your password. Your key was
encrypted with the old one, so the reset destroyed it — there is no way for it not to. Re-run
`cuttle wandb set`. (Changing your own password with `cuttle passwd` does *not* do this, and neither
does a restore from backup.) See [Telemetry](elements/telemetry.md).

### torch disappeared inside my Spawn

You declared an Environment, and an Environment inherits nothing from the base image — whichever file
you declared it in. Add torch to it. That is also what lets you pin the version you actually want
rather than living with the cluster's. See [Environments](elements/environments.md).

### "this Head cannot build Environments"

The Head has no `uv`/`micromamba` under `/opt/ccc/tools`. Only the operator can fix it —
`cuttle setup --head` installs them. It used to fall back to the base image instead of refusing;
it no longer does, because falling back means running your work against something you did not ask
for.

## Donating a machine

### `cuttle setup --arm` hangs, then times out

The Head's coordinator address is not reachable from your network. Almost always a Head advertising
a LAN IP to a remote donor. Only the operator can fix it — point them at
[Making the Head reachable](setup/head.md#making-the-head-reachable).

### "the token is invalid or expired"

Enrollment tokens work **once**, for **one machine**, within about **an hour**. Ask for a fresh one.

### My Arm shows as *enrolled* but never *admitted*

`cuttle arm authorize` gave up waiting while you were still installing. The operator runs
`cuttle arm admit <name>`.

### My Arm stopped being able to reach the scheduler

The operator rotated the cluster's shared key. On the Arm:

```sh
cuttle arm sync-key
```

### Setup fails partway, or the box is in a strange state

If the box is still on the mesh, just run setup again — no token needed:

```sh
cuttle setup --arm --enrolled
```

If it is not, or the Head was rebuilt behind the same coordinator address:

```sh
cuttle setup --arm --re-enroll --token <fresh-token>
```

That discards this machine's mesh identity and joins from scratch, which is why it needs a token —
the preauth key inside one is what admits a box to the mesh, and it is spent on use.

For a genuinely stuck install: `cuttle teardown --full`, then start again.

### I need my GPU back right now

```sh
cuttle arm retract <your-arm-name>
```

Immediate. Whatever is running gets TERM, three minutes, then killed — and its submitter pays
nothing. `cuttle arm return` when you are done. See [Arms](elements/arms.md#retract-and-return).

## Clients

### `cuttle login` cannot reach the Head

Check the address with the operator. A Head advertising a LAN IP is reachable only from that LAN.

### A certificate error

A client trusts exactly one certificate authority — the cluster's — `known_hosts`-style. If the Head
was rebuilt from scratch (`teardown --all`), it minted a new one and nothing recognises it: re-run
`cuttle setup --client`, or take a fresh bundle.

### `ccc.glados` does not resolve

Nothing outside the cluster resolves that name; each box gets an `/etc/hosts` line from `cuttle setup`.
Re-run `setup` for the box's role to rewrite it. The Head's IP works for everything in the meantime.

### The browser still warns about the Dashboard

Expected on a device CCC never installed on — a phone, a borrowed laptop, or a Windows browser reaching
a WSL Arm's forward. On a box CCC did set up: check `libnss3-tools` is installed (Chrome, Chromium,
Edge and Brave read a separate store that needs `certutil`), then restart the browser. Snap-packaged
Firefox and Chromium may not read the files `setup` writes.

### `cuttle` opens a menu when I wanted help

Bare `cuttle` opens the menu on a terminal and prints help anywhere else. `cuttle --help` always
prints help.

### Off-terminal teardown is refused

Deliberate. A script has to state its intent — a role (`--local`/`--arm`/`--client`), `--full`, or
`--yes`. Auto-detecting a role and destroying `head.db` with nobody watching is not something CCC
will do on its own.

## Operating the cluster

### `cuttle setup --head` warns my address is LAN-only

It is telling you that remote donors will hang at `setup --arm`. Fine if every donor is on your LAN.
If not, re-run with `--address=<reachable-address>`. See
[Making the Head reachable](setup/head.md#making-the-head-reachable).

### The Pod image build fails with `setgroups: Permission denied`

The build is unprivileged and maps a fakeroot namespace out of the operator's `/etc/subuid` range.
Setup grants one to an operator who has none — a directory-managed account (LDAP/AD) never went
through `adduser`, so it has none — and installs `uidmap` for the `newuidmap` that applies it. If it
still fails, the earlier `User not listed in /etc/subuid` line says the grant did not take:

```sh
grep "$(id -un)" /etc/subuid /etc/subgid   # a range should be there
command -v newuidmap                       # apt-get install uidmap if absent
```

### Re-pointing the coordinator

Confirmed rather than announced, because it invalidates every enrollment token already handed out and
forces every Arm to re-register. A bare `cuttle setup --head` keeps the stored address rather than
reverting to the LAN IP.

### An admin command "must run on the Head"

Correct, and not a misconfiguration. The Head's network API exposes no administrative writes at all.
See [Security](Security.md#the-head-api-has-no-admin-writes).

### I want to see my stored W&B key

You cannot, and neither can anyone else. A stored key is write-only: `cuttle wandb show` and the
Dashboard's Account page give you its last four characters, which is enough to tell it from some
other key. That is what lets it be kept under your password rather than somewhere the Head can reach
on its own. Get the key from W&B, or just paste it again. See
[Telemetry](elements/telemetry.md).

### No alert emails

See the [SMTP runbook](SMTP-Runbook.md#if-mail-stops-arriving).

### Arms cannot reach `slurmctld` after a munge rotation

Expected. Every already-admitted Arm needs `cuttle arm sync-key`, run by its owner. `cuttle munge
rotate` prints the list of who to tell.

### Guises or private per-Spawn `/tmp` are "held back"

Setup says this when some Arms have not yet picked up the prolog or `job_container.conf`. Those Arms
need to re-run setup, or be re-admitted, before the feature applies to them.

## Still stuck

- `cuttle <command> --help` — always current, unlike any document.
- `--debug` on any command gives a traceback instead of a one-line error.
- `journalctl -u ccc-monitor` on the Head — disk, service and rogue-usage checks.
- [`CONTEXT.md`](../CONTEXT.md) — the precise definition of any term used here.
