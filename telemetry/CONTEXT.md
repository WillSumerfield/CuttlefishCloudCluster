# telemetry — Glossary

> Local glossary for Telemetry. Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details.

Monitoring & alerting.

- **W&B injection** — `cuttle` injecting `WANDB_*` into the Pod for per-Spawn logging.
- **Rogue-actor detection** — a check flagging compute happening outside Slurm (util above a threshold
  while nothing is scheduled). Scoped today to the Head's own co-located Arm.
- **Head-box monitor** (`ccc-monitor`) — a systemd-timer script on the Head checking disk space,
  process liveness, and rogue-actor usage, emailing the operator over SMTP when one fires.

Fleet-wide (per-Arm) telemetry is not yet built.
