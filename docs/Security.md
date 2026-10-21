# Security

What CCC defends, how, and what it deliberately does not defend against.

The situation is unusual and worth stating plainly: **your code runs on machines you do not own, and
code you did not write runs on yours.** Almost everything below follows from taking that seriously in
both directions.

## The trust model

**Users are strangers to each other.** One User's work must not be able to read, kill or interfere
with another's, on any machine, and this is enforced by the operating system rather than by
agreement. See [Isolation](elements/isolation.md).

**Arm owners are trusted.** A donor is somebody the operator knows and deliberately admitted — the
Admin mints a token naming a specific machine and a specific owner. CCC does not attempt to defend
against a hostile donor who controls the hardware their guest's work runs on. It does defend against
an *accidental* one: a Spawn cannot reach the donor's files, and the donor's Licence and product keys
cannot reach a Spawn.

**The Admin is trusted by construction.** The role runs the cluster. It is outside the economy
entirely, and its Spawns sit at nice 0 and therefore outrank every User's for free. That is
deliberate, and it is also why an operator who wants to do *research* should hold an ordinary account
as well — work done from the Admin account is silently exempt from the economy.

**Machines and people are authorized separately.** Mesh membership admits a machine. A session
admits a person. Neither implies the other, and one Spawn's access to the Head is narrower than
either — see [Siphons](elements/isolation.md#siphons--what-it-can-reach).

## The Head API has no admin writes

This is the single most load-bearing decision here.

**Every privileged operation runs on the Head, locally, as the `ccc` service account.** Creating
Users, minting enrollment tokens, Karma override, queue bump, backup and restore, munge rotation —
none of them exist over HTTP. Being the operator on the Head *is* the authorization; there is no
session, no token, and no endpoint.

`cuttle` is the only administrative surface, and the privileged half of it simply refuses to work
anywhere but the Head.

The reason is what a stolen session would otherwise buy. Minting Karma is the most valuable thing an
attacker with a User's credentials could do, so that capability is not reachable from the network at
all. The API surface that faces the LAN can create no accounts, move no Karma, and admit no machines.

The same logic pins two specific commands to the Head for a second reason: `cuttle karma bump` needs
a *negative* nice, which Slurm grants only to an operator — an authority the LAN-facing API
deliberately does not have. And cancelling someone else's Spawn uses `sudo scancel`.

That last pair — `cuttle cancel` and `cuttle arm retract`/`return` — are the only commands with a road
on *both* sides of this line, because their ordinary form is an owner action that has to work from a
laptop. They ask the Head first and read its 403 as the ownership test: off the Head that refusal is
the answer, and on the Head it selects the operator's road. What decides is **being on the Head**, never
a session's `is_admin` — the API governs what a session may do, `sudo` governs what the shell may do,
and it is the shell that acts here.

## Isolation

Three mechanisms, covered properly in [Isolation](elements/isolation.md):

- **Pods** (Apptainer) — a Spawn cannot see the host filesystem, other processes, or any concurrent
  Spawn's working files. The walls enclose the whole Spawn, including the wrapper that stages files.
- **Guises** — one OS identity per User, everywhere in the cluster. A Guise authenticates nobody and
  can log in nowhere; it exists only to be a boundary.
- **Siphons** — one Spawn's channel to the Head reaches that Spawn's own project and results, and
  nothing else the Head holds.

Two consequences worth calling out:

**No User's code ever runs as the cluster.** Not on an Arm, and not on the Head. Building an
Environment is running a dependency declaration, which is code, so it happens in a Pod as that User's
Guise — on the Head. There is no path by which submitted code becomes the Mantle.

**A Spawn's environment does not leak sideways.** It is visible from inside its own Pod and nowhere
else — not from another Spawn on the same machine, and not through `/proc`.

## Secrets, and how each is handled

| Secret | Where it lives | Notes |
|---|---|---|
| User passwords | `head.db`, argon2id | One-way. Never recoverable, never printed. |
| Session tokens | `head.db`, sha256 | One-way. `cuttle user reset` kills all of a User's sessions. |
| W&B keys, and every other Credential | `head.db`, AES-256-GCM | Encrypted under **the owner's password**. Write-only — never readable back, by anyone. |
| GitHub SSH keys | `head.db`, AES-256-GCM | The same envelope, but **minted** rather than pasted: the Head generates the pair and only the public half is ever shown. See [GitHub keys](elements/github-keys.md). |
| Tide Pool private keys | `head.db`, AES-256-GCM | Encrypted under a keyfile beside the database. Deleted when the Spawn leaves the queue. |
| Enrollment tokens | Minted, printed, expire | One machine, one use, about an hour. |
| The munge key | `0400 munge:munge` on every node | Fetched over the mesh only; mesh membership is the authorization. |
| Feature product keys | On the Arm, `0640`, CCC-owned | **Never sent to the Head.** Only the activation Spawn ever sees one. |
| Backups | `/var/backups/ccc`, `root:root 0700` | Hold live secrets including session tokens. |

**What password-bound encryption of Credentials actually buys, and what it does not.**

A Credential — your W&B key, and anything like it — is sealed under a key derived from *your*
password, not under anything the Head keeps. Concretely: a stolen `head.db` yields nothing. Not to
someone who copied the database, not to someone holding a backup archive, not to someone who pulled
the disk, and not to an Admin. **The Head cannot read your key while you are not logged in**, which
is the whole point and also the source of every consequence below.

What it does **not** buy: it is not a defence against a *live session*. Whoever holds your session
token can submit a Spawn that uses your GitHub key from inside its Pod, and no amount of encryption at rest
changes that. This is exactly why there is no "reveal" — reveal would have cost the secret a trip
across the wire and a stay in your terminal's scrollback, to defend against nothing that was not
already reachable. Protect your session; that is the boundary that matters.

Three consequences follow, none of them accidental:

- **An Admin password reset destroys that User's Credentials.** Both keys that could open them die
  with the old password and the sessions it authorized. An Admin who can reset a password
  deliberately cannot read what that password was keeping.
- **A Spawn submitted with no session carries no key.** `cuttle run --node` — Head-local, operator
  only — has a User to attribute to but no login behind it, so it runs without your GitHub key.
- **A restore now keeps them.** Both halves of the wrapping live in `head.db`, so restoring the
  database and logging in re-derives the key that opens them. This was not true before.

`cuttle backup` still **excludes `secret.key`**, which now protects only Tide Pool private keys — it
keeps the tarball strictly less sensitive than the Head itself, and costs a restore nothing, since a
Tide Pool row belongs to a Spawn that ended long before anyone restores it. See
[Backup & recovery](elements/backup-and-recovery.md).

**Temporary passwords** are printed once, by the operator, and must be changed on the User's first
login. Nothing in CCC ever prints or stores a reusable password.

The cluster's **first Admin** is not even printed once. `setup --head` seeds that account, spends its
one-time password itself on a login, and asks you to choose a real one before it finishes — so the
password reaches no screen, no scrollback, and no run log. Earlier versions printed it as a result you
were expected to use later, which made it the one reusable secret CCC ever put on a terminal.

**An account key is a standing grant, and the GitHub key is one.** Everything above is about a secret
being *readable*; this one is about a secret being *usable*. For the life of a Spawn, the ssh-agent
holding your GitHub key is reachable by every process in that Pod — your code and every dependency
your Environment installed — and that key reaches every repository your GitHub account reaches, with
write access. The key itself never leaves the agent, so nothing can take a copy and use it next week.
But while the Spawn runs, anything in it can sign as you. `cuttle run --no-git` and the Dashboard's
composer toggle leave it out of a Spawn; running code you did not write is the reason to.

## Transport

Nodes talk over the [mesh](elements/mesh.md): one flat encrypted WireGuard network containing the
cluster and nothing else. Two clusters' meshes never see each other.

The human-facing surface is TLS, under a certificate authority the cluster mints for itself. No public
CA will sign for a box with no public domain name, so the Head is its own: it mints a root once at
`setup --head` and signs the certificate it serves with it. Every machine CCC installs on is told to
trust that root — the OS store, and the separate stores Chromium-family browsers and Firefox read — so
the Dashboard loads at `ccc.glados` with no warning. A device CCC never installed on gets neither the
name nor the trust, and sees the warning as before.

A **guest** install — a Chromebook client — is the one case where the browser is not on the box CCC
was invited onto. ChromeOS is not given the trust: its store can only be written by hand.

The CLI does not settle for that. It verifies the Head against this one authority and **nothing else**,
so a public CA that mis-issued for `ccc.glados` still could not impersonate the Head to `cuttle`. The
authority is pinned on first contact the way `ssh` pins a host key, and an attacker present at that
exact moment is the standing weak point — the same one `ssh` has. Two of the three ways in avoid it
entirely: an Arm receives the authority inside `/enroll`'s token-authenticated response, and a
bundle-installed client receives it in the bundle. Only a generic `cuttle setup --client` has to ask a
human to confirm a fingerprint.

Serving certificates are reissued freely under that root — a new address, a changed LAN IP — and no
client notices, because what it pinned was the authority. Replacing the **authority** is the breaking
change, and only `teardown --all` does it.

## What is not defended

Stated so nobody assumes otherwise:

- **A hostile Arm owner.** They control the hardware. See the trust model above.
- **A compromised Head.** It holds everything and decrypts unattended by design.
- **Public exposure.** The Head is reachable over the LAN or the CCC mesh, and CCC is built for
  nothing wider. There is no public tunnel, no publicly-trusted certificate, and no hardening for an
  internet-facing deployment — the Head signs its own certificate and clients pin it on first use,
  which is sufficient on a LAN and is not sufficient on the internet. `ccc.glados` is a private name
  carried in `/etc/hosts`, not a domain; nothing outside the cluster resolves it. Put this cluster on
  the open internet and none of the above becomes true by itself.
- **A stolen Head.** The authority's private key lives on the Head, so whoever holds that box can mint
  a certificate for any name in the cluster and every other box will trust it. This is the price of
  the browser no longer warning, and it is the same box that already holds every account and secret.
- **Code you chose to run, inside your own Spawn.** A Spawn runs as you, with whatever you gave it —
  your GitHub key in its agent. CCC isolates Users from each other,
  not you from a dependency you installed. `--no-git` narrows what one Spawn holds; nothing narrows
  what a Spawn does with what it holds.
- **Denial of service by a legitimate User.** Karma makes queue-flooding self-punishing — an Escrow
  suppresses your standing the moment you queue, so flooding immediately costs you your own position
  — but it is an economic disincentive, not a rate limit.

## Reporting something

This is a small, operator-run cluster. Tell the operator.
