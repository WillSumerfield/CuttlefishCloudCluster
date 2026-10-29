---
title: Security
tags: [leaf]
---
CCC keeps Users apart from each other and from the machines they run on, and keeps every powerful
action on the Head itself. Your code runs on machines you don't own, and other people's code runs on
yours, so most of what follows is about making both of those safe.

## Who's trusted

- **Users don't have to trust each other.** One User can't read, change or stop another's files,
  Tidepool or Dives. The operating system enforces that, not good manners.
- **Arm owners are trusted.** You chose to let their machine in, and you named its owner. CCC doesn't
  try to defend a Dive from someone who controls the hardware it runs on. It does stop a Dive from
  reaching the owner's own files.
- **Admins are trusted.** They run the cluster.

Letting a machine in and letting a person in are separate. Joining the [[Mesh]] admits a machine; a
password admits a person. Neither gives you the other.

## All admin actions happen on the Head

The Head API, which serves the Dashboard and the CLI, can't do anything administrative. There's no
way over the network to create an account, reset a password, change someone's Karma or admit an
Arm. Those are `cuttle` commands you run **on the Head**, with `sudo`. Being at the Head's terminal is
the permission.

So a stolen password or session, even an Admin's, can't be used to mint Karma or take over accounts.
In the Dashboard, an Admin only gets one extra page, the read-only [Users page](dashboard:/users).

## Signing in

- Passwords are stored as argon2id hashes. Nobody, including you, can read one back.
- New and reset passwords are one-time: printed once, and the User must change them at first sign-in.
  The first Admin's one-time password isn't even printed; setup uses it and asks you for a real one.
- Sessions last 30 days and are stored hashed, so a copy of `head.db` doesn't give anyone a working
  session. Resetting a User's password ends all of their sessions.
- Wrong passwords are throttled: 5 per username or 50 per IP address, then a 30-minute wait.
- The Head API is HTTPS only, using a certificate authority the Head makes for itself at setup. Every
  machine CCC is installed on is told to trust it, so the Dashboard loads at `https://ccc.<name>`
  without a warning. `cuttle` trusts that authority and nothing else. A phone or laptop CCC wasn't
  installed on shows the browser's usual warning.

## What a User can reach

- **Their Tidepool.** It's a container with root inside it, but not on the Head. Each Tidepool has
  its own network, so it can't reach another's. It has no GPU, and its CPU, memory and disk are capped.
- **Their Dives.** Each Dive is a container on an Arm, limited to the GPUs it was given. Ending the
  Dive (or it running out of time) kills everything in it.
- **The Dashboard.** Everyone signed in sees the queue, history, Arms and Karma standings. Files and
  Tidepool controls are their own only.
- **From inside a Tidepool or Dive**, `cuttle` gets a limited session: it can read, and manage that
  User's own Dives. It never has Admin rights, even for an Admin.

Each User's containers run under their own range of user IDs on the host, so root inside one is
nobody special outside it. The details are in [[Isolation]].

## Where secrets live

| Secret | Where | Protection |
| --- | --- | --- |
| Passwords | `head.db` | argon2id hash |
| Sessions | `head.db`; `~/.ccc/session` on a User's machine | sha256 hash on the Head |
| Enrollment tokens | Printed once by `cuttle arm authorize` | One machine, one use, one hour. Not written to `cuttle`'s run logs |
| munge key | `/etc/munge/munge.key` on every node | `0400`, owned by `munge`. Handed out over the Mesh only |
| TLS keys | `/opt/ccc/tls/` | `0600`, readable only by the `ccc` account |
| SMTP password | `/var/lib/ccc/head/monitor.yaml` | Plain text, `0600`, `ccc` only |
| Backups | `/var/backups/ccc/` | `0700` root. They hold live session tokens |

Users keep their own secrets (API keys, SSH keys) as ordinary files in their Tidepool home.

## What CCC doesn't protect against

- **A hostile Arm owner.** They control the hardware.
- **Someone who gets into the Head.** It holds every account, and the key that signs the cluster's
  certificates.
- **Being on the open internet.** CCC is built for a Head reached over a LAN or campus network and the
  Mesh. Its self-signed certificate is fine there and not enough for public exposure.
- **A User's own code.** CCC keeps Users apart. It doesn't protect you from a package you chose to
  install in your own Dive.
- **One User hogging the queue.** [[Karma]] makes that expensive for them, but it isn't a hard limit.

## Related

- [[Admin/index|Admin]]: parent
- [[Isolation]]: how containers, user IDs and GPUs are kept apart
- [[Users and access]]: accounts, passwords and admin commands
- [[Mesh]]: who can join, and the munge key
- [[Backup]]: why backups need to stay private
