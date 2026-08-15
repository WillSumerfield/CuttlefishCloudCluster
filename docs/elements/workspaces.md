# Workspaces

## What it is

A **Workspace** is a working directory the cluster keeps.

A [Tide Pool](tide-pool.md) gives you a shell and throws away everything in it when the session ends.
A Workspace gives you the same shell over a folder that survives: what you installed, what you
downloaded, the half-finished thing you were in the middle of. Open it again and you are where you
left off.

Underneath, a visit is an ordinary Tide Pool — same queue, same eligibility, same Karma. The
difference is what happens at the end of it.

**Holding one costs no Karma.** Storage never does. Visiting one does, exactly like any other Spawn.

**What it costs instead is storage.** Each Workspace reserves a **Storage** out of a 32 GB per-User
allowance, chosen by you when you create it. This is the one allowance CCC hands over to be divided
up: what a session runs out of is that one Workspace's Storage, never a pool shared with the rest.

## Typical use

**Make this folder into one, with 16 GB of room:**

```sh
cuttle workspace new scratch --storage 16 --time 120 --vram 24
```

That upload is the **only** one it ever takes. From then on it changes from inside its own sessions.

**Get into it:**

```sh
cuttle workspace open scratch
```

**See what you have, and how full each one is:**

```sh
cuttle workspace list
```

**Get files out without opening it:**

```sh
cuttle workspace download scratch              # the whole tree
cuttle workspace download scratch checkpoints  # one folder inside it
```

**Change what a visit asks for, or resize it:**

```sh
cuttle workspace update scratch --time 30 --storage 24
```

**Delete it** — the only thing that ever removes one:

```sh
cuttle workspace delete scratch
```

## Behavior

### It saves as you work, and again when you leave

The tree is mirrored back to the Head about once a minute, and once more when the session ends —
whether it ended because you typed `exit`, because `--time` ran out, or because you cancelled it.

The mirror is exact, which cuts both ways: **deleting a file inside a session deletes it from the
stored Workspace too.** There is no undo and no second copy. That is deliberate — the alternative is
a Workspace you cannot empty, where `rm -rf *` silently does nothing and everything is back next
time.

### One visit at a time

Opening a Workspace that is already open **attaches** to the session that is running rather than
starting a second one. Two sessions writing one tree would each overwrite the other's work.

`cuttle workspace list` names the Spawn a live Workspace is being visited by, so you can cancel it.

### Running out of Storage

Nothing is evicted, and nothing fails loudly. The mirror simply stops: the session keeps working, but
it stops being saved. CCC warns you inside the session when that happens, and keeps warning, because
otherwise you would find out by losing the work.

Free space inside the session and the next mirror succeeds on its own.

You can grow a Workspace at any time if your allowance has room. You can shrink one down to what its
tree currently holds, and no further — a resize never truncates. A resize during a live visit takes
effect on the **next** visit.

### Declaring outputs is optional

Like a Tide Pool's, and for the same reason. An output here is an ordinary folder in the Workspace
tree, so anything you write is kept either way and declaring one costs you no extra Storage. What
declaring an output buys you is that the end of each session ALSO sends it home, to be downloaded like
any Spawn's results:

```sh
cuttle outputs <spawn id>
```

### There is no Recovery Window

There is nothing for one to rescue. The [Recovery Window](storage.md#the-recovery-window) exists to
catch what a Spawn wrote outside its declared outputs before that working directory is destroyed — and
a Workspace's working directory is not destroyed. It is the thing that was already saved.

### It never expires

No TTL, no eviction. A Workspace only goes away when you delete it, which is why the CLI makes you
type its name back rather than answering y/N. It is refused outright while a visit is live.

## Workspace or Recipe?

They are siblings, and the split is frozen versus mutable:

| | [Recipe](recipes.md) | Workspace |
|---|---|---|
| What it holds | A snapshot, frozen | A working directory, live |
| How you use it | `recipe run` — batch, unattended | `workspace open` — a shell, interactive |
| Changing it | Re-freeze from your local folder | Work inside it; it saves itself |
| Runs | Any number at once | One visit at a time |
| Allowance | 32 GB, one pool | 32 GB, divided into a Storage each |

Reach for a Recipe when the work is settled and you want to run it again from anywhere with nothing
to hand. Reach for a Workspace when the work is not settled yet.
