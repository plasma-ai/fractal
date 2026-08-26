#!/usr/bin/env bash
set -euo pipefail

# Kill a running node by reaping its process group and tmux session
# -----------------------------------------------------------------

usage() {
    cat <<USAGE
Usage: kill.sh <path>

Kill a node by reaping its process group and tmux session.

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

REPO_NAME=${WORKTREE_DIR##*/}
REPO_ROOT=""
if COMMON_DIR=$(git -C "$WORKTREE_DIR" rev-parse --git-common-dir 2>/dev/null); then
    if [[ "$COMMON_DIR" = /* ]]; then
        REPO_ROOT=$(cd "$COMMON_DIR/.." && pwd)
    else
        REPO_ROOT=$(cd "$WORKTREE_DIR/$COMMON_DIR/.." && pwd)
    fi
    REPO_NAME=${REPO_ROOT##*/}
fi

TMUX_SESSION_NAME="${REPO_NAME//[.:]/-}"
BRANCH=""
if BRANCH=$(git -C "$WORKTREE_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null); then
    TMUX_SESSION_NAME="${REPO_NAME//[.:]/-} (${BRANCH//./-})"
fi

# derive the node's data directory (mirrors Node.node_dir): the project
# prefix comes from the .worktrees/.project/<branch> cache in the main repo
PGID_FILE=""
if [[ -n "$REPO_ROOT" && -n "$BRANCH" ]]; then
    PROJECT="."
    PROJECT_FILE="$REPO_ROOT/.worktrees/.project/$BRANCH"
    if [[ -f "$PROJECT_FILE" ]]; then
        PROJECT=$(cat "$PROJECT_FILE")
    fi
    if [[ "$PROJECT" == "." ]]; then
        PGID_FILE="$WORKTREE_DIR/.fractal/$BRANCH/.pgid"
    else
        PGID_FILE="$WORKTREE_DIR/$PROJECT/.fractal/$BRANCH/.pgid"
    fi
fi

# resolve pane pid by exact session_name match (spaces/parens
# defeat split-on-space lookup)
PANE_PID=$(
    tmux list-panes -a -F '#{session_name}'$'\t''#{pane_pid}' 2>/dev/null \
        | awk -F'\t' -v name="$TMUX_SESSION_NAME" '$1 == name { print $2; exit }' \
        || true
)

# capture pgid before teardown -- pane shell vanishes but orphans keep the pgid
PGID=""
if [[ -n "$PANE_PID" ]]; then
    PGID=$(ps -o pgid= -p "$PANE_PID" 2>/dev/null | tr -d ' ') || true
fi

# fall back to the pgid recorded at run start when the pane lookup is empty --
# an out-of-band pane death leaves the agent group headless with no tmux handle
if [[ -z "$PGID" && -n "$PGID_FILE" && -f "$PGID_FILE" ]]; then
    RECORDED_PGID=$(tr -d '[:space:]' <"$PGID_FILE") || true
    if [[ -n "$RECORDED_PGID" ]] && kill -0 -- "-$RECORDED_PGID" 2>/dev/null; then
        PGID="$RECORDED_PGID"
    fi
fi

# the in-flight agent invocation runs in its own group (.step_pgid, recorded by
# the loop at launch) outside the pane's -- reap it too or the agent survives the kill,
# headless and still spending
STEP_PGID=""
STEP_PGID_FILE=""
if [[ -n "$PGID_FILE" ]]; then
    STEP_PGID_FILE="${PGID_FILE%.pgid}.step_pgid"
    if [[ -f "$STEP_PGID_FILE" ]]; then
        RECORDED_STEP_PGID=$(tr -d '[:space:]' <"$STEP_PGID_FILE") || true
        if [[ -n "$RECORDED_STEP_PGID" ]] \
            && kill -0 -- "-$RECORDED_STEP_PGID" 2>/dev/null; then
            STEP_PGID="$RECORDED_STEP_PGID"
        fi
    fi
fi

# grep -qxF (exact match), not tmux -t: -t resolves targets by
# prefix/fnmatch, so a short name false-matches longer session names
SESSION_EXISTS=false
if tmux list-sessions -F '#{session_name}' 2>/dev/null \
    | grep -qxF "$TMUX_SESSION_NAME"; then
    SESSION_EXISTS=true
fi

# backend-neutral no-op: no pane, session, or live recorded group to reap
if [[ -z "$PANE_PID" && "$SESSION_EXISTS" != true && -z "$PGID" && -z "$STEP_PGID" ]]; then
    echo "No running node found: $TMUX_SESSION_NAME"
    exit 0
fi

# ------ helpers

# signal the whole process group, not just the session -- killing only the
# session orphans the agent (PPID 1, still spending budget)
_reap() {
    [[ -n "$STEP_PGID" ]] && kill "-$1" -- "-$STEP_PGID" 2>/dev/null || true
    [[ -n "$PGID" ]] && kill "-$1" -- "-$PGID" 2>/dev/null || true
    [[ -n "$PANE_PID" ]] && kill "-$1" "$PANE_PID" 2>/dev/null || true
}

_alive() {
    { [[ -n "$STEP_PGID" ]] && kill -0 -- "-$STEP_PGID" 2>/dev/null; } \
        || { [[ -n "$PGID" ]] && kill -0 -- "-$PGID" 2>/dev/null; } \
        || { [[ -n "$PANE_PID" ]] && kill -0 "$PANE_PID" 2>/dev/null; }
}

# ------ main teardown

# SIGTERM, brief poll, then SIGKILL stragglers; destroy session
_reap TERM

WAITED=0
while _alive && ((WAITED < 5)); do
    sleep 1
    ((WAITED++)) || true
done

if _alive; then
    _reap KILL
fi

# '=' pins the target to the exact name -- a bare -t falls back to
# prefix/fnmatch resolution and could retarget another live session
tmux kill-session -t "=$TMUX_SESSION_NAME" 2>/dev/null || true
# drop the recorded pgid handles -- the groups are dead either way
[[ -n "$PGID_FILE" ]] && rm -f "$PGID_FILE" 2>/dev/null || true
[[ -n "$STEP_PGID_FILE" ]] && rm -f "$STEP_PGID_FILE" 2>/dev/null || true
echo "Killed node: $TMUX_SESSION_NAME"
