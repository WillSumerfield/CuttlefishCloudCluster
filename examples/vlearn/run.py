"""Ant PPO on VSim — the entry point CCC runs for the `vlearn` Arm Feature test.

A wrapper rather than submitting `train/rl_games_train.py` directly, because two things have to happen
before vlearn is importable at all, and neither can be done from inside a running Python process:

  * **LD_LIBRARY_PATH.** vlearn/__init__.py `ctypes.CDLL`s libvsimdynamics.so and libgym.so, which link
    SDL2/assimp/zeromq/czmq/GLU (from the conda prefix) and libcudart (from torch's nvidia wheels).
    The dynamic loader reads LD_LIBRARY_PATH once, at exec — so setting os.environ here is already too
    late for those loads. Hence the re-exec below.
  * **VL_WORKING_DIRECTORY.** vlearn resolves assets, shaders, cache and its licence files against it,
    and rl_games_train.py resolves `train/rl_games_config/` against it too.

CCC runs `$py run.py <args>` with no shell (ccc_head_api.spawns.PAYLOAD_SCRIPT), so `LD_LIBRARY_PATH=…
python …` is not available — this file is the only place the environment can be fixed.

The Arm's **Licence** arrives separately: CCC binds it at this Spawn's Guise home, which is where
TurboActivate looks and the one thing this script must NOT try to arrange itself.
"""
import os
import pathlib
import runpy
import sys

STAGE = "CCC_VLEARN_STAGE"
ROOT = pathlib.Path(__file__).resolve().parent


def _lib_dirs() -> list[str]:
    """Every directory holding a .so that libgym.so needs, in load order.

    Globbed rather than hardcoded: the conda prefix, the vlearn package and torch's bundled CUDA
    runtime all move with the Environment CCC built, which is not this file's to predict.
    """
    site = pathlib.Path(sys.prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    dirs = [pathlib.Path(sys.prefix) / "lib", site / "vlearn" / "lib"]
    # torch's nvidia-* wheels: libcudart.so.13 and friends, one lib/ per component.
    dirs += sorted(p for p in site.glob("nvidia/*/lib") if p.is_dir())
    found = [str(d) for d in dirs if d.is_dir()]
    # Said out loud, because the failure it prevents is unhelpful: without these, `import vlearn` dies
    # on "libcudart.so.13: cannot open shared object file", which names a CUDA problem when the real
    # fault is an Environment that resolved no torch.
    if not any(p for d in found for p in pathlib.Path(d).glob("libcudart.so*")):
        print("ccc: WARNING no libcudart in the Environment — is torch (cu130) in environment.yml?",
              file=sys.stderr, flush=True)
    return found


if os.environ.get(STAGE) != "2":
    env = dict(os.environ)
    env[STAGE] = "2"
    env["LD_LIBRARY_PATH"] = ":".join(_lib_dirs() + [env.get("LD_LIBRARY_PATH", "")]).rstrip(":")
    env["VL_WORKING_DIRECTORY"] = str(ROOT)
    # create_gym fails at gym.cpp:174 ("Could not find the cache directory") if this is absent — before
    # it ever reaches the licence check, which makes it look like a licensing problem when it isn't.
    (ROOT / "cache" / "tmp").mkdir(parents=True, exist_ok=True)
    os.execve(sys.executable, [sys.executable, __file__, *sys.argv[1:]], env)

# ---- stage 2: the environment is right, so vlearn is importable ----------------------------------

# A short run by default: this is a hardware test, not a policy anyone will use. Overridable by
# passing args to the Spawn, which CCC forwards here verbatim.
argv = sys.argv[1:] or ["ant_custom", "train", "--max_epochs", "50"]

print(f"ccc: VL_WORKING_DIRECTORY={os.environ['VL_WORKING_DIRECTORY']}", flush=True)
print(f"ccc: running rl_games_train.py {' '.join(argv)}", flush=True)

import vlearn  # noqa: E402  -- import only now, once LD_LIBRARY_PATH is in effect

print(f"ccc: vlearn {getattr(vlearn, '__version__', '?')} loaded from {vlearn.__file__}", flush=True)

# Only what a Spawn writes under $CCC_OUT is staged home; the project dir is scratch and is thrown away.
# rl_games resolves its `train_dir` ("runs") against the CWD, and offers no flag to move it — so the CWD
# is what has to move. Safe because everything the trainer reads is resolved against
# VL_WORKING_DIRECTORY instead, which stays pointed at the project: the config at
# `train/rl_games_config/`, and the .vsim assets.
out = os.environ.get("CCC_OUT")
if out:
    os.chdir(out)
    print(f"ccc: writing runs/ under {out}", flush=True)

# rl_games_train.py is a top-level script (no main()), so it runs on load and reads sys.argv itself.
sys.argv = ["rl_games_train.py", *argv]
runpy.run_path(str(ROOT / "train" / "rl_games_train.py"), run_name="__main__")
