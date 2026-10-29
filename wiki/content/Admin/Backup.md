---
title: Backup
tags: [leaf]
---
The Head backs up the cluster's records every night: accounts, Karma, Dives, the Arm list and the
Mesh. `cuttle backup create` takes one by hand, and `cuttle backup restore` puts one back.
Users' files in their Tidepools are **not** part of it.

## What's in a backup

Three files, which together are the cluster's whole record:

| File | Holds |
| --- | --- |
| `head.db` | Accounts, password hashes, sessions, Dives, the Karma ledger |
| `arms.json` | Every Arm: its owner, GPUs and benchmark results |
| Headscale's database | Which machines are on the [[Mesh]], and their addresses |

They go into one file, `/var/backups/ccc/ccc-backup-<date>T<hour>Z.tar.gz`.

What's **not** in it:

- **Tidepools.** Each User's files are one image file in the Tidepool folder (`/var/lib/ccc-tidepools`
  by default). If you want those backed up, that's a separate job.
- The cluster's certificates, the munge key and your setup answers. A fresh `cuttle setup --head`
  makes those again.

## Automatic backups

`ccc-backup.timer` takes one every night and keeps the **newest three**. Check it's running:

```bash
systemctl list-timers ccc-backup.timer
```

Backups stay on the Head. Copying them somewhere else is up to you, for example with `rsync` or `scp`
from another machine. Keep the copies private: they contain live session tokens.

## Taking one now

```bash
cuttle backup create
```

Nothing stops while it runs. It prints where the file went.

## Restoring

```bash
cuttle backup restore /var/backups/ccc/ccc-backup-20261029T03Z.tar.gz
```

Leave the path off to pick from a list. You type the file name to confirm.

A restore replaces **all three** files at once, so everything since that backup is gone: new accounts,
password changes, Dives, Karma, Arms that joined. There's no restoring just one User or one file,
and no undo except restoring a different backup.

It stops `ccc-enroll-api`, `ccc-head-api`, `ccc-karma` and `headscale`, swaps the files in, and
starts them again.

## Teardown and backups

`cuttle teardown` never deletes `/var/backups/ccc`, even with `--all`.

## Related

- [[Admin/index|Admin]]: parent
- [[Head setup]]: what teardown removes and keeps
- [[Security]]: why backups need to stay private
- [[Tidepool]]: where Users' files actually live
