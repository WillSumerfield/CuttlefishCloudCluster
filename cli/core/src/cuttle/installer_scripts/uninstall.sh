#!/bin/sh
# CCC uninstaller. Reverses whatever setup did to this box, THEN removes the cuttle tool itself.
#
# Order matters: `cuttle teardown` is a cuttle subcommand, so it has to run while cuttle still
# exists — once its tool venv is gone there is nothing left to run. So teardown first (from cuttle),
# `uv tool uninstall` second (from this shell, after cuttle has exited — nothing deletes its own
# running venv).
set -u

if command -v cuttle >/dev/null 2>&1; then
    # --full sweeps every role's state plus ~/.ccc, base.sif and the TLS cert, tolerant of whatever
    # was never there — the right hammer for "remove all of it, including anything a partial install
    # left behind." --yes skips the typed confirmation (this is already the confirmation).
    echo "cuttle: tearing this box down..."
    cuttle teardown --full --yes \
        || echo "cuttle: teardown reported problems — continuing to remove the tool" >&2
else
    echo "cuttle: 'cuttle' not on PATH — skipping teardown, removing the tool only" >&2
fi

# uv-tool isolation means this removes cuttle AND every one of its Python deps in one shot.
if command -v uv >/dev/null 2>&1; then
    uv tool uninstall cuttle 2>/dev/null || true
fi
echo "cuttle: removed."
