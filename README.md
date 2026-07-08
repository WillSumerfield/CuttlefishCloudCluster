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
| [`cli/`](cli/) | The `cuttle` tool — Go host launcher + Python core (recipes, submit, auto-detect). |
| [`head/`](head/) | Control plane: `slurmctld`, `slurmdbd` + MariaDB, scheduler config, prolog/epilog. |
| [`arm/`](arm/) | Compute node: Linux-layer bring-up (WSL2/VM), `slurmd`, Apptainer, Retract, staging. |
| [`karma/`](karma/) | Karma economy: wallets, pricing (the Tide), congestion, FairShare sync, override (SQLite). |
| [`telemetry/`](telemetry/) | Exporters, rogue-actor detection, W&B injection, alerts/webhooks, Grafana. |
| [`gateway/`](gateway/) | Network fabric (WireGuard mesh) + SSO. |
| [`shared/`](shared/) | Cross-context code: CLI↔Head protocol, common types, metric schemas. |
| [`docs/`](docs/) | Design spec and documentation. |

## Cross-platform reality

Arms run on any OS via a **Linux substrate** (`cuttle setup` provisions native / WSL2 / VM). GPU is
**NVIDIA-only**; macOS Arms are **CPU-only** (no CUDA passthrough).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) — Conventional Commits (scope = context), rebase not merge,
branch + PR.
