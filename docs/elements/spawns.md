# Spawns

## What it is

A **Spawn** is one job on the cluster: a GPU held on some machine, for a declared time.

**Nothing can request a Spawn in 2.0 until Dives.** What is left reads back and ends the Spawns the
Head already knows: CCC keeps their logs and exit code on the Head.

The thing worth internalising: a Spawn runs **once**. If the machine it landed on is taken back by
its owner mid-run, that Spawn is over. Nothing silently retries somewhere else. So "Spawn 412" always
names one attempt — what it cost and what it produced both refer to the same single run.

## Commands

| | |
|---|---|
| `cuttle status <id>` | Queued, running, or done — and with what exit code. |
| `cuttle logs <id>` | Print its stdout and stderr. `--follow` to stream. |
| `cuttle queue` | Everything pending or running on the cluster, and whose it is. |
| `cuttle history` | Past Spawns, with their cost and outcome. |
| `cuttle cancel <id>` | End one of yours early. |

## Behavior

### What happens when it goes wrong

- **It ends non-zero** — the Spawn is *settled, not requeued*. You pay for the seconds it actually
  consumed and it does not quietly run again.
- **The machine it was on is retracted underneath it** — the Spawn dies, and **settles at zero**.
  You pay nothing for work someone else stopped, and the Arm's owner earns nothing for stopping it.
  See [Arms](arms.md#retract-and-return).

### Following a Spawn

`cuttle status` and `cuttle logs` take a Spawn id; leave it off and you are asked which one, from
your recent Spawns. `cuttle queue` is the live cluster — everything pending and running, everyone's.
`cuttle history` is what the live queue has already forgotten, with the Karma each one actually cost.

### Ending one early

`cuttle cancel <id>` from anywhere, for your own Spawns. It **settles** for the seconds it really
held: you chose to stop it, so you pay for what you used. This is the opposite of a Retract, which
settles at zero — the difference is who decided.

An Arm's owner has no way to kill one particular Spawn running on their machine. What they have is
[Retract](arms.md#retract-and-return), which takes the whole machine back.

## Admin — on the Head

`cuttle cancel <id>` ends a Spawn belonging to someone else, when you run it **on the Head**. No flag:
the Head API is asked first, and its "not yours" is the ownership test rather than the end of the
command — on the Head that answer selects the operator's road (`sudo scancel`) instead. Pick from the
menu and the whole queue is offered, not just your own Spawns.

`--any` remains for the one case that road cannot be reached by asking: an id the ledger never
recorded. `DELETE /spawns/{id}` answers 404 for those and is never escalated past it, so reaping a
runaway the Head has no row for is what the flag is for. It is refused off the Head.
