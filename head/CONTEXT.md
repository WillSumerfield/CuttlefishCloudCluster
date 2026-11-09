# head — Glossary

> Local glossary for the Head (control plane). Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details.

The cluster control plane. One logical role, Linux-only. Owns:

- **Scheduler** — Slurm control (`slurmctld`): allocations, queue, fairness.
- **Coordinator** — self-hosted Headscale admitting nodes to the CCC mesh (root term in `../CONTEXT.md`).
- **Enroll API** — mesh-only service that admits an enrolling Arm: hands it the munge key + `slurmctld`
  address. Two facts authorize it, and both trace to one **Admin** decision: the machine is on the
  **Mesh** (which only an **enrollment token** grants), and it answers to an Arm name the Admin named
  in advance. A machine on the Mesh that no Admin named gets nothing.
- **Head API** — the LAN+mesh service that backs both the web **Dashboard** and the **CLI** (one API,
  two clients). Serves the Dashboard SPA, exposes cluster state, and carries authenticated job
  submission (login, submit). A separate service from the **Enroll API** — the Enroll
  API is mesh-only and holds the munge key (a durable boundary), while the Head API must reach the LAN.
- **Dashboard** — the web app (SPA) over the Head API: the **primary** surface for viewing cluster +
  **Dive** state and for starting/managing Dives. Reachable on the CCC mesh **and** the LAN.
- **Accounts** — the Head is the cluster's identity store: the **User** table (argon2 hashes) +
  **Sessions** live here, authenticating logins and authorizing Dive submission.
- **Attribution ledger** — the Head's record of which **User** each **Dive** belongs to (root term:
  **Attribution**). The Head owns it because the Head is the sole submitter: the Head API `sbatch`es
  every Dive as that User's **Guise** (`sudo -u` the Guise, never root), so the scheduler's owner is a
  Guise, and `head.db` is where a Dive's *person* — and the Dives of an ended one — are known.
- **Prolog / Epilog** — Slurm job hooks that create/release a Dive's **Guise** identity around each
  run.

Hosts the head-side of other domains ([`../karma`](../karma), [`../telemetry`](../telemetry)).
Provisioned via `setup/`.
