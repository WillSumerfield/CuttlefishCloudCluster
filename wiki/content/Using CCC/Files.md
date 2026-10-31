---
title: Files
---
Your files live in your [[Tidepool]] on the Head. There are three ways to move them between your own
machine and the cluster: `cuttle upload` / `cuttle download`, the Files page in the
[[Glossary#Dashboard|Dashboard]], or any SSH tool (`scp`, `rsync`, VS Code).

## From your own machine

```bash
cuttle upload data.csv                 # → ~/data.csv in your Tidepool
cuttle upload ./dataset proj/data      # → ~/proj/data/dataset
cuttle download proj/results           # → ./results, here on your machine
cuttle download proj/results ~/Desktop # → ~/Desktop/results
```

- Paths in your Tidepool are from your home folder: `proj` and `~/proj` are the same thing.
- The thing you name always lands *inside* the destination folder, which is created if needed.
- Without a destination, uploads go to your Tidepool home and downloads come to the folder you're in.

### To or from a running Dive

Add `--dive` and the Dive's number:

```bash
cuttle upload config.yaml --dive 58           # → the Dive's main folder
cuttle download fractals/out.png --dive 58    # from the Dive's copy
```

Most of the time you don't need this. The Dive's main folder is copied back to your Tidepool every
minute anyway, so downloading from the Tidepool works too, and is at most a minute behind.

## From inside your Tidepool or a Dive

If you came in with `cuttle tidepool` or `cuttle dive attach`, the same two commands work from the
inside, pointed back at your machine:

```bash
cuttle download results.csv   # sends it to your machine
cuttle upload notes.md        # fetches it from your machine, into the folder you're in
```

On your machine's side, everything happens in **the folder you ran `cuttle tidepool` from**. Downloads
land there, and uploads are read from there. You can't reach outside that folder from the inside,
so start `cuttle tidepool` in the folder you want to share.

This only works in a shell you entered through `cuttle`. In a plain `ssh` session or an editor's
terminal you'll see "this shell has no link to your machine".

## How copies behave

- **Resumable.** If a big copy breaks off, run the same command again and it picks up where it
  stopped.
- **Never deletes.** Copies only add or update files.
- **Won't overwrite by surprise.** If any file is already at the destination and differs, nothing is
  copied and `cuttle` lists the clashes. Add `--force` to overwrite them.
- **Honours `.cuttleignore`.** A `.cuttleignore` file in a folder you copy skips whatever it lists,
  the same as for a [[Dives|Dive]].

`cuttle` uses `rsync` under the hood. Your machine needs it installed (`cuttle setup --client`
installs it).

## The Files page

[Open Files in the Dashboard](dashboard:/files). It shows your Tidepool home, with a bar showing how full it is.
You can:

- click into folders, and back out through the path at the top
- **Download** a file, or a whole folder as a `.zip`
- **Upload files** or **Upload a folder**, or drag files and folders onto the list
- make a **New folder**, **Rename** or **Delete** things

If an upload would replace files that are already there, the page lists them and asks whether to
overwrite them or skip them.

A main folder that a Dive is using is marked **locked**. You can browse and download from it, but
not change it until the Dive ends.

The page works as you, inside your own Tidepool, so it can see and change exactly what your shell
can.

## Other tools

Anything that speaks SSH can use your Tidepool's alias, which is named after your cluster (set up by
`cuttle login`, see [[Tidepool]]):

```bash
scp -r results mycluster:proj/
rsync -a mycluster:proj/out/ ./out/
```

VS Code's Remote-SSH lets you edit files in place.

## Related

- [[Using CCC/index|Using CCC]]: parent
- [[Tidepool]]: where your files live, and how full it can get
- [[Dives]]: how a Dive's main folder copies back
- [[CLI]]: `cuttle upload` and `cuttle download` in the reference
- [[Dashboard tour]]: the other Dashboard pages
