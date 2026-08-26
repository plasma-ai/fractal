#!/usr/bin/env bash
set -euo pipefail

# Scaffold a hello-world fractal node in a scratch repo
# -----------------------------------------------------
# Walks the full setup end to end in a scratch git repo, up to a
# ready-to-start capped `hello` node. Everything before `fractal node start`
# runs for real; the start/watch/merge commands are printed instead of run,
# because starting the loop invokes the configured agent and spends money.

# ------ argument parsing

SCRATCH_DIR=""
AGENT="claude"

usage() {
    cat <<USAGE
Usage: hello.sh [options]

Build a ready-to-start fractal node in a scratch repository.

Options:
    --dir=<path>       Directory for the scratch repo (default: a fresh
                       temp directory; must be empty or absent)
    --agent=<agent>    Default agent command for spawned nodes
                       (default: claude)
    --help|-h          Show this help message
USAGE
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir | --dir=*)
            if [[ "$1" == *=* ]]; then
                SCRATCH_DIR="${1#*=}"
                shift
            else
                [[ -z "${2:-}" ]] && {
                    echo "Error: --dir requires an argument" >&2
                    usage 1
                }
                SCRATCH_DIR="$2"
                shift 2
            fi
            ;;
        --agent | --agent=*)
            if [[ "$1" == *=* ]]; then
                AGENT="${1#*=}"
                shift
            else
                [[ -z "${2:-}" ]] && {
                    echo "Error: --agent requires an argument" >&2
                    usage 1
                }
                AGENT="$2"
                shift 2
            fi
            ;;
        --help | -h)
            usage
            ;;
        *)
            echo "Error: unknown argument: $1" >&2
            usage 1
            ;;
    esac
done

# ------ preconditions

if ! command -v fractal &>/dev/null; then
    echo "Error: fractal is not on PATH" >&2
    echo "Install it first with: pip install plasma-fractal" \
        "(see the README's Installation section)" >&2
    exit 1
fi
if [[ -z "$SCRATCH_DIR" ]]; then
    # underscores and no dots: `fractal init` derives the project name
    # from the directory name and rejects characters it cannot map
    SCRATCH_DIR="$(mktemp -d "${TMPDIR:-/tmp}/fractal_hello_XXXXXX")"
elif [[ -e "$SCRATCH_DIR" && -n "$(ls -A "$SCRATCH_DIR" 2>/dev/null)" ]]; then
    echo "Error: $SCRATCH_DIR exists and is not empty" >&2
    exit 1
fi

# ------ scratch repo

# a normal project fractal can attach to
echo "==> creating scratch repo at $SCRATCH_DIR"
mkdir -p "$SCRATCH_DIR"
cd "$SCRATCH_DIR"
# --quiet: drop git's "Initialized empty Git repository" line so the
# phase message above stays the single user-facing line
git init --quiet --initial-branch=main
# scratch-repo-local identity so the walkthrough runs on machines
# without a global git config; your real repo uses your own
git config user.name "Fractal Hello"
git config user.email "hello@example.invalid"
echo "A scratch project for the fractal hello-node walkthrough." >project.md
git add project.md
# --quiet: drop git's commit summary so the phase message above stays
# the single user-facing line
git commit --quiet -m "seed scratch project"

# ------ fractal init

# one-time repo setup
echo "==> fractal init --agent=$AGENT"
fractal init --agent="$AGENT"

# the wiki scaffold must be committed on the base branch before nodes
# can be created from it
echo "==> committing the wiki scaffold"
git add wiki .gitattributes
# --quiet: drop git's commit summary so the phase message above stays
# the single user-facing line
git commit --quiet -m "add project wiki"

# ------ hello node

# create a capped node: never create an uncapped one -- defaults are
# unlimited, and one iteration is several agent invocations
echo "==> fractal node init hello --max-iters=2 --max-cost=5.0"
fractal node init hello --max-iters=2 --max-cost=5.0

# ------ mission

# author the node's mission: replace the two placeholder comments in
# the generated NODE.md (normally you would open it in an editor)
NODE_MD=".worktrees/main.hello/.fractal/main.hello/NODE.md"
MISSION='Write HELLO.md at the worktree root: a two-line greeting from the hello node, plus a count of the files in the repo.'
# shellcheck disable=SC2016  # literal markdown backticks, no expansion
DONE_WHEN='HELLO.md exists at the worktree root with both lines. Then run `fractal node finish --reason="hello done"`.'
echo "==> authoring the mission in $NODE_MD"
awk -v mission="$MISSION" -v done_when="$DONE_WHEN" '
    /Author the node.s goals and directions here/ { print mission; next }
    /Author the node.s completion conditions here/ { print done_when; next }
    { print }
' "$NODE_MD" >"$NODE_MD.tmp"
mv "$NODE_MD.tmp" "$NODE_MD"
if ! grep -qF "$MISSION" "$NODE_MD"; then
    echo "Error: mission injection failed -- placeholder not found in $NODE_MD" >&2
    exit 1
fi

# show the authored contract
echo
echo "The node's contract (from $NODE_MD):"
sed -n '/^## Instructions/,/^## Rules/p' "$NODE_MD" | sed '$d'

# ------ handoff

# starting the loop spends real money, so print, don't run
cat <<NEXT
The hello node is ready. To run it (invokes '$AGENT' in tmux and
spends real money -- the node is capped at 2 iterations / \$5.00):

    cd $SCRATCH_DIR
    fractal node start hello     # start the loop in tmux
    fractal node status hello    # lifecycle status
    fractal node activity hello  # what it is doing right now

When status reads 'completed':

    fractal node merge hello     # merge the node's branch back

Scratch repo (delete it when done): $SCRATCH_DIR
NEXT
