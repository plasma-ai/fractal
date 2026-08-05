#!/usr/bin/env bash
set -euo pipefail

# Launch a node in a tmux session (propagates environment, execs the loop on re-entry)
# ------------------------------------------------------------------------------------

usage() {
    cat <<USAGE
Usage: start.sh <path> [options]

Launch a node in a tmux session.

Options:
    --continue    Continue a stopped/exited node (clean worktree, further iterations;
                  a budget-ended run relaunches only with an explicit new --max-cost)
    --resume      Resume a paused node (adopt its open run where the pause left it)
    --drain       With --continue: forbid spawns and re-arms for the run (a wind-down)
    --help|-h     Show this help message

Run parameters come from the node's config.json (set at init, editable before start).
USAGE
    exit 0
}

# only --help is parsed here -- the full argv (path at $1) is forwarded
# verbatim to the tmux re-entry exec, so flags like --continue/--resume
# pass through to the loop
for arg in "$@"; do
    case "$arg" in
        --help | -h) usage ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

if [[ -z "${1:-}" ]]; then
    echo "Error: path is required" >&2
    exit 1
fi
WORKTREE_DIR="$1"
if [[ ! "$WORKTREE_DIR" = /* ]]; then
    WORKTREE_DIR="$(cd "$WORKTREE_DIR" && pwd)"
fi
BRANCH=$(git -C "$WORKTREE_DIR" rev-parse --abbrev-ref HEAD)

COMMON_DIR=$(git -C "$WORKTREE_DIR" rev-parse --git-common-dir)
if [[ "$COMMON_DIR" = /* ]]; then
    REPO_DIR="$(cd "$COMMON_DIR/.." && pwd)" # absolute .git/common-dir
else
    REPO_DIR="$(cd "$WORKTREE_DIR/$COMMON_DIR/.." && pwd)" # relative
fi
PROJECT=$(cat "$REPO_DIR/.worktrees/.project/$BRANCH" 2>/dev/null || echo ".")
if [[ "$PROJECT" == "." ]]; then
    NODE_DIR="$WORKTREE_DIR/.fractal/$BRANCH"
else
    NODE_DIR="$WORKTREE_DIR/$PROJECT/.fractal/$BRANCH"
fi
if [[ ! -d "$NODE_DIR" ]]; then
    echo "Error: no .fractal/$BRANCH directory found in $WORKTREE_DIR" >&2
    exit 1
fi

# re-entry: _NODE (set by the tmux command below) ensures only
# this node execs -- a child spawned from a running parent has
# a different _NODE and won't match
if [[ "${_NODE:-}" == "$NODE_DIR" ]]; then
    exec fractal node _loop --path="$WORKTREE_DIR" "${@:2}"
fi

# ------ launch in tmux

if ! command -v tmux &>/dev/null; then
    echo "Error: tmux is required (brew install tmux)" >&2
    exit 1
fi

REPO_NAME=${REPO_DIR##*/}
TMUX_SESSION_NAME="${REPO_NAME//[.:]/-} (${BRANCH//./-})"

# propagate env into the tmux session via env(1) argv -- a plain command
# every default-shell parses alike, where a 'VAR=value cmd' prefix is
# POSIX-only grammar (tmux hands the single shell-command string to the
# user's default-shell, which need not be POSIX); _NODE also drives the
# re-entry exec above
ENV_ARGS=("_NODE=$NODE_DIR")
VENV_DIR="$REPO_DIR/.venv"
if [[ -d "$VENV_DIR" ]]; then
    ENV_ARGS+=("VIRTUAL_ENV=$VENV_DIR")
    ENV_ARGS+=("PATH=$VENV_DIR/bin:$PATH")
else
    ENV_ARGS+=("PATH=$PATH")
    [[ -n "${VIRTUAL_ENV:-}" ]] && ENV_ARGS+=("VIRTUAL_ENV=$VIRTUAL_ENV")
fi

TMUX_CMD="env"
for word in "${ENV_ARGS[@]}" bash "$SCRIPT_DIR/start.sh" "$@"; do
    TMUX_CMD="$TMUX_CMD $(printf '%q' "$word")"
done

# provider route keys must reach the loop without landing in ps(1) argv.
# A cold new-session BECOMES the tmux server and keeps any -e KEY=VALUE
# in the server's argv for its whole lifetime; a warm new-session is a
# transient client whose argv is gone in milliseconds. So forward with
# -e only when a server already runs -- with none, the exported keys are
# inherited into the new server's (argv-invisible) global environment.
# An unset key is never forwarded, so grok OAuth and opencode
# auto-detection still see it absent rather than empty. The probe races
# the launch: a server transition in between either cold-starts with the
# keys in argv or warm-starts without them -- accepted, the window is
# milliseconds wide. new-session -e needs tmux >= 3.2, so older tmux
# skips the forwarding and the loop's key preflight reports the missing
# key instead of tmux erroring on the flag.
TMUX_ARGS=(-d -s "$TMUX_SESSION_NAME")
TMUX_VERSION=$(tmux -V)
TMUX_VERSION=${TMUX_VERSION#tmux }
if tmux list-sessions >/dev/null 2>&1 \
    && [[ "$(printf '3.2\n%s\n' "$TMUX_VERSION" | sort -V | head -n1)" == "3.2" ]]; then
    [[ -n "${OPENROUTER_API_KEY:-}" ]] \
        && TMUX_ARGS+=(-e "OPENROUTER_API_KEY=$OPENROUTER_API_KEY")
    [[ -n "${XAI_API_KEY:-}" ]] \
        && TMUX_ARGS+=(-e "XAI_API_KEY=$XAI_API_KEY")
fi

# grep -qxF (exact match), not tmux -t: -t resolves targets by
# prefix/fnmatch, so a short name false-matches longer session names
if tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -qxF "$TMUX_SESSION_NAME"; then
    echo "Error: tmux session already exists: $TMUX_SESSION_NAME" >&2
    # the name may belong to another fractal sharing this repo basename --
    # do not blindly kill it; stop that node from its own repo, or rename
    # one repository directory so the sessions no longer collide
    echo "Stop that node from its own repository, or rename one repo directory." >&2
    exit 1
fi
tmux new-session "${TMUX_ARGS[@]}" "$TMUX_CMD"
echo "Started tmux session: $TMUX_SESSION_NAME"
