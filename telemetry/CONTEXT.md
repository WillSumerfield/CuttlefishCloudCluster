# telemetry — Glossary

> Local glossary for Telemetry. Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details.

Monitoring & alerting. Spans Head + Arm.

- **Exporter** — Prometheus Node Exporter in each Arm's Linux layer (hardware health, utilisation).
- **Rogue-actor detection** — Python daemon flagging compute outside Slurm (>10% util, 0 jobs).
- **W&B injection** — `cuttle` injecting `WANDB_*` into the Pod for per-Spawn logging.
- **Alerts / webhooks** — user pings (email/Slack/Discord) on NaN, freeze, or Retract.
- **Dashboards** — Prometheus server + Grafana on the Head.

Provisioned via `setup/` (head-side aggregation + arm-side exporter).
