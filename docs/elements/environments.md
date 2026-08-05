# Environments

## What it is

The **Environment** is the Python your code actually runs against inside its sandbox.

Every [Spawn](spawns.md) gets a base one — the same on every machine in the cluster, with torch and
CUDA already in it. If that is enough, you declare nothing and there is nothing here to learn.

If you need more, you declare it the way you already do: the dependency file your own tooling
writes. CCC looks in the root of what you uploaded and finds it itself.

| You have | CCC does |
|---|---|
| `uv.lock` | uv sync, layered **on top of** the base image |
| `pyproject.toml` | uv, layered **on top of** the base image |
| `requirements.txt` | uv, layered **on top of** the base image |
| `environment.yml` | conda, which **replaces** the base image |
| none of these | you get the base image |

`uv.lock` wins if both it and a `pyproject.toml` are there.

The layered-versus-replaced distinction is the one that surprises people: after a conda build,
**torch is gone** unless your `environment.yml` asked for it. uv adds to what is already there;
conda starts over.

## Typical use

**Do nothing.** Most Spawns need no Environment at all.

**Name a different file** when your repo has several, or when the one in the root is not the one you
want the cluster to use:

```sh
cuttle run train.py --time 60 --vram 24 -o out --manifest requirements-gpu.txt
cuttle run train.py --time 60 --vram 24 -o out --manifest envs/cuda12.yml
```

**Decline the build** when there is a dependency file in your repo that is not an instruction to the
cluster:

```sh
cuttle run train.py --time 60 --vram 24 -o out --manifest none
```

**Watch it build** — build output is tagged `[env]`, ahead of your script's `[out]`/`[err]`:

```sh
cuttle run train.py --time 60 --vram 24 -o out --follow
```

There are six worked projects in [`examples/environments/`](../../examples/environments/), one per
path through the resolver, with a table of exactly what each should print.

## Behavior

### It is built before your Spawn is placed

The Head builds your Environment first, and only then goes looking for a machine. Two consequences,
both good for you:

- **No GPU is held while packages download.** Nobody is paying for a card that is running `pip`.
- **Building costs no Karma.** It happens before anything is placed, so there is nothing to bill.

While it builds, your Spawn shows as `building` rather than `pending` in `cuttle queue`. That is the
Spawn being held *before* the scheduler may place it — the build overlaps the wait for a card rather
than being added on to it.

### It is built as you, in a sandbox

A dependency declaration is code — a `pyproject.toml` can run arbitrary build hooks. So the Head
builds it inside a [Pod](isolation.md), as your own [Guise](isolation.md), never as the cluster
itself. Your code has no way to become the cluster, on any machine, including the Head.

### It is built for one Spawn and kept for none

The Environment dies with the Spawn's staging, and the next run builds it again. You do not own one,
hold one, or spend any allowance on one. Only the *declaration* persists, and only because it is part
of your code.

### What happens when it goes wrong

- **The build fails** (a package that will not resolve, a conflict) — the Spawn **fails having held
  nothing** and is billed nothing. It is not a rejected submission: you get a Spawn id, it appears in
  your history explaining itself, and `cuttle logs <id>` has the resolver's complaint.
- **You named a `--manifest` that is not there** — refused at submit, with no Spawn and nothing
  escrowed. A file you named and that does not exist is bad input, decided immediately. (Contrast the
  case above, where the file exists and it is the *build* that fails.)
- **Every Spawn silently gets the base image** — the Head has no `uv`/`micromamba` under
  `/opt/ccc/tools`. That is a Head provisioned before Environments existed; it degrades to the base
  image rather than refusing Spawns that used to work, and the submit echo says so. `cuttle setup
  --head` installs them.

`cuttle run` prints the Environment it detected before it returns, so you can see what it decided
without waiting for the Spawn.
