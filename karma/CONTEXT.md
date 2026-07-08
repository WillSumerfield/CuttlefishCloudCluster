# karma — Glossary

> Local glossary for the Karma economy. Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details.

The token economy. Deploys on the Head; state in a SQLite DB (WAL, single-writer).

- **Wallet** — a user's Karma balance and transaction ledger.
- **The Tide** — dynamic hourly pricing (High Tide peak / Low Tide off-peak).
- **Congestion Penalty** — extra burn when the queue backs up or Spawns are throttled.
- **FairShare sync** — periodic push of balances into Slurm FairShare (`sacctmgr ... RawUsage`).
- **Override** — admin-only add/remove of Karma.
- **Early-failure rule** — Spawn failing <1h still spends Karma, treated as done, no requeue.

Provisioned via `setup/`.
