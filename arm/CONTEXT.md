# arm — Glossary

> Local glossary for the Arm (compute node). Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details.

A compute node donating resources. One role, no kinds; runs on any OS via a Linux layer.

- **Linux layer** — the always-Linux execution env: native, or WSL2 (Windows). Everything below runs
  inside it.
- **Node agent** — `slurmd` registered to the Head, plus the Apptainer runtime for Pods.
- **Node name** — the Arm's identity in the scheduler, chosen by its owner at Enrollment and
  fixed thereafter. Unique within a cluster.
- **Mantle** — every Spawn runs on an Arm as the **Mantle** (root term), never as its submitter. An
  Arm must therefore carry the Mantle identity itself: it is the same identity cluster-wide, so a
  Spawn scheduled here is the same Spawn it would have been on any other node.
- **Retract** — owner's instant reclaim (root term), on any Arm: drain, signal, release.
- **Staging** — stage-in/stage-out of Spawn data for remote Arms (no live mount).

Provisioned via `setup/` (dispatched by `cuttle setup`).
