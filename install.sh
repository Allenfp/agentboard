#!/usr/bin/env bash
# One-line installer for yamlboard.
#
#   curl -LsSf https://raw.githubusercontent.com/Allenfp/yamlboard/main/install.sh | bash
#
# Installs uv if you do not have it (the official astral.sh installer), then
# installs yamlboard as a uv tool so `yamlboard` lands on your PATH.
# Idempotent: re-running upgrades in place.
set -euo pipefail

REPO="https://github.com/Allenfp/yamlboard"

# This script pipes curl into a shell, so the URL is checked before the first
# fetch. RAW_BASE is derived from REPO rather than spelled out again: two
# literals drift, and the day they do the example board is fetched from
# somebody else's repository.
case "$REPO" in
  https://github.com/*) ;;
  *) printf 'error: refusing to fetch %s; want an https://github.com URL\n' "$REPO" >&2
     exit 1 ;;
esac
RAW_BASE="https://raw.githubusercontent.com/${REPO#https://github.com/}/main"

say() { printf '  %s\n' "$*"; }

if ! command -v uv >/dev/null 2>&1; then
  say "uv not found - installing it (https://docs.astral.sh/uv/)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # make it visible in this shell without the user restarting
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    printf 'error: uv installed but is not on PATH. Open a new shell and re-run.\n' >&2
    exit 1
  fi
fi

say "installing yamlboard from ${REPO}"
uv tool install --force "git+${REPO}"

say "done."
yamlboard --version || {
  printf 'error: yamlboard is installed but not on PATH; add %s to it and re-open your shell.\n' \
    "$HOME/.local/bin" >&2
  exit 1
}
cat <<EONEXT

  start a board in an empty directory:

    mkdir -p ~/scratch && cd ~/scratch
    git init -q -b main
    yamlboard init
    yamlboard serve

  ...or play with the demo store - cards, live claims and one path conflict
  reported by \`yamlboard conflicts\`. It is a directory now, not one file, so
  it is copied after a shallow clone instead of fetched by curl:

    git clone --depth 1 ${REPO} /tmp/yamlboard-demo
    mkdir -p ~/demo && cd ~/demo && git init -q -b main
    cp -r /tmp/yamlboard-demo/examples/demo/.yamlboard ./
    yamlboard conflicts

  Just want to read the shape of a board before copying one?
  ${RAW_BASE}/examples/demo/.yamlboard/board.yaml

EONEXT
