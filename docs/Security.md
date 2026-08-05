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
deliberately does not have. And `cuttle cancel --any` uses `sudo scancel`.

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

**Injected credentials do not leak sideways.** A Spawn's W&B key is visible from inside its own Pod
and nowhere else — not from another Spawn on the same machine, and not through `/proc`.

## Secrets, and how each is handled

| Secret | Where it lives | Notes |
|---|---|---|
| User passwords | `head.db`, argon2id | One-way. Never recoverable, never printed. |
| Session tokens | `head.db`, sha256 | One-way. `cuttle user reset` kills all of a User's sessions. |
| W&B keys | `head.db`, AES-256-GCM | Encrypted under a keyfile beside the database. |
| Enrollment tokens | Minted, printed, expire | One machine, one use, about an hour. |
| The munge key | `0400 munge:munge` on every node | Fetched over the mesh only; mesh membership is the authorization. |
| Feature product keys | On the Arm, `0640`, CCC-owned | **Never sent to the Head.** Only the activation Spawn ever sees one. |
| Backups | `/var/backups/ccc`, `root:root 0700` | Hold live secrets including session tokens. |

**What at-rest encryption of W&B keys actually buys.** The Head decrypts unattended, so the keyfile
sits next to the database and anything that can read one can read the other. It protects a `head.db`
that leaks *on its own* — a copied database, a backup, a pulled disk. It does not protect a
compromised Head, and it is not claimed to.

This is why `cuttle backup` deliberately **excludes the keyfile**: it makes the tarball strictly less
sensitive than the Head itself. The cost is that a restore comes back with every stored key
unreadable, which the Head treats as unset rather than as an error. See
[Backup & recovery](elements/backup-and-recovery.md#stored-wb-keys-do-not-come-back).

**Temporary passwords** are printed once, by the operator, and must be changed on the User's first
login. Nothing in CCC ever prints or stores a reusable password.

## Transport

Nodes talk over the [mesh](elements/mesh.md): one flat encrypted WireGuard network containing the
cluster and nothing else. Two clusters' meshes never see each other.

The human-facing surface is TLS with a **self-signed** certificate. No CA will sign a certificate for
a box with no public domain name, so the Head signs its own and clients pin it on first use, the way
`ssh` pins a host key. A client installed from a Head-built bundle has the pin baked in and never
sees a warning; a browser shows one once.

## What is not defended

Stated so nobody assumes otherwise:

- **A hostile Arm owner.** They control the hardware. See the trust model above.
- **A compromised Head.** It holds everything and decrypts unattended by design.
- **Public exposure.** The Head is assumed reachable over the LAN or the CCC mesh. There is no
  public tunnel, no real certificate, and no hardening for an internet-facing deployment. See
  [TODO](TODO.md).
- **Denial of service by a legitimate User.** Karma makes queue-flooding self-punishing — an Escrow
  suppresses your standing the moment you queue, so flooding immediately costs you your own position
  — but it is an economic disincentive, not a rate limit.
- **Preemption of volatile Arms.** Not built. Every enrolled Arm is treated as permanent; Retract is
  what an owner has, and it is available everywhere.

## Reporting something

This is a small, operator-run cluster. Tell the operator.
