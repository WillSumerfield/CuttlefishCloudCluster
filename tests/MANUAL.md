# Manual acceptance pass

What the automated suite cannot check, and therefore what a person has to do before calling a
release good. Policy for what earns an *automated* test is in [`TESTING.md`](TESTING.md); this is
the other half.

Everything under `tests/` renders a config, a unit file, a job script or an HTTP response — none of
it needs Slurm, sudo, a GPU or a database. That is deliberate, and it is also the limit. Nothing in
the suite has ever provisioned a box, placed a Spawn on real silicon, or looked at the screen.

Teardown-and-setup is the normal redeploy here, so a full pass is cheap. Do it on a real Head and at
least one real Arm.

## 1. The Head comes up

```sh
cuttle teardown --full
cuttle setup --head --address=<reachable-address>
sinfo -N -o "%N %t %C %G"
```

- [ ] Every step frame closes green; no step is silently skipped.
- [ ] The Head's own node is `idle` with its GPUs listed.
- [ ] The Feature prompts offer what this box declared last time (on a re-run).
- [ ] The LAN-only warning fires if and only if the address is LAN-only.
- [ ] `https://<head>/` serves the dashboard behind one certificate warning.

## 2. An Arm enrolls

On the Head:

```sh
cuttle arm authorize test-arm --user <someone>
```

On the donor box, with the token:

```sh
cuttle setup --arm --token <token>
```

- [ ] `authorize` waits, then admits, without a second command.
- [ ] The benchmark runs and the Arm gets a Weight.
- [ ] `cuttle arm list` shows it **admitted**, not merely enrolled.
- [ ] A declared Feature with a wrong path is re-asked at the prompt, not accepted.
- [ ] A second use of the same token is refused.

## 3. A Tide Pool runs end to end

```sh
cuttle run --time 10 --vram 8
```

- [ ] `cuttle queue` shows it pending, then running.
- [ ] It attaches without asking for a key or a port.
- [ ] Leaving the shell says it is still running and names `cuttle cancel <id>`.
- [ ] `cuttle run <id>` reattaches; `cuttle cancel <id>` ends it.

## 4. Isolation actually isolates

From inside a Tide Pool (`cuttle run --time 10 --vram 8`):

- [ ] `ls /` is the Pod, not the host.
- [ ] Another User's spawn directory is unreachable.
- [ ] `ps aux` shows nothing belonging to the host or another Spawn.
- [ ] The Guise is not an account: `su - <guise>` fails.

## 5. Retract does what it promises

With someone else's Spawn running on the Arm:

```sh
cuttle arm retract test-arm
```

- [ ] The running Spawn is signalled, gets its grace, and dies.
- [ ] It settles at **zero** — `cuttle karma ledger` on both sides shows no movement.
- [ ] No new Spawn is placed there.
- [ ] `cuttle arm return` reopens it, and does not resurrect the killed Spawn.

## 6. The economy moves the right way

- [ ] A Spawn on someone else's Arm debits the submitter and credits that owner, the same amount.
- [ ] A Spawn on your own Arm is free.
- [ ] A Spawn on a Commons Arm destroys the Karma — nobody is credited.
- [ ] Queueing several Spawns suppresses your Share **immediately**, before any of them run.
- [ ] Over-declaring `--time` refunds at settlement.
- [ ] `cuttle karma override` on an **admin** is refused.

## 7. The interactive layer

Typed, and looked at. This is the part the suite explicitly does not cover.

- [ ] Bare `cuttle` opens the menu on a terminal; `cuttle | cat` prints help instead of hanging.
- [ ] `cuttle run` with no flags asks for `--time` and `--vram` rather than refusing.
- [ ] Ctrl-C at every prompt leaves the terminal usable — no `reset` needed.
- [ ] `cuttle setup` off-terminal refuses rather than guessing a role.
- [ ] A destructive command (`teardown`, `arm remove`, `backup restore`) demands the typed name and
      cannot be answered by a confirmation reflex.

## 8. Backup and restore

```sh
cuttle backup create
# make changes: a User, a Spawn, an Override
cuttle backup restore <archive>
```

- [ ] `create` causes no downtime — the dashboard stays up throughout.
- [ ] `restore` stops and restarts everything, and the changes since are gone.
- [ ] The nightly timer has produced archives, pruned to the newest three.

## 9. The Dashboard

- [ ] Every rail destination loads and shows the same data as its CLI counterpart.
- [ ] Retract/Return work from the Arms page.
- [ ] The Users page is visible to an Admin and read-only.
- [ ] Logging out of one client does not log out the other.

## 10. Docs still describe reality

- [ ] Every command in [`../docs/CLI.md`](../docs/CLI.md) exists, and every command that exists is
      listed there.
- [ ] The worked examples in the docs run as written.

There is no automated check for any of this, deliberately — a doc test that policed the command list
would fail on every rename and teach nobody anything.
