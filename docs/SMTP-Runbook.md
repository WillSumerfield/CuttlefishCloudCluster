# SMTP runbook

Wiring up the Head's alert mail. Operator only; nothing here affects Users.

The Head runs `ccc-monitor` on a 5-minute systemd timer, checking disk, service liveness and rogue
GPU/CPU usage. When a check fires it emails you. Without SMTP configured the checks still run — they
just log instead. See [Telemetry](elements/telemetry.md#admin--on-the-head) for what the checks are.

## Configuring it

Re-run setup and answer the SMTP prompts:

```sh
cuttle setup --head
```

Six fields:

| | |
|---|---|
| SMTP host | e.g. `smtp.gmail.com` |
| SMTP port | `587`. STARTTLS is always used. |
| SMTP username | usually the full address |
| SMTP password | blank keeps the existing one |
| From address | defaults to the username |
| To address | your own inbox |

Leaving the **host blank skips the whole thing** and leaves alerting unconfigured. On a re-run every
field defaults to what is already stored, so pressing Enter through all of them changes nothing.

The result is written to `/var/lib/ccc/monitor.yaml`, owned `ccc:ccc` at `0600`. It contains a live
password in plaintext — that file's permissions are the only thing protecting it.

## Gmail and other providers with 2FA

Your normal account password will not work. Create an **app password** in your provider's account
settings and use that as the SMTP password. `smtp.gmail.com:587` otherwise needs nothing special.

## Checking it works

The monitor only mails when a check *fires*, so there is no test button. Force one instead:

```sh
sudo systemctl start ccc-monitor.service     # run the checks now
journalctl -u ccc-monitor -n 50              # what happened
```

Three things you might see:

| Log line | Means |
|---|---|
| `<check> fired, no SMTP conf at …` | Not configured. Re-run setup. |
| `<check> fired but email failed: …` | Configured, but the send failed. The error is the provider's. |
| nothing | Nothing fired. The Head is healthy. |

The easiest check to trigger deliberately is `services` — stop something the monitor watches, run the
service, then start it again.

## Alert behaviour

Each check emails **independently** and at most **once per day**. A stuck disk alert cannot suppress a
later rogue-usage one; a disk that stays full does not mail you every five minutes.

Cooldown state is in `/var/lib/ccc/monitor_state.json`. Delete it to make everything eligible to
alert again immediately.

## Thresholds

They live in `/var/lib/ccc/monitor_checks.json`, written by setup:

| | Default |
|---|---|
| Disk | 85% of `/` |
| Rogue GPU/CPU | above 10%, sustained over 3 consecutive checks, with no Slurm job on the node |
| Check interval | 5 minutes |
| Cooldown | 24 hours |
| Services watched | `slurmctld`, `munge`, `headscale`, CCC's tailscaled, the Enroll API, the Head API, the Karma tick |

Editing that file takes effect on the next run — no restart needed, since the timer's service reads
it fresh each time. Re-running `cuttle setup --head` rewrites it back to the defaults.

## If mail stops arriving

1. `journalctl -u ccc-monitor` — is anything firing at all?
2. `sudo systemctl list-timers ccc-monitor.timer` — is the timer still active?
3. Check the provider. App passwords get revoked, and a "from" address the provider will not send as
   is a common silent failure.
4. `sudo cat /var/lib/ccc/monitor.yaml` — confirm the fields are what you think. A restored backup
   brings this file back with it; a rebuilt Head does not.
