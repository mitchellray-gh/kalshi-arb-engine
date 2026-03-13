#!/usr/bin/env bash
# Install repository git hooks directory as the active hooks path
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "Installing git hooks from: $ROOT_DIR/.githooks"
git config core.hooksPath ".githooks"
echo "Done. To revert: git config --unset core.hooksPath"
