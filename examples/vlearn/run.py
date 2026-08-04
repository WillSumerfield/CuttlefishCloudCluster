"""Ant PPO on VSim — the entry point CCC runs for the `vlearn` Arm Feature test.

This used to be 120 lines that arranged vlearn's whole runtime: LD_LIBRARY_PATH (via a re-exec, since
the loader reads it once at exec), VL_WORKING_DIRECTORY, the cache directory, the licence key search,
and warnings for each way those could be wrong. All of it is CCC's now — `--feature vlearn` is the
whole request, and the job script does the rest (ccc_head_api.features).

What is left is one thing CCC has no opinion about: **rl_games decides where to write from the current
working directory**, and offers no flag to move it. A Spawn's CWD is its project directory, which is
scratch — only `$CCC_OUT` is staged home. So this moves the CWD and hands over.

Not a vlearn concern, which is why it did not go into the Feature profile: an rl_games project run
outside CCC has exactly the same problem, and a different trainer would not have it at all.
"""
import os
import pathlib
import runpy
import sys

ROOT = pathlib.Path(__file__).resolve().parent
TRAINER = ROOT / "train" / "rl_games_train.py"

# A short run by default: this is a hardware test, not a policy anyone will use. Overridable by passing
# args to the Spawn, which CCC forwards here verbatim.
argv = sys.argv[1:] or ["ant_custom", "train", "--max_epochs", "50"]

# Everything the trainer READS is resolved against VL_WORKING_DIRECTORY (the project dir), which CCC
# sets and which stays put — so moving the CWD only moves the output.
out = os.environ.get("CCC_OUT")
if out:
    os.chdir(out)
    print(f"ccc: writing runs/ under {out}", flush=True)

# `train/` on sys.path, because the trainer does `from envs.ant_environment_gpu import ...` and `envs`
# is a plain directory beside it. `python train/rl_games_train.py` would give this for free — the
# interpreter puts a script's own directory at sys.path[0] — but runpy.run_path does not do it for a
# script FILE. Prepended, matching where the interpreter would have put it.
sys.path.insert(0, str(ROOT / "train"))

# rl_games_train.py is a top-level script (no main()), so it runs on load and reads sys.argv itself.
sys.argv = ["rl_games_train.py", *argv]
runpy.run_path(str(TRAINER), run_name="__main__")
