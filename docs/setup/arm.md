# Set up an Arm

You have been asked to donate a machine to a CCC cluster. This page is everything you need. You do
not have to read anything else.

## What you are agreeing to

Someone else's code will run on your GPU, in a sandbox, when you are not using it.

Specifically:

- **CCC installs a scheduler, a container runtime and a private network client** on this box. It
  needs `sudo` to do that.
- **The private network contains the cluster and nothing else.** Nobody gets access to your LAN, and
  you get none to theirs. If you already use Tailscale personally, CCC runs its own separate instance
  alongside it and neither touches the other — CCC does not take over your DNS, your firewall rules
  or your route table.
- **Jobs run walled off from your machine.** A Spawn cannot see your filesystem, your processes, or
  anything else running on the box. It runs as an identity that is not you and cannot log in.
- **You can take the machine back at any second**, with one command, without asking anyone. See
  [Taking your machine back](#taking-your-machine-back).
- **You earn Karma** when other people's work runs here — standing in the cluster, which is what
  decides who goes first when two people want a GPU at once. See [Karma](../elements/karma.md).

What CCC does *not* do: it does not run anything when the machine is off, it does not give the
cluster's operator a login on your box, and it does not stop you from using your own GPU — retract
and it is yours.

## Before you start

- **Ubuntu 24.04**, natively or under **WSL2** on Windows.
- An **NVIDIA** GPU with working drivers. Check with `nvidia-smi`.
- `sudo` on this box.
- **An enrollment token from the cluster's admin.** Ask them for one:
  > `cuttle arm authorize <a-name-for-my-machine> --user <my-username>`

  The token is a secret, it works for one machine, and it **expires within the hour**. Get it when
  you are ready to use it.

## 1. Install `cuttle`

Either the admin sends you an installer bundle:

```sh
tar xzf cuttle-installer.tar.gz
cuttle-installer/install.sh
```

Or, if you have the repo checked out:

```sh
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install --editable ./cli/core
```

Either way you now have `cuttle` on your PATH. Check with `cuttle --version`.

## 2. Enroll

```sh
cuttle setup --arm --token <the-token-from-the-admin>
```

Or run `cuttle setup` with no flags and it will walk you through it, asking for the token.

This takes a while. It joins the mesh, registers your hardware with the Head, installs Slurm, munge
and Apptainer, and runs a benchmark on your GPU to measure how fast it actually is.

### It will ask what software this box has

Partway through, you are asked one question per [Feature](../elements/arm-features.md) — heavy or
licence-bound software that CCC knows how to run — and, if you say yes, where it is installed.

**Say yes only for what is really there.** A Spawn that asks for a Feature can be sent nowhere else,
so a wrong answer here routes someone's job to a machine that cannot run it. Paths must be absolute.

CCC checks each path against what the software actually needs and re-asks if it does not look right,
rather than letting a stranger's job die inside a shared library hours later.

If a Feature has a product key, you are asked where that is too. It is copied into CCC's own state on
**this box** and never leaves it — the Head never sees it.

## 3. Confirm it worked

Ask the admin to run `cuttle arm list`. Your Arm should be **admitted**, not merely *enrolled*.

If it is stuck at *enrolled*, the admin runs `cuttle arm admit <your-arm-name>` — that usually just
means `arm authorize` gave up waiting while you were still installing.

That is the whole setup. You do not need to keep `cuttle` open, log in, or do anything else. The
machine donates itself whenever it is switched on.

## Taking your machine back

This is the part worth remembering.

```sh
cuttle arm retract <your-arm-name>
```

Immediately: no new work is placed here, and anything running right now is signalled — `TERM`, then
a 3-minute grace period, then killed. Whoever's Spawn that was pays nothing for it, and you earn
nothing for stopping it.

When you are done:

```sh
cuttle arm return <your-arm-name>
```

A retracted Arm **never leaves the cluster**. It stays enrolled and stays on the mesh, so returning
it is entirely yours to do — no admin, no new token, no re-setup.

You cannot kill one particular job on your machine. Retract is what you have, and it takes the whole
box back.

## Leaving

To stop donating permanently:

```sh
cuttle teardown
```

It detects that this box is an Arm and reverses the setup — services, mesh, packages, local state. It
makes you type the machine's hostname to confirm.

If you installed from a bundle, `cuttle-installer/uninstall.sh` does the teardown and then removes
the tool itself.

Then tell the admin, so they can run `cuttle arm remove <your-arm-name>` and take you out of the
registry and the mesh properly.

## If something goes wrong

**`setup --arm` hangs and eventually times out.** The Head's coordinator address is not reachable
from your network — almost always a Head advertising a LAN IP to a remote donor. The admin has to fix
it on their end; point them at [Set up the Head](head.md#making-the-head-reachable).

**"the token is invalid or expired".** Tokens last about an hour and work once. Ask for a fresh one.

**Your Arm stops being able to reach the scheduler.** The operator rotated the cluster's shared key.
Run `cuttle arm sync-key` on this box.

**Your box's state is corrupt, or the Head was rebuilt.** `cuttle setup --arm --re-enroll --token
<fresh-token>` discards this machine's mesh identity and joins from scratch.

More at [Troubleshooting](../Troubleshooting.md).
