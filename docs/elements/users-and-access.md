# Users & access

## What it is

A **User** is a person with an account on the Head. Accounts are created by the Admin — there is no
sign-up.

Your account is what ties everything together. Every [Spawn](spawns.md) you submit is yours, any
[Arms](arms.md) you donate are marked as yours, your [Karma](karma.md) balance hangs off it, and
your [W&B key](telemetry.md) and your [GitHub key](github-keys.md) are held under it — under your
*password*, specifically, which is why an Admin password reset destroys both.

Logging in gets you a **session**: a token stored on the box you logged in from. It authorizes *you*
to submit and to read your own things. It is a completely different thing from mesh membership,
which authorizes *machines*.

## Typical use

**Log in** — the first time, tell it where the Head is:

```sh
cuttle login --head <head-address>
```

After that, just:

```sh
cuttle login
```

**Check who this box thinks you are:**

```sh
cuttle whoami
```

**Change your password:**

```sh
cuttle passwd
```

**Log out of this box** — revokes the session on the Head and deletes the local token:

```sh
cuttle logout
```

## Commands

| | |
|---|---|
| `cuttle login` | Log in and store a session on this box. |
| `cuttle logout` | Revoke it and delete the local token. |
| `cuttle whoami` | Who you are logged in as. |
| `cuttle passwd` | Change your password. |
| `cuttle user add` | **admin** — see below. |
| `cuttle user list` | **admin** — see below. |
| `cuttle user remove` | **admin** — see below. |
| `cuttle user reset` | **admin** — see below. |

## Behavior

### Your first login

The Admin creates your account and gives you a one-time temporary password. Your first `cuttle login`
**forces you to change it** — you cannot decline and carry on.

### Sessions are per-box

The token lands in `~/.ccc/session` on the machine you logged in from. Log in from a laptop and
a desktop and you have two sessions; `cuttle logout` ends the one you are sitting at.

A session is also what makes your stored keys usable: outside one the Head cannot reach them at all.

The [Dashboard](../Dashboard.md) is the same account and the same cluster, in a browser.

### Admins are different, in one specific way

The Admin is **outside the economy**. They earn no Karma, pay none, and hold no Balance. Their Spawns
are never escrowed, never settled, and never lose contention. An Arm the Admin owns is a **Commons
Arm** and the Karma spent there is destroyed rather than transferred to anyone.

That is a role for *running* the cluster, not for *using* it. If the operator wants to actually do
work on the cluster, they do it as an ordinary User with an ordinary account.

## Admin — on the Head

All four of these run on the Head. The Head's network API exposes no administrative writes at all —
see [Security](../Security.md#the-head-api-has-no-admin-writes).

**`cuttle user add <username>`** creates a User and prints a one-time temporary password, which they
must change on first login. `--admin` makes them an Admin.

**`cuttle user list`** — everyone.

**`cuttle user remove <username>`** deletes a User.

**`cuttle user reset <username>`** issues a fresh temporary password and **kills that User's existing
sessions**, everywhere. This is the lever to pull if someone's laptop goes missing.
