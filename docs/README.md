# CCC documentation

This is the table of contents for the documentation.

For technical specifications, read [`CONTEXT.md`](../CONTEXT.md).

> Whatever you are trying to do, running `cuttle` on its own is a reasonable first move: it opens a
> menu of what this box can do right now and asks for whatever your choice needs. These pages give
> the typed form of each command, because it is precise and because it is what a script uses.

## I want to set a machine up

| Doc | For |
|---|---|
| [Set up an Arm](setup/arm.md) | Donating a machine to a cluster that already exists. |
| [Set up a client](setup/client.md) | Submitting work from a laptop that donates nothing. |
| [Set up the Head](setup/head.md) | Standing up the cluster itself. One box, once. |

## I want to use the cluster

| Doc | For |
|---|---|
| [Elements](Elements.md) | What CCC actually does, one paragraph each, linking into the detail. **Start here.** |
| [CLI](CLI.md) | Every `cuttle` command, one line each, pointing at the doc that explains it. |
| [Dashboard](Dashboard.md) | The web front door — the same cluster, in a browser. |
| [Troubleshooting](Troubleshooting.md) | It didn't work. Symptom → cause → fix. |

## I want to know how it works

| Doc | For |
|---|---|
| [Architecture](Architecture.md) | How the Head, the Arms and the Mesh fit together, and why. |
| [Security](Security.md) | What isolates one User's work from another's, and who is trusted with what. |
| [SMTP runbook](SMTP-Runbook.md) | Wiring up the alert mailer. |
| [TODO](TODO.md) | Designed, not built. |

## Elements

The per-capability docs live in [`elements/`](elements/) and are indexed by
[Elements.md](Elements.md):

[Spawns](elements/spawns.md) ·
[Environments](elements/environments.md) ·
[Karma](elements/karma.md) ·
[Arms](elements/arms.md) ·
[Arm Features](elements/arm-features.md) ·
[Tide Pool](elements/tide-pool.md) ·
[Users & access](elements/users-and-access.md) ·
[Isolation](elements/isolation.md) ·
[Storage](elements/storage.md) ·
[Mesh](elements/mesh.md) ·
[Telemetry](elements/telemetry.md) ·
[Backup & recovery](elements/backup-and-recovery.md)

## Working on CCC itself

[`CONTRIBUTING.md`](../CONTRIBUTING.md) — repo layout, commit convention, tests.
[`tests/MANUAL.md`](../tests/MANUAL.md) — the acceptance pass no automated test can do.
