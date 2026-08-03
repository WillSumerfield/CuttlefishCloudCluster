# vlearn on CCC — the Arm Feature hardware test

Ant PPO on VSim, submitted as an ordinary Spawn. Exercises the whole `vlearn` **Feature** path end to
end: Eligibility narrowing to the one licensed Arm, an Environment built on the Head, the Arm's
**Licence** bound at the Spawn's Guise home, and declared outputs staged back.

Design and the hardware probes behind it: `temp/FEATURES_LICENCE.md`.

## Prerequisites

The Head must have the `vlearn` Feature in its catalog and a **Licence** directory recorded — both are
prompted at `cuttle setup --head`.

**What the licence actually is.** Not a `License.key`, and not `TurboActivate.dat` (that ships with the
vlearn checkout — it is the product definition, part of the app, not machine state). TurboActivate
scatters three small hidden files across the activated user's home, one per directory:

```
~/.prof/.<token>      the saved product key, plaintext
~/.local/.<token>     the activation record — rewritten on periodic online re-validation
~/.config/.<token>
```

`<token>` is an obfuscated per-product string, the same in all three but permuted at the front, and not
stable across vlearn releases. Find them with `ls -a ~/.prof ~/.local ~/.config`; `.prof/` holds
nothing else, which is the easy one to start from.

The licence directory is a **copy**, keeping that layout — `<dir>/.prof/`, `<dir>/.local/`,
`<dir>/.config/` — and holding **nothing else**: the whole directory is mounted into every Spawn that
asks for a Feature, so anything else living there goes with it.

Never point it at a real home directory.

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
