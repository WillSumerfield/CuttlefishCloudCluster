# The `cuttle` CLI

A map of every command, and where each one is explained properly. **Flags are not listed here** —
`cuttle <command> --help` has them, and unlike this page it cannot go stale.

## You can just run `cuttle`

On a terminal, `cuttle` with nothing after it opens an arrow-key menu. It shows only what this box
can do *right now* — the submit commands appear once you are logged in, the admin ones only on the
Head — so you are never picking from a list of things that would refuse you. Pick something and it
asks for what it needs.

In a pipe or a script it prints help instead, because a menu there would wait forever for a keystroke
nobody can send.

The same is true one level down: leave a command's required arguments out and it prompts for them
rather than failing. `cuttle run` and `cuttle setup` especially — both walk you through and show you
what they are about to do before they do it. The rest of this page is the typed form, which is what
you want once you know the command and what a script needs.

## Who can run what

`cuttle` is the whole of CCC's command line: there is no second, more privileged tool. Rows marked
**admin** are refused for ordinary Users, and most of them additionally have to be run *while sitting
on the Head*, because CCC's Head API exposes no administrative writes at all — see
[Security](Security.md#the-head-api-has-no-admin-writes).

## Getting a machine ready

| Command | | Explained in |
|---|---|---|
| `cuttle setup --head` | Provision this box as the cluster's control plane. | [setup/head.md](setup/head.md) |
| `cuttle setup --arm` | Enroll this box as a donor node. Needs a token. | [setup/arm.md](setup/arm.md) |
| `cuttle setup --client` | Submit-only box: directories and login, no root. | [setup/client.md](setup/client.md) |
| `cuttle teardown` | Reverse setup. Auto-detects the role. | [setup/arm.md](setup/arm.md#leaving) |
| `cuttle build-installer` | Package a portable client installer, pre-pointed at this Head. | [setup/head.md](setup/head.md#handing-out-the-installer) |
| `cuttle --version` | What you have installed. Answers on a box that is not set up. | — |

## Signing in

| Command | | Explained in |
|---|---|---|
| `cuttle login` | Log in and store a session on this box. | [Users & access](elements/users-and-access.md) |
| `cuttle logout` | Revoke this box's session and delete the local token. | [Users & access](elements/users-and-access.md) |
| `cuttle whoami` | Who this box is logged in as. | [Users & access](elements/users-and-access.md) |
| `cuttle passwd` | Change your password. | [Users & access](elements/users-and-access.md) |
| `cuttle user add` | **admin** Create a User. Prints a one-time temp password. | [Users & access](elements/users-and-access.md#admin--on-the-head) |
| `cuttle user list` | **admin** Every User. | [Users & access](elements/users-and-access.md#admin--on-the-head) |
| `cuttle user remove` | **admin** Delete a User. | [Users & access](elements/users-and-access.md#admin--on-the-head) |
| `cuttle user reset` | **admin** Reset a password and kill that User's sessions. | [Users & access](elements/users-and-access.md#admin--on-the-head) |

## Running work

| Command | | Explained in |
|---|---|---|
| `cuttle run` | Submit a Spawn: upload this directory and run a script on the cluster. | [Spawns](elements/spawns.md) |
| `cuttle run --tidepool` | Ask for a live interactive shell instead of running a script. | [Tide Pool](elements/tide-pool.md) |
| `cuttle run --tidepool --vscode` | The same session, opened in VS Code rather than this terminal. | [Tide Pool](elements/tide-pool.md#editing-in-vs-code) |
| `cuttle status` | Is it queued, running, or finished — and with what exit code. | [Spawns](elements/spawns.md#following-a-spawn) |
| `cuttle logs` | Print or follow a Spawn's stdout and stderr. | [Spawns](elements/spawns.md#following-a-spawn) |
| `cuttle queue` | Everything pending or running on the cluster, and whose it is. | [Spawns](elements/spawns.md#following-a-spawn) |
| `cuttle history` | Past Spawns, with their cost and outcome. | [Spawns](elements/spawns.md#following-a-spawn) |
| `cuttle cancel` | End a Spawn early. Yours from anywhere; **admin** `--any`, on the Head. | [Spawns](elements/spawns.md#ending-one-early) |
| `cuttle outputs` | Download a finished Spawn's declared results again. | [Storage](elements/storage.md) |
| `cuttle recover` | Download what a Spawn wrote *outside* its declared outputs. One-day window. | [Storage](elements/storage.md#the-recovery-window) |
| `cuttle dashboard` | Open the web Dashboard in a browser. `--print` gives the URL instead. | [Dashboard](Dashboard.md#getting-to-it) |

## Recipes

| Command | | Explained in |
|---|---|---|
| `cuttle recipe save` | Freeze this directory — code and how to run it — into a Recipe. | [Recipes](elements/recipes.md) |
| `cuttle recipe list` | Your Recipes, and what they hold against your allowance. | [Recipes](elements/recipes.md) |
| `cuttle recipe update` | Re-freeze a Recipe around this directory's current code. | [Recipes](elements/recipes.md#updating-one) |
| `cuttle recipe run` | Run a Recipe's snapshot as a Spawn. *Not wired up yet.* | [Recipes](elements/recipes.md) |
| `cuttle recipe delete` | Delete a Recipe and free its bytes. | [Recipes](elements/recipes.md) |

## Workspaces

| Command | | Explained in |
|---|---|---|
| `cuttle workspace new` | Make a directory into a workspace the cluster keeps between sessions. | [Workspaces](elements/workspaces.md) |
| `cuttle workspace list` | Yours, how full each is, and which one is open right now. | [Workspaces](elements/workspaces.md) |
| `cuttle workspace open` | Get a shell in one, resuming where the last visit left off. | [Workspaces](elements/workspaces.md) |
| `cuttle workspace update` | Change what a visit asks for, or resize its Storage. | [Workspaces](elements/workspaces.md#running-out-of-storage) |
| `cuttle workspace download` | Pull the tree, or one folder inside it, down as a tar. | [Workspaces](elements/workspaces.md) |
| `cuttle workspace delete` | Delete a workspace and free its Storage. | [Workspaces](elements/workspaces.md) |

## Karma

| Command | | Explained in |
|---|---|---|
| `cuttle karma` | Everyone's standing — Balance, Escrow, Share, and the queue priority it implies. | [Karma](elements/karma.md) |
| `cuttle karma ledger` | Your own history: Settlements, Overrides, and the Balance they left you at. | [Karma](elements/karma.md) |
| `cuttle account` | Who you are and where you stand, in one place. | [Karma](elements/karma.md) |
| `cuttle tide` | How badly each Arm has been wanted, hour by hour. | [Karma](elements/karma.md#the-tide) |
| `cuttle karma override` | **admin** Add or remove Karma from a User's Balance. | [Karma](elements/karma.md#admin--on-the-head) |
| `cuttle karma bump` | **admin, on the Head** Shove one queued Spawn up the queue. No Karma moves. | [Karma](elements/karma.md#admin--on-the-head) |

## Arms

| Command | | Explained in |
|---|---|---|
| `cuttle arm retract` | Take your machine back, now. Stops new work, ends what is running. Yours from anywhere; **admin** `--any`, on the Head. | [Arms](elements/arms.md#retract-and-return) |
| `cuttle arm return` | Give a retracted Arm back to the cluster. Same reach as `retract`. | [Arms](elements/arms.md#retract-and-return) |
| `cuttle arm list` | Every Arm: owner, and whether it is authorized, enrolled, admitted. | [Arms](elements/arms.md) |
| `cuttle arm sync-key` | *On the Arm.* Re-fetch the cluster's munge key after a rotation. | [Mesh](elements/mesh.md#the-munge-key) |
| `cuttle arm authorize` | **admin** Mint one machine's enrollment token, then wait and admit it. | [Arms](elements/arms.md#admin--on-the-head) |
| `cuttle arm admit` | **admin** Finish admitting an Arm that enrolled after `authorize` gave up waiting. | [Arms](elements/arms.md#admin--on-the-head) |
| `cuttle arm benchmark` | **admin** Re-measure an Arm's Power and re-price it. | [Arms](elements/arms.md#admin--on-the-head) |
| `cuttle arm chown` | **admin** Re-point an Arm at a different owner. | [Arms](elements/arms.md#admin--on-the-head) |
| `cuttle arm remove` | **admin** Delete an Arm from Slurm, the registry and the mesh. | [Arms](elements/arms.md#admin--on-the-head) |

## Features

| Command | | Explained in |
|---|---|---|
| `cuttle feature list` | The Features CCC knows how to run. Built in, not configurable. | [Arm Features](elements/arm-features.md) |
| `cuttle feature show` | What CCC arranges for a Feature, and which Python it needs. | [Arm Features](elements/arm-features.md#writing-against-a-feature) |

## Experiment tracking

| Command | | Explained in |
|---|---|---|
| `cuttle wandb set` | Store your W&B key on the Head. Injected into every Spawn you submit. | [Telemetry](elements/telemetry.md) |
| `cuttle wandb show` | Whether a key is stored, masked to its last four characters. | [Telemetry](elements/telemetry.md) |
| `cuttle wandb clear` | Forget it. Your Spawns go back to running with W&B disabled. | [Telemetry](elements/telemetry.md) |

## Operating the cluster

| Command | | Explained in |
|---|---|---|
| `cuttle backup create` | **admin, on the Head** Snapshot the Head's state. No downtime. | [Backup & recovery](elements/backup-and-recovery.md) |
| `cuttle backup restore` | **admin, on the Head** Put it back. All-or-nothing, no undo. | [Backup & recovery](elements/backup-and-recovery.md#restoring) |
| `cuttle munge rotate` | **admin, on the Head** Mint a fresh cluster-wide munge key. | [Mesh](elements/mesh.md#the-munge-key) |
