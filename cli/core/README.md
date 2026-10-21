# cli/core — `cuttle` (Python)

The Python (`typer`) core of the `cuttle` front door: provisions nodes, enrolls remote Arms, submits
Spawns. `cuttle` is Python-only — no separate host-native launcher.

## Dev

```bash
cd cli/core
uv sync
uv run cuttle --help
```

## Commands

### Setup / teardown
- `cuttle setup --head [--address <host>]` — provision **this** box as the Head: self-hosted Headscale
  coordinator + isolated `tailscaled-ccc` mesh, `slurmctld` + munge, the mesh-only enroll API, the
  LAN+mesh Head API + dashboard, rsync stage-out, the auth store, and a co-located Arm. Seeds the
  first admin account. `--address` = the coordinator's advertised host (default: primary IP; use a host
  remote Arms can actually reach). `--local` is an alias for the single-box case. Idempotent.
- `cuttle setup --arm --token <blob> [--re-enroll]` — enroll **this** box as a remote Arm, redeeming
  the enrollment token its owner was given: join the mesh (unattended — nobody approves anything),
  fetch the munge key, register a dynamic `slurmd -Z`. The token names the Arm, so there is nothing to
  choose.
- Both roles create the **Mantle** (the `ccc` account every Spawn runs as, at a uid pinned
  cluster-wide) and build `/opt/ccc/images/base.sif`.
- `cuttle teardown --local | --arm [--keep-packages] [--all]` — reverse setup. Default keeps `~/.ccc`
  and base.sif; `--keep-packages` = fast config-only reset; `--all` also deletes both. Purges only the
  packages setup recorded (`~/.ccc/config/apt-installed.txt`). Never touches personal Tailscale.

### Enrollment (Head-side)
The **Admin decides first, the donor redeems.** One admin command, one donor command, nothing relayed
by hand.

- `cuttle arm authorize <name> --user <owner>` — authorize one machine to become an Arm. Names it,
  names the **User** who owns it, and prints the **enrollment token** they redeem. Then blocks until
  they do, admits them to Slurm, and **benchmarks** them. **The token is a secret** — it admits one
  machine, once, within the hour. Send it privately.
- `cuttle arm admit <name>` — the second half alone, if `authorize` gave up waiting because the donor
  wasn't at their desk yet.
- `cuttle arm benchmark <name>` — measure an Arm's **Power** (fp32 TFLOP/s) by running a matmul inside
  the Pod, on that node, submitted down the ordinary Slurm path — so the number reflects the machine as
  Spawns really meet it (a throttling laptop card, a stale driver), not
  what a spec sheet claims. Sets the node's Slurm `Weight` ∝ 1/Power, so the fastest *free* eligible
  card wins. Runs automatically as part of `authorize`; re-run it by hand after a GPU or driver change.
- `cuttle arm chown <name> --user <owner>` — re-point an Arm at a different owner. Touches neither the
  mesh nor Slurm, so the donor's box keeps running.
- `cuttle arm list` — Arms, their owners, their GPU/VRAM/Power, and whether each is authorized /
  enrolled / admitted.
- `cuttle arm remove <name>` — delete an Arm from Slurm, the registry, and the mesh. **Required after a
  donor runs `teardown --arm`**: an Arm can't delete its own mesh node, and `authorize` refuses a name
  whose node still exists (it will never evict a running machine as a side-effect).

Two gates guard the cluster, and both fall out of that single `authorize`: the token admits the
machine to the mesh, and the pre-declared name is what the mesh-only Enroll API checks before handing
over the munge key. A machine on the mesh that no Admin named gets nothing.

### Spawns
All of these go through the Head API and need `cuttle login` — the CLI is a network client, so you can
submit from any box, whether or not it ever ran `setup`.

- `cuttle run <script> --time MIN --vram GB [--gpu N] [-f] [-- ARGS]` — submit a **Spawn**: the Head
  queues the job and the **Arm** it lands on runs `<script>` inside a Pod. `-f` streams stdout. Job id = spawn id.
  - `--time` (wall-clock minutes) and `--vram` (GB) are **required, with no defaults**. Nothing can
    price a Spawn whose length nobody declared, and a `--vram` that defaulted to the fleet's biggest
    card would make every untouched field eligible for that one GPU alone — queued behind everything,
    for work that might need 2 GB. You say what you *need*; the Head finds every card that qualifies
    and Slurm gives you the fastest one that's free. Over-estimating either costs you queue position,
    not Karma.
  - `--node NAME` pins a Spawn to one Arm, and is **admin-only**.
- `cuttle status <id>` — `queued`/`running`, `DONE (EXIT N)`, or `FAILED` (the Spawn was killed or its
  Arm dropped before staging its logs home — no exit code was ever recorded).
- `cuttle logs <id> [-f]` — print/tail the Spawn's stdout. A co-located Spawn streams live; a remote
  Arm's output arrives at stage-out, so `-f` there is post-hoc.

### Accounts
- `cuttle login [--head <url>]` / `logout` / `whoami` / `passwd` — session lives in `~/.ccc/session`
  (0600). First login on a fresh account forces a password rotation.
- `cuttle user add|list|remove <name>` — Head-side only (runs as the Mantle against the auth store).
  `add` prints a one-time temp password; the operator never learns a reusable one.

## Dashboard
`cuttle setup --head` serves the web Dashboard (Vite/React) at `https://<head>/`, reachable over
the LAN and the CCC mesh. It is the **primary** surface: log in and submit a
Spawn; then watch the queue and tail a Spawn's log. Enrollment stays on a
separate **mesh-only** API (`:8081`) so the munge-key handout never touches the LAN.

## TLS — expect a browser warning, once
The Head carries passwords, so it speaks HTTPS. No certificate authority will sign a cert for a box
with no public domain name, so **the Head signs its own** — which encrypts the connection but proves
nothing about identity on its own. Two consequences, both intentional:

- **Browser** — the first visit warns *"your connection is not private."* That is expected. Click
  Advanced → Proceed; the browser remembers, and you see it once per browser.
- **CLI** — `cuttle login` shows the certificate's fingerprint once and asks you to confirm it, then
  **pins** it to `~/.ccc/pins/`. This is exactly SSH's `known_hosts`: from then on a *different*
  certificate is refused, so nothing on the LAN can impersonate the Head and harvest your password.

If you ever rebuild the Head with a fresh cert (`teardown --all`), clients will refuse to connect until
you delete the stale pin and log in again. A plain `teardown` preserves the cert precisely so this
doesn't happen on every redeploy.

## Config / artifacts
- `~/.ccc` holds **only** the operator's settings + its session token. Spawn artifacts are not here:
  they live on the Head under `/var/lib/ccc/head/spawns/<id>/{out,err,exit}` (owned by the
  Mantle) and are read back over the API — a submitter never touches the Head's disk.
- The Pod image is at `/opt/ccc/images/base.sif`, the same path on every node, so it is readable by the
  home-less Mantle. It is Ubuntu + sshd + CA certs (~150 MB) and carries no Python — every Spawn
  brings its own. A changed image reaches a node only through `teardown --all` + setup; a plain
  setup re-run keeps whatever is already there.
- Pinned versions live in `src/cuttle/config.py`.
