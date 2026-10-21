# Environments

## What it is

The **Environment** is the Python your code runs against inside its sandbox: a complete Python, its
own interpreter included, built from a dependency file you name and nothing else.

**There is no way to declare one right now.** Batch Spawns were the only thing that built one, and
they are gone in 2.0. Environments come back, optional,
with Dives: a named dependency file built on the Arm when the Dive starts.

What the Head still knows how to build (`ccc_head_api.environments`, unused until then):

| You name | CCC does |
|---|---|
| `pyproject.toml` | `uv sync` |
| `uv.lock` | `uv sync` |
| `requirements.txt` | `uv pip install -r` |
| `environment.yml` | `micromamba create` |

Any path, under any name — CCC reads the *extension* to pick the tool and never opens the file to
guess.

## Behavior

**An Environment inherits nothing.** It is not a layer over the base image, which carries no Python
at all — so **torch is absent** unless the file asks for it, and the Python version is whatever the
file says (`requires-python = "==3.11.14"` gets exactly that, fetched).

**It is built on the machine that runs it, as you, in a sandbox.** Only the declaration travels; the
Arm builds it inside a [Pod](isolation.md) as your [Guise](isolation.md), and keeps what it
downloads so a repeat build is fast. The build carries no credentials of yours, so a private git
dependency does not build.

**A package an Arm Feature supplies is replaced before the build** with an empty placeholder of the
same name and version, so the copy that runs is the Arm's own — see [Arm Features](arm-features.md).
