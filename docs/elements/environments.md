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
  [Recipe](recipes.md) keeps the declaration exactly as you froze it, so re-running one re-resolves the
  same file and nothing else.

One package arrives that you did not ask for: **`pip`**, so `pip install` works if you open a
[Tide Pool](tide-pool.md) over it. Declare your own version and yours is kept.

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

**Watch it build** — the build runs first, and its output arrives on your Spawn's `[err]` stream
ahead of anything your script prints:

```sh
cuttle run train.py --time 60 --vram 24 -o out --manifest pyproject.toml --follow
```

There are six worked projects in [`examples/environments/`](../../examples/environments/), one per
path through the resolver, with a table of exactly what each should print.

## Behavior

### It is built on the machine that runs it

Your Spawn is placed first, and the machine it lands on builds your Environment as its first act —
before your script's first line, out of that machine's own connection to PyPI.

It used to be built on the Head and copied out, which was better in one way (no card was held while
packages downloaded) and fatal in another: a built Environment is several GB, the cluster's private
network is not a LAN, and a machine in another building could spend half an hour receiving one. Now
only your *declaration* travels, which is kilobytes.

Two things follow, and one of them costs you:

- **The card is yours while it builds**, and you are billed for that time like any other. A cold build
  of a large declaration is minutes; declare `--time` with that in mind.
- **Each machine keeps what it downloads.** The first Spawn to want a given torch on a given box pays
  for it; every later one gets it from disk. In practice a repeat run of the same declaration is fast.

### It is built as you, in a sandbox

A dependency declaration is code — a `pyproject.toml` can run arbitrary build hooks. So it is built
inside a [Pod](isolation.md), as your own [Guise](isolation.md), never as the cluster itself, with the
same walls your script gets a moment later. Your code has no way to become the cluster, on any machine.

### A private git dependency does not build

Your [GitHub key](github-keys.md) reaches your Spawn's *payload*, not this build. So a manifest
pinning a dependency to a private repository — `git+ssh://git@github.com/you/lib` — fails here, before
your script ever starts, with an authentication error from `git`.

Public git dependencies are fine. For a private one, clone it in your script instead, where the key
is available.

### Nobody owns one

Your Environment dies with the Spawn that built it; the next run builds it again. You do not own one,
hold one, or spend any allowance on one. Only the *declaration* persists, and only because it is part
of your code — including in a [Recipe](recipes.md), which freezes what you declared rather than a tree
built from it.

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
into a prefix of their own, on the same machine and in the same job, for any Spawn that requests the
Feature. That includes a [Tide Pool](tide-pool.md) that declared no manifest at all.

### What happens when it goes wrong

- **The build fails** (a package that will not resolve, a conflict) — the Spawn **fails**, and it
  fails on a machine it has already been given, so the [Karma](karma.md) held for it is spent, not
  returned. It is not a rejected submission: you get a Spawn id, it appears in your history explaining
  itself, and `cuttle logs <id>` has the resolver's complaint. This is the one real cost of building
  where the work runs — everything decidable *without* building is still refused for free, below.
- **You named a `--manifest` that is not there** — refused at submit, with no Spawn and nothing
  escrowed. A file you named and that does not exist is bad input, decided immediately. (Contrast the
  case above, where the file exists and it is the *build* that fails.)
- **You declared nothing** — refused before anything is uploaded, naming the flag that fixes it.
  `cuttle run` asks you for it rather than refusing, if you are at a terminal.
- **Every Spawn is refused with "this Head cannot build Environments"** — there is no
  `uv`/`micromamba` under `/opt/ccc/tools`, which only the operator can fix (`cuttle setup`). Every
  machine in the cluster is given them at setup. It used to fall back to the base image; it no longer
  does, because falling back would mean running your work against something you did not ask for.

`cuttle run` prints the Environment it will build before it returns, so you can see what you declared
without waiting for the Spawn.
