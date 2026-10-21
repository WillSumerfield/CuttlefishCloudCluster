# Troubleshooting

Symptom, cause, fix. Grouped by who you are.

## Submitting work

### My Spawn was refused at submit, with no id

You asked for something the fleet can never satisfy — more VRAM than the biggest card, or a
[Feature](elements/arm-features.md) no enrolled Arm carries. Refusing is deliberate: the alternative
is queuing forever for a machine that may never exist.

### My Spawn died at exactly `--time`

That is what `--time` is. It is a kill, not a warning. Over-estimating costs you only queue position
— the hold is released and you pay for the seconds you actually held.

### My Spawn is stuck behind everyone else's

Check `cuttle karma` — someone with better standing wins contention. Karma never *gates*, so it will
run; it is behind, not blocked. If the cluster is idle and it is still not running, look at
eligibility: an unusual `--vram` or `--feature` may leave only one machine able to take it.

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

### My Arm worked until I restarted the machine

If you also run Tailscale personally, it is dropping CCC's traffic. Tailscale installs a rule that
discards anything from `100.64.0.0/10` not arriving on its own interface; CCC's mesh addresses are
inside that range and arrive on `ccc0`. Which of the two firewalls wins is decided at boot, so a
restart can flip a machine that had been working. Check for the rule:

```sh
sudo iptables -S ts-input
```

A line ending `-s 100.64.0.0/10 -j DROP` is the cause. The fix is to update the Arm — this installs
the exemption and the timer that keeps it in place, and it works even while the mesh is being dropped:

```sh
cuttle update
```

To confirm afterwards, `sudo iptables -S ts-input` should now show `-i ccc0 -j RETURN` as its first
rule, above the DROP.

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

Expected on a device CCC never installed on — a phone or a borrowed laptop. On a box CCC did set up: check `libnss3-tools` is installed (Chrome, Chromium,
Edge and Brave read a separate store that needs `certutil`), then restart the browser. Snap-packaged
Firefox and Chromium may not read the files `setup` writes.

Also expected on a **guest** — a Chromebook — where the browser runs on the OS around the box and
shares none of its trust. Import `ccc-ca.crt` by hand (Settings → Privacy and security → Manage certificates → Authorities →
Import); there is no scriptable way to do it.

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
