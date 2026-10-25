#!/usr/bin/env bash
set -euo pipefail

# Run linters on the node's worktree
# ----------------------------------

# NODE_DIR walks up from this hook's seeded location -- the inverse of
# Node.node_dir's <worktree>[/<project>]/.fractal/<branch> derivation
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
NODE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKTREE_DIR="$(git -C "$(dirname "$NODE_DIR")" rev-parse --show-toplevel)"

SKILLS_DIR="$NODE_DIR/skills"
MEMORY_DIR="$NODE_DIR/memory"

PROJECT="$(fractal config _get project --path="$WORKTREE_DIR" 2>/dev/null || echo ".")"
WIKI="$(fractal config _get wiki --path="$WORKTREE_DIR" 2>/dev/null || true)"
if [[ "$PROJECT" == "." ]]; then
    WIKI_DIR="$WORKTREE_DIR/${WIKI:-wiki}"
else
    WIKI_DIR="$WORKTREE_DIR/$PROJECT/${WIKI:-wiki}"
fi

if [[ -d "$SKILLS_DIR" ]]; then
    for ENTRY in "$SKILLS_DIR"/*; do
        [[ -e "$ENTRY" ]] || continue
        if [[ ! -d "$ENTRY" ]]; then
            echo "Error: skills/$(basename "$ENTRY") is not a directory" >&2
            exit 1
        fi
        if [[ ! -f "$ENTRY/SKILL.md" ]]; then
            echo "Error: skills/$(basename "$ENTRY")/ is missing SKILL.md" >&2
            exit 1
        fi
    done
fi

if command -v wiki &>/dev/null; then
    if [[ -f "$MEMORY_DIR/_index.md" ]]; then
        wiki lint --path="$MEMORY_DIR" \
            || echo "Warning: memory wiki ($MEMORY_DIR) has lint issues" >&2
    fi
    if [[ -f "$WIKI_DIR/_index.md" ]]; then
        wiki lint --path="$WIKI_DIR" \
            || echo "Warning: project wiki ($WIKI_DIR) has lint issues" >&2
    fi
fi
