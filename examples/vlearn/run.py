"""Ant PPO on VSim — the entry point CCC runs for the `vlearn` Arm Feature test.

vlearn runtime setup (LD_LIBRARY_PATH, VL_WORKING_DIRECTORY, cache dir, licence key) is handled by
`--feature vlearn` (ccc_head_api.features). Nothing else is needed: rl_games writes `runs/` from the
current working directory with no flag to move it, and the CWD is the project.
"""
import pathlib
import runpy
import sys

ROOT = pathlib.Path(__file__).resolve().parent
TRAINER = ROOT / "train" / "rl_games_train.py"

# A short run by default: this is a hardware test, not a policy anyone will use. Overridable by passing
# args to the Spawn, which CCC forwards here verbatim.
argv = sys.argv[1:] or ["ant_custom", "train", "--max_epochs", "50"]

# train/ on sys.path: trainer does `from envs...` and envs is a plain dir beside it. `python train/x.py`
# puts this on sys.path[0] automatically; runpy.run_path doesn't, so it's prepended manually.
sys.path.insert(0, str(ROOT / "train"))

# rl_games_train.py is a top-level script (no main()), so it runs on load and reads sys.argv itself.
sys.argv = ["rl_games_train.py", *argv]
runpy.run_path(str(TRAINER), run_name="__main__")
