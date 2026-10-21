# GitHub keys

## What it is

Your work lives in a private repository. Your [Spawn](spawns.md) runs on a machine you have never
touched, as an account with no home directory, and it needs to `git clone` that repository — or
install a dependency pinned to it.

So the Head keeps an SSH key for you, and puts it inside every Spawn you submit.

**The Head generates the key; you never see the private half.** There is nothing to paste and nothing
to leak from your terminal: the Head mints an ed25519 keypair, seals the private half under your
password, and hands you the public half to register with GitHub. That public half is the only part of
this key anyone ever sees.

Inside the Spawn it is served by an **ssh-agent**, started before your code runs and gone when the job
ends. The key is never written to a file in the Pod, never appears in the environment, and never shows
up in `ps`.

## Typical use

**Mint one:**

```sh
cuttle github mint
```

It prints a public key and waits. Press **c** to copy it, paste it at
[github.com/settings/ssh/new](https://github.com/settings/ssh/new) — any title you like — and the
command confirms which GitHub account it authenticates as, then exits. You can skip the wait with
Ctrl-C, but until the key is registered it opens nothing.

Copying works over SSH: with no clipboard tool on the box, CCC asks your *terminal* for its own
clipboard instead, which is the one you actually want when you are logged into the Head from
somewhere else.

**See it again, and copy it:**

```sh
cuttle github show --copy
```

**Check GitHub still has it:**

```sh
cuttle github verify
```

**Run one Spawn without it:**

```sh
cuttle run train.py --time 60 --vram 24 -o out --manifest pyproject.toml --no-git
```

**Stop carrying one:**

```sh
cuttle github clear
```

From the [Dashboard](../Dashboard.md), all of this is on the Account page, and the per-Spawn switch is
a toggle in the composer.

## Commands

| | |
|---|---|
| `cuttle github mint` | Generate a key and print its public half. Replacing one asks first. |
| `cuttle github show` | Print your public key, in full. `--copy` puts it on your clipboard. |
| `cuttle github verify` | Ask GitHub whether it is registered, and who it authenticates as. |
| `cuttle github clear` | Forget it. Your Spawns stop carrying one. |

## Behavior

### It is on for every Spawn, and off per Spawn

Once you have a key, every Spawn you submit carries it — from either client, from any machine.
`cuttle run --no-git` leaves it out of one, and the Dashboard's composer has the same switch.

That default is the opposite of what you might expect from a secret, and it is deliberate: you did not
choose this key's *value*, only whether it exists. Having minted one is the choice to use it. Read
[the cost](#what-it-costs-you) before deciding that is fine for every Spawn you run.

A [Recipe](recipes.md) freezes the choice along with everything else, so a Recipe saved with the key
declined keeps declining it.

### What reaches your repositories, and what does not

The Head sets `GIT_SSH_COMMAND` inside the Pod, so anything that shells out to `git` picks the key up
automatically:

- `git clone git@github.com:you/private-repo`
- `pip install git+ssh://git@github.com/you/lib`
- a `uv` or Poetry dependency pinned to a git URL

A bare `ssh` command in your own script does **not** get it. There is nowhere in the Pod to put an
`~/.ssh/config` — your account there has no home directory by design ([Isolation](isolation.md)) — so
the configuration rides on git's own environment variable instead of a file.

This applies to the Spawn's payload only. It does **not** apply while your
[Environment](environments.md) is being built, so a manifest with a private git dependency still fails
at build time. Nor does it reach a [Tide Pool](tide-pool.md) shell.

### GitHub's host key is pinned

CCC ships GitHub's ed25519 host key and checks against that one key alone. Nothing in a Spawn is ever
asked to accept an unknown host — there is nobody there to answer, and a job that shrugs and continues
is not a job that verified anything.

### The private half never leaves the Head

Not to you, not to an Admin, not to anyone holding a copy of the database. It is sealed under a key
derived from your password, with all the same consequences, spelled out
in [Security](../Security.md). The two that matter most here:

- **An Admin password reset destroys it.** Mint a new one and register it again.
- **A Spawn submitted with no session carries no key**, which includes `cuttle run --node`.

### `verify` has three answers, not two

`cuttle github verify` distinguishes *GitHub refused this key* from *nobody could ask* — no route out
of the Head, no `ssh`, a timeout. They look identical at the exit-code level and mean opposite things:
the first is a key you have not registered yet, the second is a broken Head. Collapsing them would
send you to GitHub to fix a network fault.

### Replacing one breaks the old one

Minting again generates a fresh keypair. The public key already on your GitHub account stops matching
anything, and every Spawn fails to authenticate until you paste the new one in. Both clients ask
before doing it; `cuttle github mint --force` skips the question.

`cuttle github clear` leaves that stale public key sitting on your GitHub account. It opens nothing,
but remove it there too.

## What it costs you

**This is an account key.** It reaches everything your GitHub account reaches — every repository, every
organization you belong to — and it can **write**, not only read. GitHub enforces global uniqueness on
deploy keys, so a per-repository key would mean one repository per User; an account key is what makes
one key useful at all.

Inside the Pod, the agent holding it is reachable by any process running there. That is what makes it
work, and it is not a boundary that can be tightened: your code, and every dependency your Environment
installed, runs as the same account. For the life of a Spawn, anything in that Pod can sign a git
operation as you.

The Head never hands out the key itself, so nothing can steal it and use it tomorrow. But for those
minutes or hours, it is a signing oracle for your whole GitHub account.

`--no-git` is the way out, and running someone else's code is the reason to use it.
