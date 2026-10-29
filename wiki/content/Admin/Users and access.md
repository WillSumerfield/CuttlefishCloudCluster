---
title: Users and access
---
Everyone who uses CCC has an account on the Head: a [[Glossary#User|User]], or an
[[Glossary#Admin|Admin]]. You create accounts on the Head with `cuttle user add`, hand over a one-time
password, and the person picks their own the first time they sign in.

## Adding someone

On the Head:

```bash
cuttle user add alice
```

It prints a one-time password. Send it to Alice privately, along with the Head's address. The first
time she signs in, from the [[Glossary#Dashboard|Dashboard]] or with `cuttle login`, she has to choose
a new password.

Adding a User also creates their [[Glossary#Tidepool|Tidepool]] straight away: the full-size image is
reserved on disk and their container starts. If there isn't room for another full Tidepool, the add is
refused and says how much space is free.

Usernames are lowercase letters, digits, `-` and `_`, starting with a letter, at most 32 characters.
System names like `root` or `ubuntu` are refused, because the username is also the account inside
the User's Tidepool.

Add `--admin` to make an Admin:

```bash
cuttle user add bob --admin
```

### Getting them connected

Alice needs `cuttle` on her own machine, pointed at the Head. Two ways:

- **`cuttle setup --client`** asks for the Head's address, trusts the cluster's certificate, and logs
  her in. See [[Getting started]].
- **An installer bundle.** Run `cuttle build-installer` on the Head and give her the file yourself.
  It comes already pointed at this Head and already trusting it.

Users connect to the Head directly over the network: the Dashboard on 443, their Tidepool over SSH,
Dives through the relay ports. CCC is built for a Head that Users can reach on the same LAN or campus
network. It doesn't offer a way in from outside that network.

## Passwords and sessions

- Passwords need at least 8 characters and a mix of two of: lowercase, uppercase, digits, symbols.
- Each sign-in makes a **session** that lasts 30 days. The CLI keeps it in `~/.ccc/session`; the
  browser keeps it in a cookie. `cuttle logout` ends it.
- Users change their own password with `cuttle passwd`.
- **Forgotten password:** `cuttle user reset alice` on the Head. It prints a new one-time password
  and signs Alice out everywhere.
- **Lost the Admin password:** `cuttle user reset admin` on the Head works the same way.
- **Locked out:** 5 wrong passwords for one username, or 50 from one IP address, blocks further tries
  for 30 minutes after the last failure. Wrong old passwords in `cuttle passwd` count too. The count
  is held in memory, so `sudo systemctl restart ccc-head-api` clears it if you need to let someone
  back in sooner.

## Removing someone

Look at the [Users page](dashboard:/users) first. It lists every User with their Karma, any Escrow (work they
have on the cluster right now) and which Arms they donated, so you can see what removing them would
leave behind.

```bash
cuttle user remove alice
```

You type the username to confirm. It deletes the account, every session, and **their Tidepool:
the image and every file in it**. There's no undo.

If Alice donated an Arm, move it to someone else with `cuttle arm chown <arm> --user <new owner>`,
or take it out with `cuttle arm remove <arm>` (see [[Mesh]]).

## Users and Admins

An Admin can do everything a User can, and also sees the [Users page](dashboard:/users). Admins sit outside
the Karma economy: their Dives hold and spend no [[Glossary#Karma|Karma]], and they don't appear on
the Users page or in Karma standings. If you want to use the cluster for your own work, give yourself
an ordinary User account as well as the `admin` one.

Being an Admin in the Dashboard doesn't let you change anything there. The Users page is read-only.
Every change to accounts, Karma or Arms happens with `cuttle` **on the Head**, because the Head API
has no admin actions at all. See [[Security]].

## Admin commands on the Head

| Command | What it does |
| --- | --- |
| `cuttle user add <name> [--admin]` | Create an account, print a one-time password |
| `cuttle user list` | Every account, with `admin` and `must-change-pw` flags |
| `cuttle user reset <name>` | New one-time password, ends their sessions |
| `cuttle user remove <name>` | Delete the account and their Tidepool |
| `cuttle karma override <name> <amount> --note "why"` | Add Karma to a Balance, or take it away with a negative amount |
| `cuttle karma bump <dive id>` | Push one queued Dive up the queue. Touches nobody's Karma. `--clear` undoes it |
| `cuttle dive end <id>` | End anyone's Dive (off the Head, only your own) |
| `cuttle arm retract <name>` / `return <name>` | Pause or reopen anyone's Arm |
| `cuttle tidepool restart --user <name>` | Restart a User's Tidepool container |
| `cuttle tidepool reset --user <name>` | Rebuild it from the base image: keeps their home folder, drops everything installed outside it |
| `cuttle arm authorize <name> --user <owner>` | Let a new machine join as an Arm. See [[Mesh]] |

All of these need `sudo` on the Head.

## Related

- [[Admin/index|Admin]]: parent
- [[Getting started]]: what a new User does with their password
- [[Tidepool]]: what each User gets when you add them
- [[Karma]]: what override and bump change
- [[Security]]: why admin actions only work on the Head
- [[Mesh]]: adding and removing Arms
