# Environment test projects

Six one-file projects, one per path through `ccc_head_api.environments`. Each runs the same
`check_env.py`, which prints three lines and writes them to `$CCC_OUT/env.txt`:

```
interpreter: <path>     # /opt/ccc/env/... = an Environment was built; anything else = the base image
pyfiglet: <version>     # the declared package — present only if a manifest was found AND built
torch: <version>        # the base image's. Survives a uv build, gone through a conda one
```

## Running one

Each directory is its own project root — `cuttle run` uploads the working directory, and detection
looks at the root of what was uploaded. So `cd` in first:

```sh
cd examples/environments/uv-requirements
cuttle run check_env.py --time 10 --vram 8 -o out
```

`cuttle run` prints the Environment it detected before it returns. Results download themselves once
the Spawn ends, so the answer is:

```sh
cat spawn-<id>-outputs/env.txt
```

Add `-f` to watch it happen instead of waiting — build output is tagged `[env]`, ahead of the
script's own `[out]`/`[err]`.

While the env builds the Spawn shows as `building` rather than `pending` (`cuttle queue`). That is
the Spawn held before Slurm may place it; the build overlaps the wait for a card rather than being
added to it.

## What each one should give you

| directory | echo at submit | interpreter | pyfiglet | torch |
|---|---|---|---|---|
| `default/` | no dependency file found | base image | MISSING | present |
| `uv-requirements/` | `requirements.txt` … on top of | `/opt/ccc/env/bin/python` | 1.0.4 | present |
| `uv-project/` | `pyproject.toml` … on top of | `/opt/ccc/env/bin/python` | 1.0.4 | present |
| `uv-lock/` | `uv.lock` … on top of | `/opt/ccc/env/bin/python` | 1.0.4 | present |
| `conda/` | `environment.yml` … replaces | `/opt/ccc/env/bin/python` | 1.0.4 | **MISSING** |
| `fails/` | `requirements.txt` … on top of | — | — | — |

`torch: MISSING` in the conda row is the expected answer, not a bug: uv layers on the base image,
conda replaces it. `uv-lock/` differs from `uv-project/` only in which file the echo names — the lock
is preferred when both are there. All five build the same `pyfiglet==1.0.4`, which PyPI and
conda-forge both ship, so the rows differ only in the path taken and never in what was asked for.

### The failure case

```sh
cd examples/environments/fails
cuttle run check_env.py --time 10 --vram 8 -o out     # returns an id; does NOT error
cuttle logs <id>                                       # the resolver's complaint
cuttle status <id>                                     # done, exit 1
```

The point of this one is that a broken manifest is a **failed Spawn**, not a rejected submission:
it appears in your history explaining itself, and settles for nothing because it never held a card.

## Two more things to try, with no new directories

```sh
cd examples/environments/uv-requirements
cuttle run check_env.py --time 10 --vram 8 -o out --manifest none
```

Declines the build outright — same project, but the echo says "no dependency file found" and pyfiglet
comes back MISSING. A file in a repo is not always an instruction to the cluster.

```sh
cuttle run check_env.py --time 10 --vram 8 -o out --manifest nope.txt
```

Refused at submit, with a 422 and no Spawn — a manifest you *named* and that is not there is bad
input, decided before anything is Escrowed. (Contrast `fails/`, where the manifest exists and it is
the *build* that fails.)

## If every row says MISSING

The Head has no `uv`/`micromamba` in `/opt/ccc/tools`. That is a Head provisioned before Environments
existed, and it degrades to the base image on purpose rather than refusing Spawns that used to work —
the submit echo says so. `cuttle setup --head` installs them.
