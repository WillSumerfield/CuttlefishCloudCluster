#!/bin/bash
# Populate this Arm's CCC **Licence** directory from an activation that already exists on this box.
#
#   sudo ./sync-licence.sh [licence-dir] [activated-user-home]
#
# Defaults: the directory recorded in /etc/ccc/features.env, and the invoking user's home.
#
# WHY THIS EXISTS. The clean way to fill a Licence directory is to run the vendor's activation as the
# `ccc-licence` account, whose home it is — TurboActivate resolves its state directory through
# getpwuid(getuid())->pw_dir, so the identity that activates decides where the state lands. That needs
# a product key, and vlearn's LINUX wheels ship none (checked: of the four 0.3.9 wheels, only the two
# win_amd64 ones carry dist-info/licenses/License.key). So on Linux a box that is already activated
# cannot re-activate itself without the operator's own key, and copying the existing state is the
# only automatic path.
#
# The objection to copying was never the copying — it was asking a human to FIND three files named
# from an obfuscated per-release token. That is what this script removes: the token is read off the
# live activation, so nothing has to be known in advance. Re-run it after any vlearn upgrade, which
# is when the names change.
set -euo pipefail

DEST="${1:-}"
SRC_HOME="${2:-$(getent passwd "${SUDO_USER:-$USER}" | cut -d: -f6)}"

if [ -z "$DEST" ]; then
    DEST=$(sed -n 's/^export CCC_LICENCE_HOME=//p' /etc/ccc/features.env 2>/dev/null || true)
fi
[ -n "$DEST" ] || { echo "no licence dir given and none in /etc/ccc/features.env" >&2; exit 1; }
[ -d "$SRC_HOME/.prof" ] || { echo "no $SRC_HOME/.prof — is this box activated?" >&2; exit 1; }

# .prof holds exactly one file and nothing else, which is what makes it the anchor: whatever is in
# there IS the token. The three files share a suffix and differ only in the four characters after the
# dot (.6k33…/.k336…/.k633…), so the suffix found here locates the other two.
TOKEN=$(cd "$SRC_HOME/.prof" && ls -A | head -1)
[ -n "$TOKEN" ] || { echo "$SRC_HOME/.prof is empty — nothing to sync" >&2; exit 1; }
SUFFIX="${TOKEN#?????}"   # strip the leading dot + the four permuted characters
[ -n "$SUFFIX" ] || { echo "cannot derive a token suffix from $TOKEN" >&2; exit 1; }

echo "licence dir  : $DEST"
echo "copying from : $SRC_HOME"

n=0
for sub in .prof .local .config; do
    # Hidden files only, and only at the top of each directory: .local and .config are full of the
    # operator's own things, and every one of them would be handed to a Spawn.
    for f in "$SRC_HOME/$sub"/.*"$SUFFIX"; do
        [ -f "$f" ] || continue
        install -D -m 660 "$f" "$DEST/$sub/$(basename "$f")"
        echo "  $sub/$(basename "$f")"
        n=$((n + 1))
    done
done

[ "$n" -gt 0 ] || { echo "found no licence files matching the token" >&2; exit 1; }
if [ "$n" -ne 3 ]; then
    echo "warning: copied $n files, expected 3 (.prof, .local, .config)" >&2
fi

# Owned by the Licence account, readable AND writable by any Guise: a Spawn runs as a different uid,
# and TurboActivate rewrites the activation record on periodic online re-validation.
chown -R ccc-licence:ccc-guises "$DEST"
chmod 2770 "$DEST" "$DEST"/.prof "$DEST"/.local "$DEST"/.config 2>/dev/null || true

echo
echo "done — $n files. Submit with:"
echo "  cuttle run run.py --feature vlearn --output runs --vram 16 --gpu 1 --time 30 --follow --no-gitignore"
