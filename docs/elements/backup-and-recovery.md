# Backup & recovery

> This whole page is the operator's. Nothing here is available to, or affects, ordinary Users.

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

### Stored W&B keys do come back

They did not always. Each User's key used to be encrypted under a keyfile that was deliberately left
out of every archive, so a restore left every stored key unreadable and each User had to re-paste
theirs.

That is no longer how keys are held: a [Credential](telemetry.md) is sealed under its owner's own
password, and both halves of the wrapping are rows in `head.db`. Restore the database and the next
login re-derives the key that opens them. Nobody has to re-paste anything.

`secret.key` is still excluded from every archive — it now protects only Tide Pool private keys, and
those belong to Spawns that left the queue long before anyone restores a backup. The exclusion keeps
the tarball strictly less sensitive than the Head itself and costs a restore nothing.

### What a backup is not

It is not a way to undo one User's mistake. There is no per-User, per-Spawn or per-store restore —
a restore is the whole cluster, back to that moment.

Rebuilding a Head from scratch is the routine path here, not a disaster: teardown and setup is the
normal redeploy. A backup is for getting the *state* back onto a Head, not for reinstalling one.
