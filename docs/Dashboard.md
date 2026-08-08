# Dashboard

The web front door, and the **primary** surface for everyday use. Same account, same cluster, same
Head as the [CLI](CLI.md) — neither is a privileged path.

## Getting to it

```sh
cuttle dashboard
```

That opens it in a browser. You do not have to remember the Head's address — the Dashboard is served
by the Head itself, at the same address `cuttle` already logs in to, so any box that has been set up
or logged in already knows where it is. It is also **Open the dashboard** in the `cuttle` menu.

`cuttle dashboard --print` prints the URL instead of opening it, for when you are on a box with no
browser and want to paste it into one that has.

The address it opens is:

```
https://ccc.glados
```

That name is the cluster's, on every machine CCC installs on — Head, Arms and clients alike. Nothing
outside the cluster resolves it: `setup` writes it into each box's `/etc/hosts`, pointed at whichever
address that box can actually reach the Head on. It also installs the cluster's certificate authority,
so the page loads clean, with no "your connection is not private". See
[Security](Security.md#transport) for why the cluster signs its own.

Reachable from a browser on the LAN or on the CCC [mesh](elements/mesh.md).

**From a phone, or a borrowed laptop.** A device CCC never installed on has neither the name nor the
certificate authority, so use the Head's IP — `setup --head` prints it — and expect the warning.

Log in with the account the Admin created for you. First login forces a password change.

**On a WSL Arm**, `cuttle dashboard` opens `https://localhost/` instead. The CCC mesh lives
inside WSL and your Windows browser has no route to the Head's mesh IP, so `setup --arm` installs a
local forward and this command hands the browser that rather than an address that could never load.
This is also the one place the certificate warning survives: the browser is on the Windows side, which
shares neither the Linux trust store nor the `/etc/hosts` entry. Click through it once.

## What is there

| | |
|---|---|
| **Queue** | The live cluster: everything pending or running, and whose it is. Also where you submit. |
| **History** | Past Spawns, with cost and outcome. |
| **Arms** | The fleet — who owns what, and its state. Retract and Return your own. |
| **Recipes** | Your saved projects. |
| **Karma** | Everyone's standing, and your own ledger. |
| **Tide** | How badly each Arm has been wanted, hour by hour. |
| **Account** | Your password, your W&B key, where you stand. |
| **Users** | Admin only. |

Click a Spawn anywhere to open its detail page: state, logs, cost, outputs.

## Submitting from the browser

Drop a folder onto the composer, or click to choose one. Picking it expands the composer and shows
you what you picked — name, file count, total size — as a preview.

**Nothing uploads until you press Submit.** Bytes move on one click and never on the pick, so you can
repick, toggle `.gitignore` handling, and change your mind for free.

You then fill in the same three required things the CLI requires — the script, the wall-clock limit,
the VRAM — plus your output folders. There is a [Tide Pool](elements/tide-pool.md) mode which hides
the project and upload story entirely and asks only for the resource needs.

## The one thing the CLI does that this cannot

**Downloading results unattended.**

When a Spawn finishes, the CLI pulls its outputs down to `~/ccc/outputs/spawn-<id>-outputs/` on its
own — you do not have to be watching, logged in, or even have that machine switched on.

A browser cannot do that, for a mundane reason: it cannot write into a folder on a machine with
nobody sitting at it. From the Dashboard you download a finished Spawn's results by asking for them.

Parity covers what you can ask of the *cluster*. It does not cover what each client can do to the
machine it is running on. The other half of that same rule is that the Dashboard can never run
`setup` or `teardown` — those provision the box you run them on.

## Admin in the Dashboard

You can *see* everything. You cannot *change* anything privileged.

The Users page is admin-only, and it is read-only — not for want of plumbing. The Head API exposes
**no administrative write endpoints at all**, deliberately, so there is nothing for the SPA to call.
Creating a User, minting an enrollment token, overriding Karma, bumping the queue: every one of those
runs on the Head, through `cuttle`.

See [Security](Security.md#the-head-api-has-no-admin-writes) for the reasoning, and
[Set up the Head](setup/head.md#running-the-cluster) for where each of those commands lives.

Retract and Return are the exception, and are on the Arms page: they are **owner-scoped**, not
admin-scoped, so they are yours to do over the API from anywhere.
