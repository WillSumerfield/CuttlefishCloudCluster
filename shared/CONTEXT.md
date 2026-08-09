# shared — Glossary

> Local glossary for shared code. Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details.

Cross-context concepts with no single owner — the contract the CLI and Head agree to without either
side owning a shared package (see `tests/CONTEXT.md`'s **Seam**). No dedicated code lives here yet, and
no `setup/` (not a deployable role).

- **Protocol** — the CLI↔Head message contract (`cuttle` core ↔ control plane).
- **Common types** — shared domain types (Recipe, Spawn, roles).
- **Metric schemas** — shared shapes for telemetry/economy data.

Note: distinct from **Shared storage** (the cluster filesystem) — this is code, not storage.
