# vlearn on CCC — the Arm Feature hardware test

Ant PPO on VSim, submitted as an ordinary Spawn. Exercises the whole `vlearn` **Feature** path end to
end: Eligibility narrowing to the one licensed Arm, an Environment built on the Head, the Arm's own
vlearn package and assets arranged by CCC, its **Licence** bound at the Spawn's Guise home, and
declared outputs staged back.

## Prerequisites

Somebody's box has to *have* vlearn, and say so at `cuttle setup --arm` (or `--head`, for the Head's
own co-located Arm). Setup asks three things: whether this box has it, where the checkout is, and
where the product key is. It then unpacks the checkout's Linux wheels, creates the **Licence**
directory, and — as the Arm is admitted — runs an activation Spawn on it. An Arm whose activation
fails is not admitted, so a Feature that appears in `cuttle arm list` is one that works.

Nothing to sync, copy or hand-place. The licence directory is created as the home of the `ccc-licence`
account, which is what keeps it out of a real human's home — the whole directory is mounted into every
Spawn that asks for a Feature.

## Running it

```bash
./prepare.sh                  # copies train/ out of a vlearn checkout — that is the whole project
cuttle run run.py --feature vlearn --output runs --vram 16 --gpu 1 --time 30 --follow
```

`--feature vlearn` is the entire request. It restricts placement to an Arm that holds the licence, and
it is what tells CCC to arrange the runtime. Without it the Spawn lands anywhere and fails at
`import vlearn`.

The first submission builds the conda Environment on the Head and takes a while; the download cache is
shared, so later ones are much faster.

## What the upload does *not* contain

The wheel, `assets/`, `shaders/`, `cache/` and `TurboActivate.dat`. All five come from the Arm:

- **The wheel**, because vlearn's activation state is per-release and the Arm is the machine that was
  activated — so the copy that runs has to be the copy its owner licensed. `setup --arm` unpacks it
  once; the job script puts it on `PYTHONPATH` ahead of the Environment.
- **`assets/`** (~970 MB) and **`shaders/`**, symlinked into the Spawn's working directory. Symlinks
  and not copies because `$proj` is what the **Recovery Window** measures and rsyncs — a real gigabyte
  in there would silently disable recovery for every vlearn Spawn.
- **`cache/`** is the one of the four vlearn writes, so it is copied per Spawn (~1 MB) rather than
  symlinked into a read-only bind.

So `prepare.sh` copies `train/` and nothing else, and a submission is a few hundred KB.

## Why the pieces are shaped this way

**`environment.yml`, not `pyproject.toml`.** vlearn ships cp310/cp311 wheels and `base.sif` is Python
3.12. The uv path inherits the image interpreter; micromamba brings its own. The system libraries at
the top of the file are the generated starter — `cuttle feature show vlearn` prints exactly that, and
everything below the divider is this example's own.

**`run.py` is four lines of work.** It used to arrange `LD_LIBRARY_PATH` (through a re-exec, since the
loader reads it once at `exec`), `VL_WORKING_DIRECTORY`, the cache directory and the licence key. CCC
does all of that now. What is left is that rl_games writes to the current working directory and has no
flag to move it — an rl_games quirk, not a vlearn one, which is why it lives here and not in the
Feature profile.

**Nothing here arranges the licence.** TurboActivate finds its activation state through
`getpwuid(getuid())->pw_dir` and ignores `$HOME` — a redirected `HOME` fails and a bogus one still
passes. So the mount point is not something a submitter can influence; CCC binds it.

## Expected output

`runs/Ant_<timestamp>/` under `$CCC_OUT`, holding rl_games checkpoints (`nn/*.pth`) and TensorBoard
event files, downloaded to your machine when the Spawn finishes.

## If it fails

- `this Spawn's Environment is Python cp312, and this Arm's vlearn is built for: cp310 cp311` — the
  `python=3.11` pin is missing from `environment.yml`, or the Spawn declared no Environment at all.
- `License validation failed` — the Arm's licence directory is empty or unreadable by the Guise. It
  should have been filled by the activation Spawn at admit; check `/etc/ccc/features.env` on the Arm.
- `libassimp.so.5: cannot open shared object file` — conda-forge resolved assimp 6. The `assimp=5` pin
  is load-bearing.
- `no Arm has every requested feature (vlearn)` — no admitted Arm declares it. Re-run
  `cuttle setup --arm` on the box that has it.
