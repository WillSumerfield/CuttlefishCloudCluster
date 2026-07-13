# head — Glossary

> Local glossary for the Head (control plane). Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details.

The cluster control plane. One logical role, Linux-only. Owns:

- **Scheduler** — Slurm control (`slurmctld`): allocations, queue, fairness.
- **Coordinator** — self-hosted Headscale admitting nodes to the CCC mesh (root term in `../CONTEXT.md`).
- **Enroll API** — mesh-only service that admits an approved Arm: hands it the munge key + `slurmctld`
  address. Being on the mesh *is* the authorization; no token.
- **Head API** — the LAN+mesh service that backs both the web **Dashboard** and the **CLI** (one API,
  two clients). Serves the Dashboard SPA and exposes cluster state; from the auth phase it also carries
  authenticated job submission (login, project upload, submit). A separate service from the **Enroll
  API** — the Enroll API is mesh-only and holds the munge key (a durable boundary), while the Head API
  must reach the LAN.
- **Dashboard** — the web app (SPA) over the Head API: the **primary** surface for viewing cluster +
  Spawn state and, from the auth phase, submitting/managing jobs. Started read-only (Phase 1); grows
  interactive per phase. Reachable on the CCC mesh **and** the LAN.
- **Accounts** — the Head is the cluster's identity store: the **User** table (argon2 hashes) +
  **Sessions** live here, authenticating logins and authorizing job submission. *(Auth phase.)*
- **Attribution ledger** — the Head's record of which **User** submitted each **Spawn** (root term:
  **Attribution**). The Head owns it because the Head is the sole submitter: every Spawn is launched
  by the Head API as the **Mantle**, so the scheduler's own owner field says "Mantle" for all of them
  and this ledger is the only place a Spawn's person is known. *(Auth phase.)*
- **Accounting** — `slurmdbd` + MariaDB: job accounting and FairShare (drives the Karma sync). *(Economy phase.)*
- **Prolog / Epilog** — Slurm job hooks used for setup, teardown, and webhook fires.

Hosts the head-side of other domains ([`../karma`](../karma), [`../telemetry`](../telemetry)).
Provisioned via `setup/`.
