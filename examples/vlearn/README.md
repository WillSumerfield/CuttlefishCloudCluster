# vlearn on CCC — the Arm Feature hardware test

Ant PPO on VSim, submitted as an ordinary Spawn. Exercises the whole `vlearn` **Feature** path end to
end: Eligibility narrowing to the one licensed Arm, an Environment built on the Head, the Arm's
**Licence** bound at the Spawn's Guise home, and declared outputs staged back.

Design and the hardware probes behind it: `temp/FEATURES_LICENCE.md`.

## Prerequisites

The Head must have the `vlearn` Feature in its catalog and a **Licence** directory recorded — both are
prompted at `cuttle setup --head`. You name a directory that does not exist yet; setup creates it as
the home of the `ccc-licence` account.

Then activate vlearn **once** as that account:

```bash
sudo -u ccc-licence env VL_WORKING_DIRECTORY=/path/to/vlearn-main <vlearn's activation>
```

That is the whole setup. The activation files land in the licence directory by themselves, because
TurboActivate chooses where to write by calling `getpwuid(getuid())->pw_dir` — it ignores `$HOME`
entirely, so the identity that activates is what decides the location. At Spawn time CCC binds that
directory at the Guise's home inside the Pod, where the same call finds it.

**Why not just copy the files.** TurboActivate keeps its state in three small hidden files, one each
under `~/.prof/`, `~/.local/` and `~/.config/`, named from an obfuscated per-release token. The token
is in neither `TurboActivate.dat` nor the vlearn wheels, so it cannot be looked up or derived — and a
version bump renames all three. A hand-made copy then goes stale silently, since the bind only checks
that the directory exists. Activating as the account avoids all of it.

Never point the licence directory at a real home: the whole thing is mounted into every Spawn that
asks for a Feature. Giving it to a CCC-owned account is what makes that structural rather than a rule
to remember.

## Running it

```bash
./prepare.sh                  # copies the wheel, train/ and assets/ out of a vlearn checkout
cuttle run run.py --feature vlearn --output runs --vram 16 --gpu 1 --time 30 --follow
```

`--feature vlearn` is what restricts placement to an Arm that actually holds the licence, and what
tells CCC this Spawn may see it. Without it the Spawn will land anywhere and fail at `create_gym`.

The first submission builds a ~6 GB conda Environment on the Head and takes a while; the download cache
is shared, so later ones are much faster.

## Why the pieces are shaped this way

**`environment.yml`, not `pyproject.toml`.** vlearn ships cp310/cp311 wheels and `base.sif` is Python
3.12. The uv path inherits the image interpreter, so it can never install vlearn; micromamba brings its
own Python. The conda system libraries (`sdl2`, `assimp=5`, `zeromq`, `czmq`, `libglu`, `mesalib`) are
there because `libgym.so` links them at load time — they are needed even headless, and `base.sif` is a
pinned cluster-wide image with no per-Arm package hook.

**`run.py`, not `train/rl_games_train.py` directly.** `LD_LIBRARY_PATH` has to be set before the process
starts — the loader reads it once at exec, so `os.environ` is already too late for the `ctypes.CDLL`
calls in `vlearn/__init__.py`. CCC runs `$py script.py` with no shell, so the script re-execs itself.

**Nothing here arranges the licence.** TurboActivate finds its activation state through
`getpwuid(getuid())->pw_dir` and ignores `$HOME` — verified on hardware, a redirected `HOME` fails and a
bogus one still passes. So the mount point is not something a submitter can influence; CCC binds it.

## Expected output

`runs/Ant_<timestamp>/` under `$CCC_OUT`, holding rl_games checkpoints (`nn/*.pth`) and TensorBoard
event files, downloaded to your machine when the Spawn finishes.

## If it fails

- `License validation failed` — the Arm's licence directory is unset, empty, or not readable by the
  Guise. Check `/etc/ccc/features.env` on the Arm.
- `Could not find the cache directory` — `cache/` is missing from the upload. `prepare.sh` creates it;
  it fails *before* the licence check, so it looks like a licensing problem and isn't.
- `libassimp.so.5: cannot open shared object file` — conda-forge resolved assimp 6. The `assimp=5` pin
  in `environment.yml` is load-bearing.
- `no Arm has every requested feature (vlearn)` — no admitted Arm declares it. Re-run
  `cuttle setup --head` (or `--arm`) and declare it there.
