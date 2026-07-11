# head — Glossary

> Local glossary for the Head (control plane). Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details.

The cluster control plane. One logical role, Linux-only. Owns:

- **Scheduler** — Slurm control (`slurmctld`): allocations, queue, fairness.
- **Coordinator** — self-hosted Headscale admitting nodes to the CCC mesh (root term in `../CONTEXT.md`).
- **Enroll API** — mesh-only service that admits an approved Arm: hands it the munge key + `slurmctld`
  address. Being on the mesh *is* the authorization; no token.
- **Dashboard** — read-only web view of live node + Spawn state (the enrollment-free read API + SPA),
  served on the CCC mesh **and** the LAN. A separate service from the Enroll API, since it holds no
  secret and must reach the LAN.
- **Accounting** — `slurmdbd` + MariaDB: job accounting and FairShare (drives the Karma sync). *(Economy phase.)*
- **Prolog / Epilog** — Slurm job hooks used for setup, teardown, and webhook fires.

Hosts the head-side of other domains ([`../karma`](../karma), [`../telemetry`](../telemetry)).
Provisioned via `setup/`.
