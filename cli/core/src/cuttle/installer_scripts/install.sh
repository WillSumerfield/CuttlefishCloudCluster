#!/bin/sh
# CCC client installer. Drops the `cuttle` CLI on PATH via uv, and — if this bundle was built on a
# Head (`cuttle build-installer`) — points cuttle at that Head with its certificate pre-pinned, so
# the first `cuttle login` has nothing to type or confirm.
#
# Run from inside the unpacked bundle directory:  ./install.sh
#
# No hosted curl|sh: this script is built into the bundle and hand-carried, not fetched over the
# Head's self-signed cert. (astral.sh below is a real-CA host — a normal uv install, not that path.)
set -eu

here=$(cd "$(dirname "$0")" && pwd)

# 1. Ensure uv — the isolated tool installer. It puts each tool (and its deps) in its own venv, so
#    nothing leaks into the system Python and uninstalling is exact.
if ! command -v uv >/dev/null 2>&1; then
    echo "cuttle: uv not found — installing it..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv lands in ~/.local/bin; reach it for the rest of this run even before a new shell.
    export PATH="$HOME/.local/bin:$PATH"
fi

# 2. Install cuttle + all its Python deps (from PyPI) into an isolated tool venv. --force so a
#    re-run cleanly replaces an older install rather than erroring.
wheel=$(ls "$here"/cuttle-*.whl 2>/dev/null | head -n1)
[ -n "$wheel" ] || { echo "cuttle: no cuttle-*.whl next to install.sh" >&2; exit 1; }
echo "cuttle: installing $(basename "$wheel") ..."
uv tool install --force "$wheel"

# 3. If head credentials were baked in, pre-point + pre-pin this client. build-installer laid the
#    files out at exactly their destination names, so this is a copy — no URL parsing in shell.
if [ -d "$here/head" ]; then
    mkdir -p "$HOME/.ccc/config" "$HOME/.ccc/pins"
    cp "$here/head/config.yaml" "$HOME/.ccc/config/config.yaml"
    chmod 600 "$HOME/.ccc/config/config.yaml"
    if ls "$here"/head/pins/*.pem >/dev/null 2>&1; then
        cp "$here"/head/pins/*.pem "$HOME/.ccc/pins/"
        chmod 600 "$HOME"/.ccc/pins/*.pem
    fi
    echo "cuttle: installed, pointed at the Head, certificate pre-pinned."
    echo "cuttle: next -> cuttle login"
else
    echo "cuttle: installed."
    echo "cuttle: next -> cuttle setup --client   (or --head / --arm)"
fi
