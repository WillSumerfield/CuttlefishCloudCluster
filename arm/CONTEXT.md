# arm — Glossary

> Local glossary for the Arm (compute node). Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details.

A compute node donating resources. One role, no kinds; runs on native Ubuntu 24.04.

- **Linux layer** — the execution env: native Ubuntu 24.04. Everything below runs inside it.
- **Node agent** — `slurmd` registered to the Head, plus the Podman runtime for Pods.
- **Node name** — the Arm's identity in the scheduler, chosen by its owner at Enrollment and
  fixed thereafter. Unique within a cluster.
- **Guise** — every Dive runs on an Arm as the submitting User's **Guise** (root term), never as its
  submitter's login, never as the **Mantle**, never as root. The same identity cluster-wide, so a Dive
  scheduled here is the same Dive it would have been on any other node.
- **Retract** — owner's instant reclaim (root term), on any Arm: drain, signal, release.
- **Staging** — a Dive's folders copied in over the Head's rsync daemon, and its main folder mirrored
  back to the Head (no live mount).

Provisioned via `setup/` (dispatched by `cuttle setup`).
