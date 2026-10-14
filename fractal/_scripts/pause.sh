#!/usr/bin/env bash
set -euo pipefail

# Abort a pausing node's in-flight agent invocation (never the loop)
# ------------------------------------------------------------------

usage() {
    cat <<USAGE
Usage: pause.sh <path>

Abort the in-flight agent invocation of a pausing node.

Options:
    --help|-h    Show this help message
USAGE
    exit 0
}

WORKTREE_DIR=""

for arg in "$@"; do
    case "$arg" in
        --help | -h) usage ;;
        *)
            if [[ -z "$WORKTREE_DIR" ]]; then
                WORKTREE_DIR="$arg"
            fi
            ;;
    esac
done

if [[ -z "$WORKTREE_DIR" ]]; then
    echo "Error: path is required" >&2
    exit 1
fi

if [[ ! "$WORKTREE_DIR" = /* ]]; then
    WORKTREE_DIR="$(cd "$WORKTREE_DIR" && pwd)"
fi

# derive the node's data directory (mirrors kill.sh): the project prefix
# comes from the .worktrees/.project/<branch> cache in the main repo
STEP_PGID_FILE=""
if COMMON_DIR=$(git -C "$WORKTREE_DIR" rev-parse --git-common-dir 2>/dev/null); then
    if [[ "$COMMON_DIR" = /* ]]; then
        REPO_ROOT=$(cd "$COMMON_DIR/.." && pwd)
    else
        REPO_ROOT=$(cd "$WORKTREE_DIR/$COMMON_DIR/.." && pwd)
    fi
    if BRANCH=$(git -C "$WORKTREE_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null); then
        PROJECT="."
        PROJECT_FILE="$REPO_ROOT/.worktrees/.project/$BRANCH"
        if [[ -f "$PROJECT_FILE" ]]; then
            PROJECT=$(cat "$PROJECT_FILE")
        fi
        if [[ "$PROJECT" == "." ]]; then
            STEP_PGID_FILE="$WORKTREE_DIR/.fractal/$BRANCH/.step_pgid"
        else
            STEP_PGID_FILE="$WORKTREE_DIR/$PROJECT/.fractal/$BRANCH/.step_pgid"
        fi
    fi
fi

# no recorded step group means no agent in flight -- the loop parks at its
# next pause checkpoint on its own
if [[ -z "$STEP_PGID_FILE" || ! -f "$STEP_PGID_FILE" ]]; then
    echo "No agent in flight (loop parks at its next checkpoint)"
    exit 0
fi

STEP_PGID=$(tr -d '[:space:]' <"$STEP_PGID_FILE") || true
if [[ -z "$STEP_PGID" ]] || ! kill -0 -- "-$STEP_PGID" 2>/dev/null; then
    echo "No agent in flight (loop parks at its next checkpoint)"
    exit 0
fi

# the abort is marked durably before it is delivered -- the loop classifies
# the killed step as paused by marker OR signal, so a resume that withdraws
# the signal mid-abort can never demote the park to a failure; a step the
# reap spares (a self-pause runs inside it) is never marked
ABORT_MARKER="${STEP_PGID_FILE%.step_pgid}.pause_abort"

# reap the agent's whole tree -- its tool commands run in groups (or sessions)
# of their own; the loop lives above the tree and survives to record the park;
# survivors and worktree strays are named on stderr
if ! fractal node _reap --pgid="$STEP_PGID" --worktree="$WORKTREE_DIR" \
    --abort-marker="$ABORT_MARKER"; then
    # the helper could not run: TERM the agent group alone; when timeout(1)
    # leads the group it forwards the TERM to the agent
    touch "$ABORT_MARKER"
    kill -TERM -- "-$STEP_PGID" 2>/dev/null || true

    WAITED=0
    while kill -0 -- "-$STEP_PGID" 2>/dev/null && ((WAITED < 5)); do
        sleep 1
        ((WAITED++)) || true
    done

    if kill -0 -- "-$STEP_PGID" 2>/dev/null; then
        kill -KILL -- "-$STEP_PGID" 2>/dev/null || true
    fi
fi

echo "Aborted in-flight agent (pgid $STEP_PGID)"
