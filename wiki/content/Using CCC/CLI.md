---
title: CLI
---
[[Glossary#cuttle|`cuttle`]] is CCC's command-line tool. It runs on your own machine, and also inside
your Tidepool and your Dives. Here is every command you'll use day to day. For admin and
setup commands, see [[Admin/index|Admin]] and [[Donating/index|Donating]].

Run `cuttle` on its own for a menu of everything, or add `--help` to any command for its full options.

## Account

| Command | What it does | Example |
| --- | --- | --- |
| `cuttle login` | Log in to the Head. Also sets up your SSH key and Tidepool alias. Asks for a new password the first time. | `cuttle login` |
| `cuttle logout` | Log this machine out. | `cuttle logout` |
| `cuttle whoami` | Show who you're logged in as. | `cuttle whoami` |
| `cuttle passwd` | Change your password. | `cuttle passwd` |
| `cuttle account` | Show your Karma balance, Escrow and Share. | `cuttle account` |
| `cuttle dashboard` | Open the Dashboard in your browser (`--print` to print its address instead). | `cuttle dashboard` |

To set up a new machine, see [[Getting started]] (`cuttle setup --client`).

## Tidepool

| Command | What it does | Example |
| --- | --- | --- |
| `cuttle tidepool` | Go into your Tidepool. | `cuttle tidepool` |
| `cuttle tidepool status` | Its state, how full it is, and its SSH host name. | `cuttle tidepool status` |
| `cuttle tidepool restart` | Restart it. Running programs stop; files and installs stay. | `cuttle tidepool restart` |
| `cuttle tidepool reset` | Rebuild it from the base image. Your home stays; apt installs are lost. | `cuttle tidepool reset` |
| `cuttle tidepool keys` | List the SSH keys that can log in. | `cuttle tidepool keys` |
| `cuttle tidepool keys add` | Let another public key in (`--name` to label it). | `cuttle tidepool keys add ~/.ssh/id_ed25519.pub` |
| `cuttle tidepool keys remove` | Stop a key logging in, by its id. | `cuttle tidepool keys remove 3` |

More in [[Tidepool]].

## Dives

| Command | What it does | Example |
| --- | --- | --- |
| `cuttle dive` | Start a Dive on a folder and go into it. | `cuttle dive fractals --time 4h` |
| `cuttle dive attach` | Go back into a running Dive, or wait for a queued one. | `cuttle dive attach 58` |
| `cuttle dive end` | End a Dive. The main folder gets a final copy back. | `cuttle dive end 58` |
| `cuttle dive status` | List your open Dives. | `cuttle dive status` |

Options for starting a Dive:

| Option | Meaning | Example |
| --- | --- | --- |
| `--time` | How long: `90m`, `4h`, `2d`, at most `14d`. Needed once per folder. | `--time 4h` |
| `--gpus` | Number of GPUs, on one machine (default 1). | `--gpus 2` |
| `--vram` | Total GPU memory needed, in GB. | `--vram 24` |
| `--dep` | A read-only extra folder. Repeat for more. | `--dep datasets/mnist` |
| `--env` | Dependency file to build a Python environment from. | `--env pyproject.toml` |
| `--feature` | An Arm Feature the Dive needs. Repeat for more. | `--feature vlearn` |
| `--vscode` | Open in VS Code instead of a terminal (also on `attach`). | `--vscode` |

`attach` and `end` take the Dive's number. With only one Dive open you can leave it out. `end`
asks before it ends anything (`--yes` skips that).

More in [[Dives]].

## Files

| Command | What it does | Example |
| --- | --- | --- |
| `cuttle upload` | Copy a file or folder from your machine to your Tidepool (or a Dive with `--dive`). | `cuttle upload data.csv proj/` |
| `cuttle download` | Copy a file or folder from your Tidepool (or a Dive) to your machine. | `cuttle download proj/results` |

Both take `--dive <id>` to use a running Dive and `--force` to overwrite files that are already there.
Inside a Tidepool or Dive you entered with `cuttle`, they copy to and from your own machine. More in
[[Files]].

## The cluster

| Command | What it does | Example |
| --- | --- | --- |
| `cuttle queue` | Every running and waiting Dive, and who started it. | `cuttle queue` |
| `cuttle history` | Finished Dives, with cost and outcome (`--days`, default 30). | `cuttle history --days 7` |
| `cuttle karma` | Everyone's Karma standing. | `cuttle karma` |
| `cuttle karma ledger` | Your own Karma history (`--days`, default 30). | `cuttle karma ledger` |
| `cuttle tide` | How busy each Arm has been, hour by hour (`--days`, default 7). | `cuttle tide --days 3` |
| `cuttle feature list` | The Arm Features CCC knows about. | `cuttle feature list` |
| `cuttle feature show` | What CCC sets up for one Feature. | `cuttle feature show vlearn` |

`queue` and `history` show 50 rows by default; `--limit 0` shows all.

## Options for every command

| Option | What it does |
| --- | --- |
| `--json` | Machine-readable output, on commands that print tables or facts. |
| `--help` | Help for that command. |
| `--version` | Print the `cuttle` version. |
| `--no-input` | Never ask questions; a missing answer is an error. Useful in scripts. |
| `--debug` | Show the full error instead of one line. |

These go right after `cuttle`, except `--json` and `--help`, which go after the command:
`cuttle --debug dive status`, `cuttle queue --json`.

## Related

- [[Using CCC/index|Using CCC]]: parent
- [[Getting started]]: installing `cuttle` and logging in
- [[Dives]]: Dives in depth
- [[Files]]: copying files in depth
- [[Tidepool]]: your Tidepool in depth
- [[Admin/index|Admin]]: admin commands
- [[Donating/index|Donating]]: commands for Arm owners
