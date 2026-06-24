#!/usr/bin/env bash
set -euo pipefail

# Remove all worktrees and clean up .worktrees/ directory.

usage() {
    cat <<USAGE
Usage: reset.sh <path> [options]

Remove all worktrees and clean up .worktrees/ directory.

Options:
    --force|-f    Delete remaining worktrees before resetting
    --help|-h     Show this help message
USAGE
    exit 0
}

REPO=""
FORCE=false

for arg in "$@"; do
    case "$arg" in
        --help | -h) usage ;;
        --force | -f) FORCE=true ;;
        *)
            if [[ -z "$REPO" ]]; then
                REPO="$arg"
            fi
            ;;
    esac
done

if [[ -z "$REPO" ]]; then
    echo "Error: path is required" >&2
    exit 1
fi

if [[ ! "$REPO" = /* ]]; then
    REPO="$(cd "$REPO" && pwd)"
fi

# accept any git repo: a linked worktree has a `.git` *file* (not dir), and a
# bare repo has no `.git` at all, so test via rev-parse rather than a dir check
if ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
    echo "Error: $REPO is not a git repository" >&2
    exit 1
fi

WORKTREES_DIR="$REPO/.worktrees"
if [[ ! -d "$WORKTREES_DIR" ]]; then
    echo "No .worktrees/ directory found. Nothing to reset."
    exit 0
fi

# find active worktrees
REMAINING=()
for SUBDIR in "$WORKTREES_DIR"/*/; do
    [[ ! -d "$SUBDIR" ]] && continue
    if [[ -f "$SUBDIR/.git" ]]; then
        REMAINING+=("$(cd "$SUBDIR" && pwd)")
    fi
done

if [[ ${#REMAINING[@]} -gt 0 ]]; then
    if [[ "$FORCE" == false ]]; then
        echo "Error: worktrees still exist (use --force to delete):" >&2
        for WORKTREE in "${REMAINING[@]}"; do
            echo "  $WORKTREE" >&2
        done
        exit 1
    fi

    for WORKTREE in "${REMAINING[@]}"; do
        BRANCH=$(git -C "$WORKTREE" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")

        # abort if removal fails (e.g. locked) -- the rm -rf below
        # would orphan the git worktree registration, which git
        # worktree prune can't clean
        if ! git -C "$REPO" worktree remove --force "$WORKTREE" 2>/dev/null; then
            echo "Error: failed to remove worktree: $WORKTREE" >&2
            echo "  (locked? unlock with: git -C \"$REPO\" worktree unlock \"$WORKTREE\")" >&2
            exit 1
        fi
        git -C "$REPO" branch -D "$BRANCH" 2>/dev/null || true
        echo "Deleted $WORKTREE ($BRANCH)"
    done
fi

git -C "$REPO" worktree prune
rm -rf "$WORKTREES_DIR"
echo "Reset complete: $REPO"
