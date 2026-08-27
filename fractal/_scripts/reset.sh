#!/usr/bin/env bash
set -euo pipefail

# Reset one fractal tree: its worktrees and branches; keep the user node
# ----------------------------------------------------------------------

usage() {
    cat <<USAGE
Usage: reset.sh <repo> [options]

Reset one fractal tree: its worktrees and branches; keep the user node.

Options:
    --branch=<branch>    Tree root branch (default: the current checkout)
    --help|-h            Show this help message
USAGE
    exit 0
}

REPO=""
BRANCH=""

for arg in "$@"; do
    case "$arg" in
        --help | -h) usage ;;
        --branch=*) BRANCH="${arg#*=}" ;;
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

REPO_NAME=${REPO##*/}
WORKTREES_DIR="$REPO/.worktrees"

# the caller names the tree root (the checkout may sit on another branch);
# a standalone run falls back to the current branch
if [[ -z "$BRANCH" ]]; then
    BRANCH=$(git -C "$REPO" rev-parse --abbrev-ref HEAD)
fi

# nothing to tear down -- a clean no-op
if [[ ! -d "$WORKTREES_DIR" ]]; then
    echo "No worktrees found. Nothing to reset."
    exit 0
fi

# find active worktrees -- worktree dirs are named by branch, so the
# <branch>.* scope matches only the tree's own nodes and leaves sibling
# trees standing
WORKTREES=()
for SUBDIR in "$WORKTREES_DIR"/*/; do
    [[ ! -d "$SUBDIR" ]] && continue
    NAME=$(basename "$SUBDIR")
    [[ "$NAME" == "$BRANCH".* ]] || continue
    if [[ -f "$SUBDIR/.git" ]]; then
        WORKTREES+=("$(cd "$SUBDIR" && pwd)")
    fi
done

# ------ refuse while any node still runs in tmux
# guard every node BEFORE removing any, so a live session never strands a
# half-reset tree; grep -qxF (exact match), not tmux -t: -t resolves
# targets by prefix/fnmatch, so a short name false-matches longer session names
if [[ ${#WORKTREES[@]} -gt 0 ]]; then
    for WORKTREE in "${WORKTREES[@]}"; do
        WT_BRANCH=$(git -C "$WORKTREE" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
        # the guard is per-backend (mirrors kill.sh): a headless node owns no
        # session, so a matching name is another repo's sharing this basename
        # and must not block the reset; the node dir nests under the
        # .worktrees/.project/<branch> project prefix (mirrors Node.node_dir)
        PROJECT="."
        PROJECT_FILE="$WORKTREES_DIR/.project/$WT_BRANCH"
        if [[ -f "$PROJECT_FILE" ]]; then
            PROJECT=$(cat "$PROJECT_FILE")
        fi
        if [[ "$PROJECT" == "." ]]; then
            HEADLESS_FILE="$WORKTREE/.fractal/$WT_BRANCH/.headless"
        else
            HEADLESS_FILE="$WORKTREE/$PROJECT/.fractal/$WT_BRANCH/.headless"
        fi
        TMUX_SESSION_NAME="${REPO_NAME//[.:]/-} (${WT_BRANCH//./-})"
        if [[ ! -f "$HEADLESS_FILE" ]] \
            && tmux list-sessions -F '#{session_name}' 2>/dev/null \
            | grep -qxF "$TMUX_SESSION_NAME"; then
            echo "Error: node is still running in tmux ($TMUX_SESSION_NAME)" >&2
            echo "Kill it first with: fractal node kill $WT_BRANCH" >&2
            exit 1
        fi
    done
fi

# ------ refuse while any node is paused
# a parked loop has no tmux session for the guard above to catch; the caller
# settles paused nodes (kill sweep) before taking the lock, so this re-check
# under the caller's lock is the race backstop -- it closes the window a
# pause landing after the sweep, mid-reset, would slip through
if [[ ${#WORKTREES[@]} -gt 0 ]]; then
    for WORKTREE in "${WORKTREES[@]}"; do
        WT_BRANCH=$(git -C "$WORKTREE" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
        # the node dir nests under the .worktrees/.project/<branch> project
        # prefix (mirrors Node.node_dir)
        PROJECT="."
        PROJECT_FILE="$WORKTREES_DIR/.project/$WT_BRANCH"
        if [[ -f "$PROJECT_FILE" ]]; then
            PROJECT=$(cat "$PROJECT_FILE")
        fi
        if [[ "$PROJECT" == "." ]]; then
            STATUS_FILE="$WORKTREE/.fractal/$WT_BRANCH/.status"
        else
            STATUS_FILE="$WORKTREE/$PROJECT/.fractal/$WT_BRANCH/.status"
        fi
        if [[ -f "$STATUS_FILE" && "$(cat "$STATUS_FILE")" == "paused" ]]; then
            echo "Error: node is paused ($WT_BRANCH)" >&2
            echo "Resume it first with: fractal node resume $WT_BRANCH" >&2
            echo "  (or kill it with: fractal node kill $WT_BRANCH)" >&2
            exit 1
        fi
    done
fi

# ------ refuse while any worktree is locked
# pre-flight every worktree BEFORE removing any (the teardown is non-atomic,
# so a lock found mid-tear would strand a half-reset tree)
if [[ ${#WORKTREES[@]} -gt 0 ]]; then
    for WORKTREE in "${WORKTREES[@]}"; do
        GIT_DIR=$(git -C "$WORKTREE" rev-parse --absolute-git-dir 2>/dev/null || true)
        if [[ -n "$GIT_DIR" && -f "$GIT_DIR/locked" ]]; then
            echo "Error: worktree is locked: $WORKTREE" >&2
            echo "  (unlock with: git -C \"$REPO\" worktree unlock \"$WORKTREE\")" >&2
            exit 1
        fi
    done
fi

# ------ remove worktrees and branches
REMOTE_BRANCHES=()
if [[ ${#WORKTREES[@]} -gt 0 ]]; then
    for WORKTREE in "${WORKTREES[@]}"; do
        WT_BRANCH=$(git -C "$WORKTREE" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
        # note non-local branches actually present on origin (fail closed: an
        # unreadable config counts as local, an unreachable origin reports
        # nothing -- the note must never claim a branch that was never pushed)
        LOCAL=$(fractal config _get local --path="$WORKTREE" 2>/dev/null || echo true)
        if [[ "$LOCAL" != true ]]; then
            if git -C "$REPO" ls-remote --exit-code --heads origin "$WT_BRANCH" \
                >/dev/null 2>&1; then
                REMOTE_BRANCHES+=("$WT_BRANCH")
            fi
        fi

        # abort if removal fails -- continuing would orphan the git worktree
        # registration, which git worktree prune can't clean
        if ! git -C "$REPO" worktree remove --force "$WORKTREE" 2>/dev/null; then
            echo "Error: failed to remove worktree: $WORKTREE" >&2
            exit 1
        fi
        # >/dev/null: drop git's own "Deleted branch ... (was <sha>)"
        # so only the script's message below shows (no duplicate line)
        git -C "$REPO" branch -D "$WT_BRANCH" >/dev/null 2>&1 || true
        rm -f "$WORKTREES_DIR/.project/$WT_BRANCH"
        echo "Deleted $WORKTREE ($WT_BRANCH)"
    done
fi

# release phantom registrations (worktrees rm -rf'd out of band); .worktrees/
# itself stays -- it keeps the root's .project entry (sub-project database
# resolution) and the .lock the caller holds
git -C "$REPO" worktree prune

if [[ ${#REMOTE_BRANCHES[@]} -gt 0 ]]; then
    echo "Remote branches left on origin: ${REMOTE_BRANCHES[*]}"
fi
echo "Reset tree: $BRANCH"
