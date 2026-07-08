# arm — Glossary

> Local glossary for the Arm (compute node). Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details.

A compute node donating resources. One role, `--volatile` flag; runs on any OS via a Linux layer.

- **Linux layer** — the always-Linux execution env: native, WSL2 (Windows), or VM (macOS).
  Everything below runs inside it.
- **Node agent** — `slurmd` registered to the Head, plus the Apptainer runtime for Pods.
- **Retract** — volatile-owner instant reclaim: drain, grace-checkpoint, release.
- **Staging** — stage-in/stage-out of Spawn data for remote/volatile Arms (no live mount).
- **Preemption** — volatile Arms use Slurm `PreemptMode` (REQUEUE / SUSPEND).

Provisioned via `setup/` (dispatched by `cuttle setup`).
