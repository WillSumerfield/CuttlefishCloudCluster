#!/bin/bash
# Assemble the uploadable project from a vlearn checkout.
#
# The wheel, train/ and assets/ are ~hundreds of MB of third-party, licence-restricted material — they
# are copied in at submit time rather than committed here. Everything this script writes is gitignored.
#
#   ./prepare.sh [path-to-vlearn-checkout] [--full-assets]   (default checkout: ../../../vlearn-main)
#
# Only Ant's own asset is copied by default. assets/ is ~970 MB of other robots (g1, mjcf, urdf, …) and
# ant.vsim is 14 KB — the difference is a gigabyte crossing the wire on every submission. Pass
# --full-assets if you switch this example to an environment that needs more.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FULL=""
ARGS=()
for a in "$@"; do
    if [ "$a" = "--full-assets" ]; then FULL=1; else ARGS+=("$a"); fi
done
SRC="${ARGS[0]:-$HERE/../../../vlearn-main}"

[ -d "$SRC" ] || { echo "no vlearn checkout at $SRC" >&2; exit 1; }

WHEEL=$(ls "$SRC"/vlearn-*-cp311-cp311-linux_x86_64.whl 2>/dev/null | tail -1)
[ -n "$WHEEL" ] || { echo "no cp311 linux wheel in $SRC" >&2; exit 1; }

# The wheel filename is pinned in environment.yml, so a version bump has to be reflected there too
# rather than silently resolving to whatever happens to be lying around.
want=$(grep -o 'vlearn-[0-9.]*-cp311-cp311-linux_x86_64.whl' "$HERE/environment.yml")
[ "$(basename "$WHEEL")" = "$want" ] || {
    echo "wheel mismatch: found $(basename "$WHEEL"), environment.yml pins $want" >&2
    echo "update the pip: line in environment.yml, then re-run" >&2
    exit 1
}

echo "vlearn checkout : $SRC"
cp -v "$WHEEL" "$HERE/"
# TurboActivate.dat — the product definition the licence check reads. vlearn looks for it in
# VL_WORKING_DIRECTORY, which is this directory at run time, so it has to travel with the upload.
# Not a secret and not machine state: it is identical on every install, ships with the checkout, and
# is useless without an activation. Without it the Spawn dies at `gym.cpp: License validation failed`
# even on an Arm whose Licence is bound correctly.
[ -f "$SRC/TurboActivate.dat" ] || { echo "no TurboActivate.dat in $SRC" >&2; exit 1; }
cp -v "$SRC/TurboActivate.dat" "$HERE/"
# train/ carries rl_games_train.py and rl_games_config/; assets/ carries ant.vsim. Both are resolved
# against VL_WORKING_DIRECTORY (= the project root) at run time, so they keep their names here.
rsync -a --delete "$SRC/train/" "$HERE/train/"
rm -rf "$HERE/assets"
if [ -n "$FULL" ]; then
    rsync -a "$SRC/assets/" "$HERE/assets/"
else
    # train/envs/ant_environment_common.py: filename = "assets/vsim/ant.vsim"
    install -D "$SRC/assets/vsim/ant.vsim" "$HERE/assets/vsim/ant.vsim"
fi
# vlearn writes generated collision meshes here; create_gym fails outright if it is missing.
mkdir -p "$HERE/cache/tmp"

echo
echo "ready. submit from this directory with:"
echo "  cuttle run run.py --feature vlearn --output runs --vram 16 --gpu 1 --time 30 --follow"
