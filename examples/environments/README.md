# Environment test projects

Six one-file projects, one per path through `ccc_head_api.environments`. Each runs the same
`check_env.py`, which prints four lines and writes them to `$CCC_OUT/env.txt`:

```
interpreter: <path>     # /opt/ccc/env/... = an Environment was built; anything else = the base image
python: <version>       # the version it was built against — a declared requires-python, honoured
pyfiglet: <version>     # the declared package — proof the Environment was built
torch: <version>        # the base image's, and no Environment inherits it
```

## Running one

Each directory is its own project root — `cuttle run` uploads the working directory, and the manifest
you name is resolved inside it. So `cd` in first:

```sh
cd examples/environments/uv-requirements
cuttle run check_env.py --time 10 --vram 8 -o out --manifest requirements.txt
```

Each directory is named for the manifest it declares — `uv-project/` is `--manifest pyproject.toml`,
`conda/` is `--manifest environment.yml`, and so on. `default/` has none, which is the point of it.

`cuttle run` prints the Environment it will build before it returns. Results download themselves once
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

| directory | echo at submit | interpreter | python | pyfiglet | torch |
|---|---|---|---|---|---|
| `default/` | *refused* — nothing declared | — | — | — | — |
| `uv-requirements/` | `requirements.txt` (uv) | `/opt/ccc/env/venv/bin/python` | 3.12.x | 1.0.4 | **MISSING** |
| `uv-project/` | `pyproject.toml` (uv) | `/opt/ccc/env/venv/bin/python` | **3.11.14** | 1.0.4 | **MISSING** |
| `uv-lock/` | `uv.lock` (uv) | `/opt/ccc/env/venv/bin/python` | 3.11.x | 1.0.4 | **MISSING** |
| `conda/` | `environment.yml` (conda) | `/opt/ccc/env/bin/python` | 3.12.x | 1.0.4 | **MISSING** |
| `fails/` | `requirements.txt` (uv) | — | — | — | — |

Two columns carry the whole point.

`torch: MISSING` everywhere an Environment was built is the expected answer, not a bug. An
Environment inherits nothing — it is not a layer over the base image, it is a complete Python built
from your declaration and nothing else. Declare torch and you get the torch you asked for.

`python` is the other half. `uv-project/` pins `requires-python = "==3.11.14"`, a patch release the
cluster's image does not ship, and gets exactly it — an Environment carries its own interpreter, so
the version is yours to choose. `uv-requirements/` has no `.python-version`, so it gets CCC's pinned
default; add one and uv reads it instead. `conda/` names its own in `environment.yml`, as it always
did.

The two interpreter paths differ because the shapes do: micromamba owns `/opt/ccc/env` outright,
while a uv env keeps its venv one level down and the interpreter it downloaded beside it.

`uv-lock/` differs from `uv-project/` only in which file you name. All four that build ask for the
same `pyfiglet==1.0.4`, which PyPI and conda-forge both ship, so the rows differ only in the path
taken and never in what was asked for.

`default/` has no dependency file at all, and no `--manifest` you could pass would name one. It is
kept as the worked example of the refusal: every Spawn declares an Environment, so a project with
nothing to declare cannot be submitted.

### The failure case

```sh
cd examples/environments/fails
cuttle run check_env.py --time 10 --vram 8 -o out --manifest requirements.txt   # id; does NOT error
cuttle logs <id>                                       # the resolver's complaint
cuttle status <id>                                     # done, exit 1
```

The point of this one is that a broken manifest is a **failed Spawn**, not a rejected submission:
it appears in your history explaining itself, and settles for nothing because it never held a card.

## Two more things to try, with no new directories

```sh
cd examples/environments/uv-requirements
cuttle run check_env.py --time 10 --vram 8 -o out
```

Refused before anything is uploaded, naming the flag — there is no default and nothing is detected,
even with a `requirements.txt` sitting right there. At a terminal `cuttle run` asks you for it
instead.

```sh
cuttle run check_env.py --time 10 --vram 8 -o out --manifest nope.txt
```

Refused at submit, with a 422 and no Spawn — a manifest you *named* and that is not there is bad
input, decided before anything is Escrowed. (Contrast `fails/`, where the manifest exists and it is
the *build* that fails.)

## If every Spawn is refused with "this Head cannot build Environments"

The Head has no `uv`/`micromamba` in `/opt/ccc/tools`. Only the operator can fix it —
`cuttle setup --head` installs them.
