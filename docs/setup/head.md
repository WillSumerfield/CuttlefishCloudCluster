# Set up the Head

The **Head** is the cluster: the scheduler, the coordinator that admits machines to the mesh, the
account store, the Karma economy, the dashboard, and the place every Spawn's project and results
live.

There is one, and standing it up is a job you do once. This page assumes you are the operator.

The Head also donates its own GPU — it runs a co-located Arm — so it is not a machine that sits idle.

## Before you start

- **Ubuntu 24.04.** Native, not WSL.
- **NVIDIA** GPU with working drivers.
- `sudo`.
- **Decide the address remote Arms will reach this box at** before you run setup. See
  [Making the Head reachable](#making-the-head-reachable) — getting this wrong is the single most
  common way a cluster ends up unable to accept donors.

## 1. Install `cuttle`

From the repo, and **editable**:

```sh
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install --editable ./cli/core
```

`--editable` matters here. A Head resolves its own source tree (`head/api`, `head/web`) relative to
the install, so `setup --head` and `build-installer` need it pointed at the checkout. A client can
install however it likes; a Head cannot.

## 2. Provision

Run `cuttle` on its own and pick **Set up this box**. The guided version asks for the address Arms
will reach this box at, pre-filled with whatever this Head already advertises — which is the field
worth getting right, so prefer it. Typed out:

```sh
cuttle setup --head --address=<address-Arms-will-reach-this-box-at>
```

This does a great deal, in order: joins its own mesh, installs Slurm/munge/Apptainer, configures the
scheduler, provisions the cluster's own identity and the auth store, provisions Guises, installs
stage-in/out, builds the dashboard, issues a TLS certificate, starts the Head API and the Karma tick,
installs the nightly backup timer and the health monitor, builds the Pod image, installs the
Environment build tools, and builds the benchmark environment every Arm is priced against.

Along the way it asks which [Features](../elements/arm-features.md) *this* box has — the Head's own
Arm is an Arm like any other — and, if you want alerting, for [SMTP credentials](../SMTP-Runbook.md).
Both are skippable and both can be filled in by re-running setup later.

Re-running `cuttle setup --head` is safe and is the normal way to change something. The one thing it
will *not* redo is the Pod image: an existing `base.sif` is kept as-is and never compared against the
definition setup would render now. Changing the image means `cuttle teardown --all` and setup again,
on the Head and on every Arm.

## 3. Check it

```sh
sinfo -N -o "%N %t %C %G"
```

The Head's own node should be there, idle, with its GPUs listed.

## 4. Make yourself an account

The Admin account runs the cluster. It does **not** use it — an Admin is outside the economy, earns
no Karma and never loses contention. If you also want to submit work, give yourself an ordinary
account too:

```sh
cuttle user add <your-username>
```

It prints a one-time password, which is changed on first login.

## Making the Head reachable

This is the one part worth reading twice.

An Arm contacts the **coordinator** in order to *get* a mesh address. So the coordinator's address
can never itself be a mesh address — it is the one hop that happens before the mesh exists. It has to
be something a donor's box can already reach, from wherever that box is.

By default `setup --head` advertises this machine's primary LAN IP. That works for donors on your
LAN and for nobody else: a remote donor hangs at `cuttle setup --arm` until it times out. Setup warns
you if the address you chose is LAN-only, at the moment you choose it, because that is the last point
where changing it is free.

For a Head that remote Arms can reach, advertise a **Tailscale** address instead:

```sh
tailscale ip -4                                   # e.g. 100.x.y.z
cuttle setup --head --address=100.x.y.z
```

Use the plain `tailscale` command for this. CCC runs its **own** isolated daemon on a separate socket
for the cluster mesh, so this never returns the CCC mesh address — and it must not, because that is
precisely the address an un-enrolled Arm cannot reach yet.

The address is a **durable property of the Head**, not a per-run argument. A bare `cuttle setup
--head` keeps whatever is stored rather than reverting to the LAN IP. Changing it is confirmed rather
than announced, because re-pointing the coordinator makes every enrollment token already handed out
stop working and forces every Arm to re-register.

> **Caveat.** This requires the donor's box to be on your personal tailnet too — which is exactly the
> grant the CCC mesh exists to avoid asking a donor for. It is fine for machines you own, and it is
> how remote enrollment is done. CCC offers no public route — no tunnel, no port-forward + DDNS, no
> publicly-trusted certificate — so a donor CCC cannot reach on the LAN is reached this way or not at
> all. See [Security](../Security.md#what-is-not-defended).

## Adding an Arm

One command does the whole thing:

```sh
cuttle arm authorize <name-for-the-arm> --user <owner-username>
```

It mints an enrollment token, prints it, waits for the donor to redeem it, and admits them to Slurm
when they do.

> The token is a **secret**. It admits one machine, once, within the hour. Send it privately.

Give the donor [Set up an Arm](arm.md) and the token. That page is self-contained.

If `authorize` gives up waiting because the donor had not started yet, `cuttle arm admit <name>`
finishes the job later. The rest of the fleet commands are in [Arms](../elements/arms.md#admin--on-the-head).

## Handing out the installer

Build a portable bundle so donors and clients do not need the repo:

```sh
cuttle build-installer                 # -> ./cuttle-installer.tar.gz
```

Run on a Head, it bakes in the cluster's name and certificate authority, so the installed client
comes pre-pointed and already trusting this cluster — `cuttle login` and nothing else. `install.sh`
asks for `sudo` once, to write the name into `/etc/hosts` and install the authority.

The baked bundle resolves that name to this Head's LAN IP, so it is for boxes **on that LAN**. A remote box
reaches this Head over the CCC mesh and is not on the mesh until it enrolls, so build that one a
generic bundle and let `setup --arm` point it:

```sh
cuttle build-installer --generic
```

The bundle also ships an `uninstall.sh`, which tears the box down (detecting its role) and then
removes the tool.

## The dashboard

`setup --head` builds and serves it, at `https://ccc.glados` — the cluster's name, which every
machine CCC installs on resolves and trusts. No browser warning on any of them; a phone or a borrowed
laptop has neither, and reaches it by IP with the warning.

You do not have to hand that address to anyone, or remember it yourself: `cuttle dashboard` opens it
from any box that is set up or logged in. See [Dashboard](../Dashboard.md).

## Running the cluster

| | |
|---|---|
| [Arms](../elements/arms.md#admin--on-the-head) | authorize, admit, benchmark, chown, remove |
| [Users & access](../elements/users-and-access.md#admin--on-the-head) | add, list, remove, reset |
| [Karma](../elements/karma.md#admin--on-the-head) | override, bump |
| [Backup & recovery](../elements/backup-and-recovery.md) | create, restore |
| [Mesh](../elements/mesh.md#admin--on-the-head) | munge rotate |
| [Telemetry](../elements/telemetry.md#admin--on-the-head) | the health monitor |
| [SMTP runbook](../SMTP-Runbook.md) | alert mail |

Every one of those runs **on this box**. The Head's network API exposes no administrative writes at
all — see [Security](../Security.md#the-head-api-has-no-admin-writes).

## Tearing it down

```sh
cuttle teardown --local
```

Reverses `setup --head`: services, mesh, packages, `head.db`, `~/.ccc`. It makes you type the
hostname first, because it destroys every account, Balance and ledger row.

`--all` also removes the Pod image and the cluster's certificate authority, forcing an image rebuild
and — because a new authority is a new trust decision — a re-run of `setup` on every other box in the
cluster before any of them can reach this Head again. `--full` sweeps everything `cuttle` ever installed, for a box in an unknown or
half-broken state.

Teardown-then-setup is the **normal redeploy** here, not a disaster. If you want the state back
afterwards, take a [backup](../elements/backup-and-recovery.md) first — noting that stored W&B keys
deliberately do not survive a restore.
