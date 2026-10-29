---
title: Alerts and email
---
Every five minutes the Head checks its own health: is the disk filling up, are CCC's services
running, and is something using the GPU or CPU outside a Dive. When a check fails, it emails you.
Without email set up the checks still run; they log instead.

## What it checks

| Check | Fires when | Email subject |
| --- | --- | --- |
| Disk | `/` is 85% full or more | CCC Head: disk usage high |
| Services | Any of `slurmctld`, `munge`, `headscale`, `tailscaled-ccc`, `ccc-enroll-api`, `ccc-head-api`, `ccc-karma`, `ccc-rsyncd` or `ccc-mesh-firewall.timer` isn't running | CCC Head: service(s) down |
| Unexplained use | GPU or CPU above 10% with no Slurm job running on the Head, three checks in a row (15 minutes) | CCC Head: unexplained GPU/CPU usage |

Each check emails at most **once a day**, and each one on its own, so a full disk won't hide a
service going down. The monitor only watches the Head, not the Arms.

It runs as `ccc-monitor.service`, started by `ccc-monitor.timer`.

## Setting up email

[[Head setup]] asks for SMTP details near the end. To add or change them later, re-run it:

```bash
cuttle setup --head
```

Every other question comes back with your old answer, so press Enter through those. The email
questions are:

| Question | Example |
| --- | --- |
| SMTP host | `smtp.gmail.com`. Blank skips email entirely |
| SMTP port | `587`. CCC always uses STARTTLS |
| SMTP username | usually your full email address |
| SMTP password | blank keeps the one already saved |
| Alert 'from' address | defaults to the username |
| Alert 'to' address | your own inbox |

For Gmail and other accounts with two-factor sign-in, your normal password won't work. Make an
**app password** in your account settings and use that.

`cuttle update` doesn't ask these questions, and leaves your saved settings alone.

The settings are saved in `/var/lib/ccc/head/monitor.yaml`, readable only by the `ccc` account. The
password is stored there in plain text, so that file's permissions are what protect it.

## Checking it works

There's no "send a test email" button. Run the checks now and read what happened:

```bash
sudo systemctl start ccc-monitor.service
journalctl -u ccc-monitor -n 50
```

| You see | Means |
| --- | --- |
| `<check> fired, no SMTP conf at …` | Email isn't set up |
| `<check> fired but email failed: …` | Set up, but sending failed. The rest is your mail provider's error |
| nothing | Nothing fired. All healthy |

The easiest one to trigger on purpose is the services check: stop a watched service, run the check,
then start the service again. If it has already emailed about services today, it won't email again
until tomorrow. To reset that, delete `/var/lib/ccc/head/monitor_state.json`.

## Thresholds

They're in `/var/lib/ccc/head/monitor_checks.json`. Edits take effect on the next check, but
re-running `cuttle setup --head` or `cuttle update` writes the defaults back.

## Related

- [[Admin/index|Admin]]: parent
- [[Head setup]]: where email is first set up
- [[Admin troubleshooting]]: what to do when a service-down email arrives
- [[Security]]: where other secrets live
