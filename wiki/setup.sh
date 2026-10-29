#!/usr/bin/env bash
# Fetch Quartz around this wiki's own files, then install its packages and plugins.
#
# Only the wiki's files are committed (content/, quartz.config.yaml, plugins/, scripts).
# Quartz itself is fetched here at a pinned commit, so the repo stays small.
#
#   ./setup.sh           first time, or after a fresh clone
#   ./setup.sh --force   fetch Quartz again (e.g. after changing QUARTZ_COMMIT)
set -euo pipefail

QUARTZ_REPO="https://github.com/jackyzha0/quartz.git"
QUARTZ_COMMIT="97a2d05f80c4c50534959b1d0d41cc4b3895625e"   # v5 branch, 2026-09-20
NODE_MAJOR_MIN=22

cd "$(dirname "$0")"

fail() { echo "✗ $*" >&2; exit 1; }
step() { echo "→ $*"; }

command -v git >/dev/null || fail "git is not installed."
command -v node >/dev/null || fail "Node.js is not installed (need v${NODE_MAJOR_MIN}+)."
node_major="$(node -p 'process.versions.node.split(".")[0]')"
[ "$node_major" -ge "$NODE_MAJOR_MIN" ] || fail "Node.js v${node_major} found; Quartz needs v${NODE_MAJOR_MIN}+."

if [ -f quartz/bootstrap-cli.mjs ] && [ "${1:-}" != "--force" ]; then
  step "Quartz already present (use --force to fetch it again)."
else
  step "Fetching Quartz @ ${QUARTZ_COMMIT:0:7}"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  git clone -q --filter=blob:none "$QUARTZ_REPO" "$tmp/quartz" || fail "Could not clone $QUARTZ_REPO."
  git -C "$tmp/quartz" checkout -q "$QUARTZ_COMMIT" || fail "Commit $QUARTZ_COMMIT not found upstream."

  # Copy the framework, leaving the wiki's own files alone.
  keep=(.git .github .gitignore CODE_OF_CONDUCT.md Dockerfile README.md content docs)
  for entry in "$tmp/quartz"/* "$tmp/quartz"/.[!.]*; do
    name="$(basename "$entry")"
    [[ " ${keep[*]} " == *" $name "* ]] && continue
    rm -rf "./$name"
    cp -R "$entry" "./$name"
  done
fi

step "Applying wiki styles"
cp styles/custom.scss quartz/styles/custom.scss || fail "Could not copy styles/custom.scss into Quartz."

step "Installing packages"
npm ci --no-audit --no-fund --loglevel=error

step "Installing plugins (also builds plugins/graph)"
npx quartz plugin install --from-config
# The install skips plugins it already has, so rebuild the graph each time to pick up edits.
step "Building plugins/graph"
[ -d plugins/graph/node_modules ] || (cd plugins/graph && npm ci --no-audit --no-fund --loglevel=error)
(cd plugins/graph && npm run --silent build >/dev/null) || fail "Could not build plugins/graph."

echo "✓ Wiki ready. Run: npx quartz build --serve   → http://localhost:8080"
