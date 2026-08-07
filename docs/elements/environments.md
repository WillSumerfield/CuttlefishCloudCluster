# Environments

## What it is

The **Environment** is the Python your code actually runs against inside its sandbox.

**Every Spawn declares one, and names the file it is built from.** There is no default and nothing is
detected — `--manifest` is required, alongside `--time`, `--vram` and `--output`:

```sh
cuttle run train.py --time 60 --vram 24 -o out --manifest pyproject.toml
```

You declare it the way you already do, in the dependency file your own tooling writes:

| You name | CCC does |
|---|---|
| `pyproject.toml` | `uv sync` |
| `uv.lock` | `uv sync` |
| `requirements.txt` | `uv pip install -r` |
| `environment.yml` | `micromamba create` |

Any path inside your project works, under any name you like — `envs/cuda12.yml`,
`requirements-gpu.txt`. CCC reads the *extension* to know which tool to use and never opens the file
to guess.

CCC will not pick one for you, and that is deliberate. A `pyproject.toml` in your repo might belong
to something you are vendoring rather than to this run, and the Environment decides what all your
work runs against — too much to infer from a file someone happened to commit.

**An Environment inherits nothing.** It is not a layer over the base image — it is a complete Python,
its own interpreter included, built from your declaration and nothing else. So **torch is gone**
unless you asked for it, whichever file you declared it in. That is the thing that surprises people,
and it is the price of the two things you get for it:

- **Your Python version is whatever you said it was.** A `pyproject.toml` with
  `requires-python = "==3.11.14"` gets exactly that, fetched for you — not the nearest thing the
  cluster happened to ship.
- **It is genuinely frozen.** Nothing in your Environment can change because the cluster's image
  changed underneath it — there is nothing in that image to change: it carries no Python at all. A
  [Recipe](recipes.md) goes further and keeps the built Environment itself, so re-running one is not
  merely re-resolving the same file.

Two packages arrive that you did not ask for: **`wandb`**, because that is what makes your Spawn log
to your own account, and **`pip`**, so `pip install` works if you open a
[Tide Pool](tide-pool.md) over it. Declare your own version of either and yours is kept.

## Typical use

**Name the file you already have:**

```sh
cuttle run train.py --time 60 --vram 24 -o out --manifest pyproject.toml
cuttle run train.py --time 60 --vram 24 -o out --manifest requirements.txt
```

**Keep several and pick one** — nothing has to sit in your project root, and nothing has to be
called what uv or conda call it:

```sh
cuttle run train.py --time 60 --vram 24 -o out --manifest requirements-gpu.txt
cuttle run train.py --time 60 --vram 24 -o out --manifest envs/cuda12.yml
```

**Pin your Python version** by declaring it, in `pyproject.toml`:

```toml
requires-python = "==3.11.14"
```

That exact interpreter is fetched and built against. The cluster's image has nothing to do with it.

**Watch it build** — build output is tagged `[env]`, ahead of your script's `[out]`/`[err]`:

```sh
cuttle run train.py --time 60 --vram 24 -o out --manifest pyproject.toml --follow
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

### An ad-hoc Spawn's is built for it and kept for none

Submit a project with `cuttle run` and its Environment dies with that Spawn's staging; the next run
builds it again. You do not own one, hold one, or spend any allowance on one. Only the *declaration*
persists, and only because it is part of your code.

A [Recipe](recipes.md) is the deliberate exception. The first run from one builds an Environment and
the Recipe keeps it, so every later run starts with no build — that is what "a Recipe is frozen" means
in practice. It is the only Environment you own, it counts against your Recipe allowance, and it is
released when you change what the Recipe declares.

### A package an Arm Feature supplies is replaced before the build

Some software is licensed to the *machine* rather than to you (vlearn is the built-in case), so the
copy that runs has to be the Arm's own — see [Arm Features](arm-features.md). You may still declare it
in your manifest, and often have to, since that is what makes your project resolve on your own
machine. CCC accepts the declaration and swaps the wheel for an empty placeholder of the same name and
version before building, repointing `uv.lock` at it so the build still verifies. The
[Feature](arm-features.md) deletes the placeholder on the Arm and puts the real package on your path.

Two things are refused at submit rather than discovered later: declaring such a package **without also
requesting its Feature** (the Spawn would build cleanly and die at `import`), and pointing a path
source **further up than a directory beside your project** (CCC only unpacks your project, so there is
nothing above it to point at).

A Feature's *system libraries* are handled separately and need no declaration at all — CCC builds them
into a prefix of their own for any Spawn that requests the Feature.

### What happens when it goes wrong

- **The build fails** (a package that will not resolve, a conflict) — the Spawn **fails having held
  nothing** and is billed nothing. It is not a rejected submission: you get a Spawn id, it appears in
  your history explaining itself, and `cuttle logs <id>` has the resolver's complaint.
- **You named a `--manifest` that is not there** — refused at submit, with no Spawn and nothing
  escrowed. A file you named and that does not exist is bad input, decided immediately. (Contrast the
  case above, where the file exists and it is the *build* that fails.)
- **You declared nothing** — refused before anything is uploaded, naming the flag that fixes it.
  `cuttle run` asks you for it rather than refusing, if you are at a terminal.
- **Every Spawn is refused with "this Head cannot build Environments"** — the Head has no
  `uv`/`micromamba` under `/opt/ccc/tools`, which only the operator can fix (`cuttle setup --head`).
  It used to fall back to the base image; it no longer does, because falling back would mean running
  your work against something you did not ask for.

`cuttle run` prints the Environment it will build before it returns, so you can see what you declared
without waiting for the Spawn.
