# head — Glossary

> Local glossary for the Head (control plane). Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details.

The cluster control plane. One logical role, Linux-only. Owns:

- **Scheduler** — Slurm control (`slurmctld`): allocations, queue, fairness.
- **Accounting** — `slurmdbd` + MariaDB: job accounting and FairShare (drives the Karma sync).
- **Prolog / Epilog** — Slurm job hooks used for setup, teardown, and webhook fires.

Hosts the head-side of other domains ([`../karma`](../karma), [`../telemetry`](../telemetry)).
Provisioned via `setup/`.
