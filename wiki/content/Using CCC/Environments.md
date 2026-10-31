---
title: Environments
---
A Dive runs the same system as your [[Tidepool]]: the same tools, and the same packages you've
installed. Python libraries are separate: you list them in a dependency file, and each Dive builds a
fresh [[Glossary#Environment|Environment]] from it when it starts.

## What's already there

Every Tidepool and Dive starts from the same base image (Ubuntu 24.04). It has:

- Python 3.12, `uv` and `micromamba`
- `git`, `git-lfs`, `curl`, `wget`, `rsync`, `tmux`, `vim`, `nano`, `htop`, `nvtop`, `jq`, `zip`/`unzip`
- `build-essential` and the Python headers, for packages that compile things
- the CUDA 12.8 compiler (`nvcc`) and headers. The GPU driver comes from the machine the Dive runs
  on. CUDA libraries like cuDNN come from your Python packages (PyTorch brings its own, for example).

It has **no Python packages** on purpose. Every library comes from your own dependency file, so
nothing changes under you.

## Python packages: the dependency file

Put a dependency file in your main folder and name it with `--env`:

```bash
cuttle dive fractals --time 4h --vram 16 --env pyproject.toml
```

When the Dive starts, CCC builds a `.venv` next to that file and turns it on in every shell. What it
runs depends on the file:

| File | What CCC runs |
| --- | --- |
| `pyproject.toml` or `uv.lock` | `uv sync` |
| a `.txt` file, like `requirements.txt` | `uv venv`, then `uv pip install -r` the file |
| a `.yml` or `.yaml` file, like `environment.yml` | `micromamba create` from the file (conda packages) |

The file can be in a subfolder of your main folder (`--env src/requirements.txt`). Like every flag,
`--env` is saved in `cuttle.toml`, so you only give it once.

The build runs in the background, so your shell opens right away. Until the build finishes, each new
shell says so:

```
ccc: environment still building — tail -f /run/ccc-env.log
```

Watch it with `tail -f /run/ccc-env.log`. If it fails you'll see "environment build failed", and the
log says why. Fix the file and run the build yourself (`uv sync`, for example), or start a new Dive.

Builds are quick after the first time, because downloaded packages are cached on each machine and
shared between Dives.

The `.venv` stays in the Dive. It isn't copied in, and it isn't copied back to your Tidepool. Each
Dive builds its own.

Without `--env`, a Dive has bare Python 3.12 and nothing else. Some [[Arm features|Features]]
need an environment, and CCC tells you if yours does.

### In your Tidepool

Your Tidepool has no GPU, but you can still build and test the same environment there:

```bash
cd ~/fractals && uv sync
```

## System packages: apt

You have `sudo` in your Tidepool, so install system packages as you would anywhere:

```bash
sudo apt install ffmpeg
```

Every Dive you start afterwards has them too, because a Dive starts from a snapshot of your
Tidepool's system.

You can also `apt install` inside a Dive. When the Dive ends, CCC installs the same packages (same
versions) in your Tidepool, and removes anything you removed. The next time you open your Tidepool,
one line tells you what came back, for example:

```
Dive 9 brought back: ffmpeg · failed: foo=1.2 (not in archive)
```

The full log is in `~/.ccc/dive-<id>-installs.log`. A few things don't come back:

- a `.deb` file you installed by hand (`apt install ./x.deb` or `dpkg -i`). It's listed as failed or
  not replayed.
- changes made without apt, such as files copied into `/usr/local` or `/etc`
- Python packages installed outside an environment. Use your dependency file for those.

`cuttle tidepool reset` wipes everything you installed with apt, in your Tidepool and in future
Dives. See [[Tidepool]].

## Related

- [[Using CCC/index|Using CCC]]: parent
- [[Dives]]: where `--env` fits with the other Dive settings
- [[Tidepool]]: what a restart or reset keeps
- [[Arm features]]: software some Arms provide
- [[Troubleshooting]]: when an environment won't build
