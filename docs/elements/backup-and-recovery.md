# Backup & recovery

> This whole page is the operator's. Nothing here is available to, or affects, ordinary Users —
> except one thing, which is that a restore loses everyone's stored W&B key. See the end.

## What it is

The Head holds three files that are the entire durable state of the cluster:

- **`head.db`** — Users, sessions, Karma, Spawns, Recipes, attributions.
- **`arms.json`** — the Arm registry.
- **Headscale's `db.sqlite`** — the [Mesh](mesh.md): who is a member and at what address.

`cuttle backup` bundles all three into one timestamped tarball under `/var/backups/ccc`, and
`cuttle backup restore` puts them back.

A nightly timer, installed by `cuttle setup --head`, takes one automatically and keeps the newest
three.

The backup directory is `root:root 0700` — the tarballs hold live secrets, including session tokens.

## Admin — on the Head

### Taking one

```sh
cuttle backup create
```

**No downtime.** The Head API, the Karma tick and Headscale all keep running throughout; the two live
databases are copied with SQLite's online-backup API rather than by grabbing the files.

### Restoring

```sh
cuttle backup restore /var/backups/ccc/ccc-backup-<timestamp>.tar.gz
```

This one is disruptive, and it is **all-or-nothing**:

1. Stops everything that can write one of the three files — the Enroll API, the Head API, the Karma
   tick, Headscale.
2. Replaces all three, at the right owner and permissions.
3. Restarts everything.

It overwrites **every** Spawn, Karma event, session and Arm enrollment created since that backup.
There is no selective restore of one store, and no undo short of restoring something else. It makes
you type the archive's filename to confirm.

### Stored W&B keys do not come back

This is the one consequence that reaches Users, so it is worth knowing before you need it.

Each User's W&B key is held in `head.db` encrypted under a keyfile, and **that keyfile is
deliberately excluded from every backup**. That exclusion is the entire value of encrypting them: it
keeps the tarball strictly *less* sensitive than the Head itself, which is the only thing at-rest
encryption can buy on a box that has to decrypt unattended.

The cost is that after a restore, every stored key is unreadable. The Head treats an unreadable key
as no key, so nothing breaks — Spawns simply run with W&B disabled, and say so. Each User re-pastes
theirs with [`cuttle wandb set`](telemetry.md) or the Dashboard's Account page. `restore` prints this
warning rather than letting it be discovered a Spawn later.

### What a backup is not

It is not a way to undo one User's mistake. There is no per-User, per-Spawn or per-store restore —
a restore is the whole cluster, back to that moment.

Rebuilding a Head from scratch is the routine path here, not a disaster: teardown and setup is the
normal redeploy. A backup is for getting the *state* back onto a Head, not for reinstalling one.
