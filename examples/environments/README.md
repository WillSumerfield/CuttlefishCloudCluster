# Environment test projects

Six one-file projects, one per path through `ccc_head_api.environments`. Each runs the same
`check_env.py`, which prints four lines:

```
interpreter: <path>     # /opt/ccc/env/... = an Environment was built; anything else = the base image
python: <version>       # the version it was built against — a declared requires-python, honoured
pyfiglet: <version>     # the declared package — proof the Environment was built
torch: <version>        # the base image's, and no Environment inherits it
```

## Running one

Nothing runs them on the 2.0 branch. They were submitted as batch Spawns with
`--manifest <file>`, and batch Spawns are gone; they come back as a Dive's dependency file. The
table below is what each one is expected to print once something builds it again.

Each directory is named for the manifest it declares — `uv-project/` is `pyproject.toml`, `conda/` is
`environment.yml`, and so on. `default/` has none, which is the point of it.

## What each one should give you

| directory | manifest | interpreter | python | pyfiglet | torch |
|---|---|---|---|---|---|
| `default/` | *none* | — | — | — | — |
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

`default/` has no dependency file at all, so nothing is built for it. `fails/` names a package that
does not exist, so its build fails.

## If every build is refused with "this Head cannot build Environments"

The Head has no `uv`/`micromamba` in `/opt/ccc/tools`. Only the operator can fix it —
`cuttle setup --head` installs them.
