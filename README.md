# Cuttlefish Cloud Cluster (CCC)

A distributed GPU/CPU computing cluster: donate spare compute from any machine, run sandboxed
experiments, and settle it all through a Karma token economy.

> Names are themed (cuttlefish) and canonical. See [`CONTEXT.md`](CONTEXT.md) for the glossary and
> [`docs/DesignDoc.md`](docs/DesignDoc.md) for the full design + locked tech decisions.

## Repository layout

Monorepo, split by **deployment context**. Each top-level dir is one bounded context with its own
`CONTEXT.md`; deployable contexts also carry a `setup/` for provisioning.

| Context | Role |
|---|---|
| [`cli/`](cli/) | The `cuttle` tool — a single Python CLI (setup/teardown, submit, follow, admin). |
| [`head/`](head/) | Control plane: Headscale coordinator, `slurmctld`, enroll API + web dashboard, stage-in/out; `slurmdbd`+MariaDB (economy phase). |
| [`arm/`](arm/) | Compute node: Linux-layer bring-up (WSL2/VM), `slurmd`, Apptainer, Retract, staging. |
| [`karma/`](karma/) | Karma economy: wallets, pricing (the Tide), congestion, FairShare sync, override (SQLite). |
| [`telemetry/`](telemetry/) | Exporters, rogue-actor detection, W&B injection, alerts/webhooks, Grafana. |
| [`gateway/`](gateway/) | Network fabric (WireGuard mesh) + built-in Head auth (sign-on). |
| [`shared/`](shared/) | Cross-context code: CLI↔Head protocol, common types, metric schemas. |
| [`docs/`](docs/) | Design spec and documentation. |

## Cross-platform reality

Arms run on any OS via a **Linux substrate** (`cuttle setup` provisions native / WSL2 / VM). GPU is
**NVIDIA-only**; macOS Arms are **CPU-only** (no CUDA passthrough).

## Installation

`cuttle` is a Python CLI installed with [uv](https://docs.astral.sh/uv/).

**From the repo** (the first Head, or any dev box — needs internet for deps):

```sh
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh   # install uv
uv tool install --editable ./cli/core                              # drops `cuttle` on PATH
```

`--editable`: a Head resolves its own source tree (`head/api`, `head/web`) relative to the install,
so host duties (`setup --head`, `build-installer`) need it pointed at the checkout.

Then provision the box: `cuttle setup --head` (control plane), `--arm` (donor node), or
`--client` (submit only).

**From a bundle** (clients/Arms). On the Head, build a portable installer — run there, it bakes in
the Head's address and pinned certificate, so the client comes pre-pointed and pre-pinned:

```sh
cuttle build-installer            # -> ./cuttle-installer.tar.gz
```

Copy it to the target box and run it:

```sh
tar xzf cuttle-installer.tar.gz
cuttle-installer/install.sh       # installs uv if needed, installs cuttle, writes config + pin
cuttle login                      # (pre-pointed bundle) — or `cuttle setup --client` for a generic one
```

**Uninstall.** The bundle ships an `uninstall.sh` that tears the box down (auto-detecting its role)
and then removes the tool:

```sh
cuttle-installer/uninstall.sh
```

To only remove the tool (leaving `~/.ccc`): `uv tool uninstall cuttle`. `uv`-tool isolation means
that takes cuttle's Python deps with it.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) — Conventional Commits (scope = context), rebase not merge,
branch + PR.
