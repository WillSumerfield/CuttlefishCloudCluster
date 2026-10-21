#!/bin/bash
# Assemble the project from a vlearn checkout.
#
#   ./prepare.sh [path-to-vlearn-checkout]     (default: ../../../vlearn-main)
#
# Only `train/` — the trainer and its rl_games configs. That is the whole project now.
#
# The wheel, assets/, shaders/, cache/ and TurboActivate.dat used to be copied in here and are not any
# more: the ARM supplies all five (temp/FEATURES_BUILTIN.md). Its wheel is the build its licence
# covers, and its assets/ are ~970 MB that no longer cross the wire on every submission. `cuttle setup
# --arm` unpacks the wheel once; the job script symlinks the rest into the Spawn's working directory.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${1:-$HERE/../../../vlearn-main}"

[ -d "$SRC/train" ] || { echo "no vlearn checkout with a train/ at $SRC" >&2; exit 1; }

echo "vlearn checkout : $SRC"
rsync -a --delete "$SRC/train/" "$HERE/train/"

echo "ready."
