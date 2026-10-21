# telemetry — Glossary

> Local glossary for Telemetry. Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details.

Monitoring & alerting.

- **Rogue-actor detection** — a check flagging compute happening outside Slurm (util above a threshold
  while nothing is scheduled). It watches the Head's own co-located Arm, and no donor's machine.
- **Head-box monitor** (`ccc-monitor`) — a systemd-timer script on the Head checking disk space,
  process liveness, and rogue-actor usage, emailing the operator over SMTP when one fires.
