#!/bin/bash
# Installs this repo's tracked git hooks into .git/hooks/.
#
# .git/hooks/ isn't version-controlled, so hooks tracked under
# tools/githooks/ need to be (re)installed after every fresh clone.
# Run this once after cloning:
#   tools/install-hooks.sh

set -eu

REPO_ROOT="$(cd "$(git rev-parse --show-toplevel)" && pwd)"
SRC_DIR="$REPO_ROOT/tools/githooks"
DST_DIR="$REPO_ROOT/.git/hooks"

if [ ! -d "$SRC_DIR" ]; then
    echo "install-hooks: $SRC_DIR not found." >&2
    exit 1
fi

for hook in "$SRC_DIR"/*; do
    name="$(basename "$hook")"
    dst="$DST_DIR/$name"
    ln -sf "../../tools/githooks/$name" "$dst"
    chmod +x "$hook"
    echo "installed: $name -> .git/hooks/$name"
done

echo "install-hooks: done."
