#!/usr/bin/env bash
# Points git at the tracked hooks directory.
#
# core.hooksPath is used rather than copying into .git/hooks, so the hook stays
# version-controlled and a fix reaches everyone on the next pull instead of
# needing a re-copy.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
git -C "$ROOT" config core.hooksPath infra/host/git-hooks
echo "core.hooksPath -> infra/host/git-hooks"
echo "hooks active:"
for h in "$ROOT"/infra/host/git-hooks/*; do
  case "$(basename "$h")" in install.sh) continue ;; esac
  [ -x "$h" ] && echo "  $(basename "$h")" || echo "  $(basename "$h")  (NOT EXECUTABLE — run chmod +x)"
done
