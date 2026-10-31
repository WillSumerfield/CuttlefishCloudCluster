---
title: Troubleshooting
---
Common problems when using CCC, and how to fix them. Each heading is what you see or the message
`cuttle` prints. If you donate a machine, see [[Admin troubleshooting]] and [[Arm setup]] for
problems with Arms.

## Logging in and connecting

### "not logged in" or "session expired"

Run `cuttle login`. Sessions expire after a while, and an Admin resetting your password ends them
too.

### "cannot reach the Head at …"

`cuttle` couldn't reach the Head at all. Check that:

- you're on the lab network or campus Wi-Fi. The Head can't be reached from off campus.
- the Head is running. Ask your Admin if nobody can reach it.

### "too many failed attempts — try again in …"

Too many wrong passwords for your account. Wait for the time it says (up to 30 minutes), then try
again. Ask an Admin to reset your password if you've forgotten it.

### "… presented a certificate this machine's pinned authority did not sign"

The Head's certificate changed. Usually this means the Head was rebuilt. Check with your Admin, then
delete the file the message names and run `cuttle login` again. If the Head wasn't rebuilt, don't log
in: tell your Admin.

### "… is not an address the Head's certificate covers"

You're pointing `cuttle` at an address the Head doesn't answer to. Run `cuttle setup --client` again,
which sets the right address up for you.

## Your Tidepool

### "your Tidepool isn't running" or "couldn't connect to your Tidepool (ssh exit 255)"

Restart it:

```bash
cuttle tidepool restart
```

or use **Restart** on the [Account page](dashboard:/account). If it still won't start, ask your Admin.

### Plain `ssh` says the host key changed

Your Tidepool was reset or the Head was set up again, and your machine has the old key. Run
`cuttle tidepool` once. It fetches the new key, and plain `ssh` works again afterwards.

### Writes fail with "No space left on device"

Your Tidepool is full. Check with `cuttle tidepool status` or the Files page, then delete what you
don't need. Big caches (`~/.cache`) are a common culprit.

### I can't edit a folder: "Read-only file system" or "locked"

A Dive is using that folder as its main folder. It's read-only in your Tidepool until the Dive ends,
and the Files page marks it "locked". Edit it inside the Dive, or end the Dive (`cuttle dive status`
shows which one).

### Packages I installed are gone

`cuttle tidepool reset` removes everything installed with apt. Install them again. A plain restart
keeps them. See [[Environments]].

## Starting a Dive

### "a Dive needs --time"

Every Dive needs a time limit, given once per folder. Add one, like `--time 4h`. After that it's
saved in the folder's `cuttle.toml`.

### "a Dive needs --vram"

Every Dive says how much GPU memory it needs, in GB, across all its GPUs, given once per folder.
Add it, like `--vram 16`. After that it's saved in the folder's `cuttle.toml`.

### "name the main folder, from your Tidepool home"

From your own machine, say which folder: `cuttle dive fractals`. Only inside your Tidepool does
`cuttle dive` default to the folder you're in.

### "this Dive is … GB — over the 25.0 GB cap"

A Dive can copy at most 25 GB. The message lists the biggest parts. Skip what you don't need with a
`.cuttleignore` file in the folder (see [[Dives#What gets copied|What gets copied]]), or move large data
out of the folders you're bringing.

### "your dotfiles are … — over the 1.0 GB cap"

Something big lives in one of the dotfiles CCC copies into every Dive, often a plugin folder like
`~/.oh-my-zsh` or `~/.vim`. The message lists the biggest ones. Trim them.

### "~/… is held by Dive …"

That folder is already the main folder of another Dive. End that Dive first, or use the folder as a
`--dep` instead.

### "no Arm has every requested feature"

No machine in the cluster has the Feature you asked for. The message lists the ones that exist.

### "… needs an environment — name a dependency file with --env"

That Feature needs a Python environment. Add `--env` with your dependency file. See [[Environments]].

## While a Dive runs

### My Dive has been waiting a long time

Check `cuttle queue` or the [Queue](dashboard:/). Either:

- the cluster is busy, and people with more [[Karma]] are ahead of you. Your Dive will still run.
  Karma never blocks a Dive.
- few machines can take your Dive. A large `--vram`, several `--gpus` or a `--feature` limits where it
  can go. Ask for less if you can.

### "process(es) were in ~/… before it locked and can still write to it"

Some programs in your Tidepool (a shell sitting in the folder, an open editor) still had the main
folder open when the Dive locked it. Anything they write there is overwritten when the Dive's copy
comes back. `cd` out of the folder and restart those programs. The message lists them.

### "environment still building" or "environment build failed"

Your Python environment builds in the background. Watch it with `tail -f /run/ccc-env.log`. If it
failed, the log says why. See [[Environments]].

### My Dive ended at exactly its `--time`

That's what `--time` does: when it's up, the Dive ends. Your main folder gets a final copy back.
Next time, ask for more. Asking for too much costs nothing extra, because you only pay for the
time you used.

### My Dive says "lost"

It stopped early without anyone ending it, usually because the machine it was on was switched off
or dropped off the network. CCC doesn't restart it. Your main folder has everything up to the last
minute-by-minute copy. Start a new Dive.

### "you can only end your own Dives"

You can end only Dives you started. Ask an Admin if someone else's Dive needs stopping.

### I left my Dive and it's still running

That's on purpose: leaving never ends a Dive, so a dropped connection can't lose your work. It keeps
its GPU and keeps costing Karma until you run `cuttle dive end <id>` or its time runs out.

## Copying files

### "this shell has no link to your machine"

`cuttle upload` and `cuttle download` only work from inside a shell you entered with
`cuttle tidepool` or `cuttle dive attach`. A plain `ssh` session or an editor's terminal has no way
back. Leave and come back in through `cuttle`. Inside `tmux`, a session started under an earlier
connection has the same problem. Open a fresh shell.

### "already there, copy refused (--force overwrites)"

Some files already exist at the destination and are different. They're listed. Add `--force` to
overwrite them, or pick another destination.

### "copy failed (rsync exit …) — run it again to resume"

The copy broke off, often because the network dropped. Run the same command again. It carries on
from where it stopped.

### "… is outside …, the folder you entered from"

From inside, you can only reach the folder on your machine that you ran `cuttle tidepool` (or
`cuttle dive attach`) from. Leave, `cd` to the folder you want on your machine, and come back in.

## Related

- [[Using CCC/index|Using CCC]]: parent
- [[Tidepool]]: restart, reset and limits
- [[Dives]]: how Dives start, wait and end
- [[Files]]: how copying works
- [[Environments]]: dependency files and installs
- [[Admin troubleshooting]]: problems with running the Head
