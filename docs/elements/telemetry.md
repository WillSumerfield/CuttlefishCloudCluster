# Telemetry

## What it is

The Head's own health monitor: a few checks on the Head's machine that email the operator when one
fires. It is the operator's business; nothing here touches a User's work.

## Admin — on the Head

`cuttle setup --head` installs **`ccc-monitor`**, a systemd timer that runs three checks every
5 minutes and emails you when one fires:

- **Disk** — the Head's root filesystem past 85% full.
- **Services** — any of the Head's services not active (the scheduler, the coordinator, the APIs, the
  Karma tick).
- **Rogue usage** — GPU or CPU above 10% busy on the Head with **no Slurm job running on that node**,
  sustained over three consecutive checks. That is compute happening outside the scheduler: someone
  running work on the Head by hand, or something that should not be there.

Each check alerts independently and at most once a day, so a stuck disk alert cannot suppress a later
rogue-usage one.

SMTP credentials are asked for during `cuttle setup --head` and can be skipped — the checks still run
and print to `journalctl -u ccc-monitor`, they just cannot email. Fill them in later by re-running
setup. See the [SMTP runbook](../SMTP-Runbook.md).

Rogue detection watches the **Head's own machine**. It does not watch donors' boxes: an Arm is a
machine CCC does not own, and watching one the way the Head watches itself is not something CCC does.
