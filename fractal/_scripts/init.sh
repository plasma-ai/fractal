#!/usr/bin/env bash
set -euo pipefail

# Initialize an autonomous node in a git worktree
# -----------------------------------------------

# ------ argument parsing

NAME=""
REPO_DIR=""
TITLE=""
PARENT=""
ROOT=""
SCOPE=""
BASE=""
META=""
AGENT=""
MODEL=""
MAX_ITERS=""
MAX_DEPTH=""
MAX_CHILDREN=""
MAX_DESCENDANTS=""
TIMEOUT=""
ITER_TIMEOUT=""
STEP_TIMEOUT=""
INTERVAL=""
SLEEP=""
WAIT=""
MAX_COST=""
MAX_ITER_COST=""
MAX_STEP_COST=""
RESERVE_BUDGET=""
SYNC=""
LOCAL=false
DETACHED=false
RESET=false

usage() {
    cat <<USAGE
Usage: init.sh <name> <repo> [options]

Initialize an autonomous node.

Options:
    --title=<name>               Human-readable display name (default: de-slugged name)
    --parent=<branch>            Parent node branch (resolved by the caller)
    --root=<branch>              Tree root branch (resolved by the caller)
    --scope=<relpath>            Subdirectory scope within the worktree
    --base=<branch>              Branch to start from
    --meta=<branch>              Meta-configure the target node (stores its branch)
    --agent=<agent>              Agent type (currently claude or codex)
    --model=<model>              Model override (passed to agent CLI via --model)
    --max-iters=<n>              Per-run iteration cap (default: unlimited)
    --max-depth=<n>              Maximum child node nesting depth (default: unlimited)
    --max-children=<n>           Maximum number of child nodes (default: unlimited)
    --max-descendants=<n>        Maximum total descendants in this subtree (default: unlimited)
    --timeout=<duration>         Per-run time budget (e.g. 30s, 10m, 1.5h)
    --iter-timeout=<duration>    Per-iteration time budget (e.g. 30s, 10m, 1.5h)
    --step-timeout=<duration>    Per-step time budget (e.g. 30s, 10m)
    --interval=<duration>        Run iterations on a fixed schedule (e.g. 30m, 1h)
    --sleep=<duration>           Delay between iterations (e.g. 30s, 5m)
    --wait=<duration>            Sleep between approval-wait sync invocations (default: 1s)
    --max-cost=<usd>             Maximum cost per run in USD
    --max-iter-cost=<usd>        Maximum cost per iteration in USD
    --max-step-cost=<usd>        Cost per step in USD (warn-only when unenforceable)
    --reserve-budget=<usd>       Budget reserved for cleanup (shifts reserve mode)
    --sync                       Enable sync mode before each step (default)
    --no-sync                    Disable sync mode before each step
    --local                      Skip pushing to remote after each commit
    --detached                   Run each step as a separate invocation (default: continuous)
    --reset                      Delete node files and reinitialize
    --help|-h                    Show this help message
USAGE
    exit 0
}

for arg in "$@"; do
    case "$arg" in
        --help | -h) usage ;;
        --title=*) TITLE="${arg#*=}" ;;
        --parent=*) PARENT="${arg#*=}" ;;
        --root=*) ROOT="${arg#*=}" ;;
        --base=*) BASE="${arg#*=}" ;;
        --scope=*) SCOPE="${arg#*=}" ;;
        --agent=*) AGENT="${arg#*=}" ;;
        --model=*) MODEL="${arg#*=}" ;;
        --max-iters=*)
            MAX_ITERS="${arg#*=}"
            if [[ ! "$MAX_ITERS" =~ ^[1-9][0-9]*$ ]]; then
                echo "Error: --max-iters requires a positive integer" >&2
                exit 1
            fi
            ;;
        --max-depth=*)
            MAX_DEPTH="${arg#*=}"
            if [[ ! "$MAX_DEPTH" =~ ^[0-9]+$ ]]; then
                echo "Error: --max-depth requires a non-negative integer" >&2
                exit 1
            fi
            ;;
        --max-children=*)
            MAX_CHILDREN="${arg#*=}"
            if [[ ! "$MAX_CHILDREN" =~ ^[0-9]+$ ]]; then
                echo "Error: --max-children requires a non-negative integer" >&2
                exit 1
            fi
            ;;
        --max-descendants=*)
            MAX_DESCENDANTS="${arg#*=}"
            if [[ ! "$MAX_DESCENDANTS" =~ ^[0-9]+$ ]]; then
                echo "Error: --max-descendants requires a non-negative integer" >&2
                exit 1
            fi
            ;;
        --timeout=*)
            TIMEOUT="${arg#*=}"
            if [[ ! "$TIMEOUT" =~ ^[0-9]*\.?[0-9]+(s|m|h|d)$ ]]; then
                echo "Error: --timeout requires a duration with suffix" \
                    "(e.g. 30s, 10m, 1.5h)" >&2
                exit 1
            fi
            ;;
        --iter-timeout=*)
            ITER_TIMEOUT="${arg#*=}"
            if [[ ! "$ITER_TIMEOUT" =~ ^[0-9]*\.?[0-9]+(s|m|h|d)$ ]]; then
                echo "Error: --iter-timeout requires a duration with suffix" \
                    "(e.g. 30s, 10m, 1.5h)" >&2
                exit 1
            fi
            ;;
        --step-timeout=*)
            STEP_TIMEOUT="${arg#*=}"
            if [[ ! "$STEP_TIMEOUT" =~ ^[0-9]*\.?[0-9]+(s|m|h|d)$ ]]; then
                echo "Error: --step-timeout requires a duration with suffix" \
                    "(e.g. 30s, 10m)" >&2
                exit 1
            fi
            ;;
        --interval=*)
            INTERVAL="${arg#*=}"
            if [[ ! "$INTERVAL" =~ ^[0-9]*\.?[0-9]+(s|m|h|d)$ ]]; then
                echo "Error: --interval requires a duration with suffix (e.g. 30m, 1h)" >&2
                exit 1
            fi
            ;;
        --sleep=*)
            SLEEP="${arg#*=}"
            if [[ ! "$SLEEP" =~ ^[0-9]*\.?[0-9]+(s|m|h|d)$ ]]; then
                echo "Error: --sleep requires a duration with suffix (e.g. 30s, 5m)" >&2
                exit 1
            fi
            ;;
        --wait=*)
            WAIT="${arg#*=}"
            if [[ ! "$WAIT" =~ ^[0-9]*\.?[0-9]+(s|m|h|d)$ ]]; then
                echo "Error: --wait requires a duration with suffix (e.g. 30s, 5m)" >&2
                exit 1
            fi
            ;;
        --max-cost=*)
            MAX_COST="${arg#*=}"
            if [[ ! "$MAX_COST" =~ ^[0-9]*\.?[0-9]+$ ]]; then
                echo "Error: --max-cost requires a positive number (USD)" >&2
                exit 1
            fi
            ;;
        --max-iter-cost=*)
            MAX_ITER_COST="${arg#*=}"
            if [[ ! "$MAX_ITER_COST" =~ ^[0-9]*\.?[0-9]+$ ]]; then
                echo "Error: --max-iter-cost requires a positive number (USD)" >&2
                exit 1
            fi
            ;;
        --max-step-cost=*)
            MAX_STEP_COST="${arg#*=}"
            if [[ ! "$MAX_STEP_COST" =~ ^[0-9]*\.?[0-9]+$ ]]; then
                echo "Error: --max-step-cost requires a positive number (USD)" >&2
                exit 1
            fi
            ;;
        --reserve-budget=*)
            RESERVE_BUDGET="${arg#*=}"
            if [[ ! "$RESERVE_BUDGET" =~ ^[0-9]*\.?[0-9]+$ ]]; then
                echo "Error: --reserve-budget requires a non-negative number (USD)" >&2
                exit 1
            fi
            ;;
        --meta=*) META="${arg#*=}" ;;
        --sync) SYNC=true ;;
        --no-sync) SYNC=false ;;
        --local) LOCAL=true ;;
        --detached) DETACHED=true ;;
        --reset) RESET=true ;;
        *)
            if [[ -z "$NAME" ]]; then
                NAME="$arg"
            elif [[ -z "$REPO_DIR" ]]; then
                REPO_DIR="$arg"
            else
                echo "Error: unexpected argument: $arg" >&2
                exit 1
            fi
            ;;
    esac
done

# validate required arguments
if [[ -z "$NAME" ]]; then
    echo "Error: name is required" >&2
    exit 1
fi
# '.' is the hierarchy separator; dotted names break <parent>.<child> derivation
if [[ "$NAME" == *.* ]]; then
    echo "Error: node name cannot contain '.' (reserved as the hierarchy separator): $NAME" >&2
    exit 1
fi

if [[ -z "$REPO_DIR" ]]; then
    echo "Error: path is required" >&2
    exit 1
fi
if [[ -z "$PARENT" ]]; then
    echo "Error: --parent is required" >&2
    exit 1
fi
if [[ -z "$ROOT" ]]; then
    echo "Error: --root is required" >&2
    exit 1
fi

if [[ -z "$SYNC" ]]; then
    SYNC=true
fi

# reject a sub-1s duration: it passes the per-flag format check above but
# _run.sh's parse_duration rejects < 1s at launch, so catch it here at the init
# boundary rather than letting a bad value abort only when the loop starts
reject_subsecond() {
    local VALUE="$1"
    local LABEL="$2"
    [[ -z "$VALUE" ]] && return 0
    local SECS
    SECS=$(awk -v d="$VALUE" 'BEGIN {
        u = substr(d, length(d))
        m = (u == "m") ? 60 : (u == "h") ? 3600 : (u == "d") ? 86400 : 1
        printf "%d", (d + 0) * m
    }')
    if [[ "$SECS" -lt 1 ]]; then
        echo "Error: $LABEL must be at least 1 second (got $VALUE)" >&2
        exit 1
    fi
}
reject_subsecond "$TIMEOUT" "--timeout"
reject_subsecond "$ITER_TIMEOUT" "--iter-timeout"
reject_subsecond "$STEP_TIMEOUT" "--step-timeout"
reject_subsecond "$INTERVAL" "--interval"
reject_subsecond "$SLEEP" "--sleep"
reject_subsecond "$WAIT" "--wait"

# ------ resolve source directories

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
NODE_SEED_DIR="$(cd "$SCRIPT_DIR/../_node" && pwd -P)"

# ------ validate repo root

# the caller (Node.init) resolves and passes the git root; reject a path inside
# .worktrees/ (a worktree, not the repo root) so a node never anchors there
case "$REPO_DIR" in
    */.worktrees/*)
        echo "Error: $REPO_DIR is inside .worktrees/; pass the repo root instead" >&2
        exit 1
        ;;
esac

# ------ parent branch

# the caller (Node.init) resolves the parent and passes --parent,
# so init.sh never re-derives it from the environment
PARENT_BRANCH="$PARENT"

# ------ branch and worktree

# match the worktree line with substr (not $2) so a path containing
# spaces is preserved -- mirrors merge.sh and Python _find_worktree
PARENT_WORKTREE_DIR=$(git -C "$REPO_DIR" worktree list --porcelain \
    | awk -v branch="refs/heads/$PARENT_BRANCH" \
        'index($0,"worktree ")==1{wt=substr($0,10)} $1=="branch" && $2==branch{print wt}')
if [[ -z "$PARENT_WORKTREE_DIR" ]]; then
    PARENT_WORKTREE_DIR="$REPO_DIR"
fi
PARENT_PROJECT=$(cat "$REPO_DIR/.worktrees/.project/$PARENT_BRANCH" \
    2>/dev/null || echo ".")
# inherit parent's project path
PROJECT_PATH="$PARENT_PROJECT"
if [[ "$PARENT_PROJECT" == "." ]]; then
    PARENT_NODE_DIR="$PARENT_WORKTREE_DIR/.fractal/$PARENT_BRANCH"
else
    PARENT_NODE_DIR="$PARENT_WORKTREE_DIR/$PARENT_PROJECT/.fractal/$PARENT_BRANCH"
fi

if [[ ! -d "$PARENT_NODE_DIR" ]]; then
    echo "Error: no fractal node at '$PARENT_BRANCH'; run 'fractal init' first" >&2
    exit 1
fi

# branch = <parent>.<name>
BRANCH="$PARENT_BRANCH.$NAME"

WORKTREES_DIR="$REPO_DIR/.worktrees"

# ------ validate project wiki precondition

# base ref must have a committed wiki; top-level nodes also require a clean tree
BASE_REF="${BASE:-$PARENT_BRANCH}"
if [[ "$PROJECT_PATH" == "." ]]; then
    WIKI_DIR="wiki"
else
    WIKI_DIR="$PROJECT_PATH/wiki"
fi
if ! git -C "$REPO_DIR" cat-file -e "$BASE_REF:$WIKI_DIR/_index.md" 2>/dev/null; then
    echo "Error: base branch '$BASE_REF' has no project wiki at $WIKI_DIR/_index.md" >&2
    echo "Create one first:" >&2
    echo "  (cd \"$REPO_DIR\" && wiki init --path=\"$WIKI_DIR\"" \
        "--settings='{\"naming\": {\"validate\": [\"ascii\", \"identifier\"]}}' &&" \
        "git add \"$WIKI_DIR\" && git commit -m 'add project wiki')" >&2
    exit 1
fi
# top-level nodes branch from the dotless root, which must have no uncommitted
# tracked changes (untracked .fractal/ metadata is ignored); deeper nodes may
# branch from a parent mid-iteration
if [[ "$PARENT_BRANCH" != *.* ]] \
    && [[ -n "$(git -C "$PARENT_WORKTREE_DIR" status --porcelain -uno)" ]]; then
    echo "Error: branch '$PARENT_BRANCH' has uncommitted changes in $PARENT_WORKTREE_DIR" >&2
    echo "Commit them before initializing a top-level node." >&2
    exit 1
fi

# ------ create worktree

mkdir -p "$WORKTREES_DIR"

WORKTREE_DIR="$WORKTREES_DIR/$BRANCH"
if [[ -d "$WORKTREE_DIR" ]]; then
    echo "Reusing existing worktree at $WORKTREE_DIR"
    if [[ "$RESET" != true ]]; then
        echo "Warning: $BRANCH already initialized; pass --reset to refresh" \
            "seed files and config."
    fi
else
    # clear any stale registration first: a worktree dir removed out-of-band
    # leaves a registered entry that makes `worktree add` fail with a raw error
    git -C "$REPO_DIR" worktree prune
    if git -C "$REPO_DIR" show-ref --verify --quiet "refs/heads/$BRANCH"; then
        # -q: suppress git's "Preparing worktree / HEAD is now at ..." plumbing;
        # the node-created summary below is the user-facing line
        git -C "$REPO_DIR" worktree add -q "$WORKTREE_DIR" "$BRANCH"
    else
        # branch from the resolved base ($BASE if given, else the parent tip via
        # BASE_REF) so a child inherits the spawning node's work -- never the bare
        # main-repo HEAD, which would start the child divergent from its parent
        git -C "$REPO_DIR" worktree add -q -b "$BRANCH" "$WORKTREE_DIR" "$BASE_REF"
    fi
    echo "Created worktree at $WORKTREE_DIR on branch $BRANCH"
fi

if [[ "$PROJECT_PATH" == "." ]]; then
    NODE_DIR="$WORKTREE_DIR/.fractal/$BRANCH"
else
    NODE_DIR="$WORKTREE_DIR/$PROJECT_PATH/.fractal/$BRANCH"
fi

mkdir -p "$NODE_DIR"

mkdir -p "$WORKTREES_DIR/.project"
echo "$PROJECT_PATH" >"$WORKTREES_DIR/.project/$BRANCH"

# ------ node directory

MEMORY_DIR="$NODE_DIR/memory"

if [[ "$RESET" == true ]]; then
    rm -f "$NODE_DIR/NODE.md"
fi
if [[ ! -f "$NODE_DIR/NODE.md" ]]; then
    cp "$NODE_SEED_DIR/NODE.md" "$NODE_DIR/NODE.md"
    echo "Created $NODE_DIR/NODE.md"
fi

if [[ "$RESET" == true ]]; then
    rm -rf "$NODE_DIR/steps"
fi
mkdir -p "$NODE_DIR/steps"
for FILE in "$NODE_SEED_DIR/steps/"*.md; do
    BASENAME=$(basename "$FILE")
    if [[ ! -f "$NODE_DIR/steps/$BASENAME" ]]; then
        cp "$FILE" "$NODE_DIR/steps/$BASENAME"
        echo "Created $NODE_DIR/steps/$BASENAME"
    fi
done

# reject detached in step frontmatter when the node is already detached
if [[ "$DETACHED" == true ]]; then
    for FILE in "$NODE_DIR/steps/"*.md; do
        [[ -f "$FILE" ]] || continue
        if awk '/^---$/{c++; next} c==1 && /^detached:/{f=1} c>=2{exit} END{exit !f}' \
            "$FILE"; then
            echo "Error: detached: in $(basename "$FILE") is invalid" \
                "in detached mode (already detached)" >&2
            exit 1
        fi
    done
fi

if [[ "$RESET" == true ]]; then
    rm -rf "$NODE_DIR/plans"
fi
mkdir -p "$NODE_DIR/plans"
touch "$NODE_DIR/plans/.gitkeep"

if [[ "$RESET" == true ]]; then
    rm -rf "$MEMORY_DIR"
fi

# seed only the mutable, per-node scripts (setup/test/lint);
# skip the underscore-prefixed machinery
if [[ -d "$NODE_SEED_DIR/scripts" ]]; then
    mkdir -p "$NODE_DIR/scripts"
    for SRC in "$NODE_SEED_DIR/scripts"/*; do
        [[ -f "$SRC" ]] || continue
        BASENAME=$(basename "$SRC")
        [[ "$BASENAME" == _* ]] && continue
        if [[ ! -f "$NODE_DIR/scripts/$BASENAME" ]]; then
            cp "$SRC" "$NODE_DIR/scripts/$BASENAME"
            chmod +x "$NODE_DIR/scripts/$BASENAME" 2>/dev/null || true
            echo "Created $NODE_DIR/scripts/$BASENAME"
        fi
    done
fi

if [[ "$RESET" == true ]]; then
    rm -rf "$NODE_DIR/skills"
fi
if [[ -d "$NODE_SEED_DIR/skills" ]]; then
    mkdir -p "$NODE_DIR/skills"
    for SKILL_SRC in "$NODE_SEED_DIR/skills"/*/; do
        [[ -d "$SKILL_SRC" ]] || continue
        SKILL_NAME=$(basename "$SKILL_SRC")
        if [[ ! -d "$NODE_DIR/skills/$SKILL_NAME" ]]; then
            cp -RL "$SKILL_SRC" "$NODE_DIR/skills/$SKILL_NAME"
            echo "Created $NODE_DIR/skills/$SKILL_NAME/"
        fi
    done
fi

# set up each agent dir -- seed its config and symlink in skills, recreated each
# init and gitignored; a node's base agent may be overridden per step (agent:
# frontmatter), so any node may run either agent
for AGENT_DIR in claude codex agents; do
    # the single config file each agent reads from its dir
    case "$AGENT_DIR" in
        claude) CONFIG_FILE="settings.json" ;;
        codex) CONFIG_FILE="config.toml" ;;
        *) CONFIG_FILE="" ;;
    esac
    # prefer the parent node's config so children inherit its settings; fall
    # back to the package seed for a top-level node (parent has no agent config)
    CONFIG_SRC=""
    if [[ -n "$CONFIG_FILE" ]]; then
        if [[ -f "$PARENT_NODE_DIR/.$AGENT_DIR/$CONFIG_FILE" ]]; then
            CONFIG_SRC="$PARENT_NODE_DIR/.$AGENT_DIR/$CONFIG_FILE"
        elif [[ -f "$NODE_SEED_DIR/config/$AGENT_DIR/$CONFIG_FILE" ]]; then
            CONFIG_SRC="$NODE_SEED_DIR/config/$AGENT_DIR/$CONFIG_FILE"
        fi
    fi
    if [[ "$RESET" == true ]]; then
        rm -rf "$NODE_DIR/.$AGENT_DIR"
    fi
    mkdir -p "$NODE_DIR/.$AGENT_DIR"
    if [[ -n "$CONFIG_SRC" && ! -f "$NODE_DIR/.$AGENT_DIR/$CONFIG_FILE" ]]; then
        cp "$CONFIG_SRC" "$NODE_DIR/.$AGENT_DIR/$CONFIG_FILE"
        echo "Created $NODE_DIR/.$AGENT_DIR/$CONFIG_FILE"
    fi
    # symlink skills so the agent can find them
    if [[ ! -e "$NODE_DIR/.$AGENT_DIR/skills" ]]; then
        ln -s ../skills "$NODE_DIR/.$AGENT_DIR/skills"
        echo "Created $NODE_DIR/.$AGENT_DIR/skills -> ../skills"
    fi
    # codex auth must stay global: CODEX_HOME points at this node dir, but the
    # credential is shared via a symlink to the global codex home -- codex writes
    # auth.json in-place through the link (token refresh updates the global file),
    # so the secret is never copied into the node (and .codex is gitignored)
    if [[ "$AGENT_DIR" == "codex" && ! -L "$NODE_DIR/.$AGENT_DIR/auth.json" ]]; then
        GLOBAL_CODEX_AUTH="${CODEX_HOME:-$HOME/.codex}/auth.json"
        # CODEX_HOME may be inherited from a parent node whose auth.json is itself
        # a symlink to the real ~/.codex/auth.json; canonicalize to that real file
        # so this link never dangles when an intermediate node is reset or deleted;
        # `readlink -f` is a GNU extension absent on older macOS BSD readlink, so
        # follow the link chain by hand and resolve the dir physically (pwd -P)
        if [[ -e "$GLOBAL_CODEX_AUTH" ]]; then
            while [[ -L "$GLOBAL_CODEX_AUTH" ]]; do
                LINK_TARGET="$(readlink "$GLOBAL_CODEX_AUTH")"
                case "$LINK_TARGET" in
                    /*) GLOBAL_CODEX_AUTH="$LINK_TARGET" ;;
                    *) GLOBAL_CODEX_AUTH="$(dirname "$GLOBAL_CODEX_AUTH")/$LINK_TARGET" ;;
                esac
            done
            GLOBAL_CODEX_AUTH="$(cd "$(dirname "$GLOBAL_CODEX_AUTH")" && pwd -P)/$(basename "$GLOBAL_CODEX_AUTH")"
        fi
        ln -s "$GLOBAL_CODEX_AUTH" "$NODE_DIR/.$AGENT_DIR/auth.json"
        echo "Created $NODE_DIR/.$AGENT_DIR/auth.json -> $GLOBAL_CODEX_AUTH"
    fi
    # claude auth must stay global too: CLAUDE_CONFIG_DIR points at this node dir,
    # so claude reads .credentials.json there (Linux) with no fallback to the home
    # dir -- share the global credential via a symlink so token refresh writes
    # through to the global file and the secret is never copied in (.claude is
    # gitignored). Only link when the global file exists: auth via ANTHROPIC_API_KEY
    # or the macOS Keychain has no credential file, and a dangling link would break
    # the read claude expects
    if [[ "$AGENT_DIR" == "claude" && ! -L "$NODE_DIR/.$AGENT_DIR/.credentials.json" ]]; then
        GLOBAL_CLAUDE_AUTH="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json"
        # CLAUDE_CONFIG_DIR may be inherited from a parent node whose credential is
        # itself a symlink to the real ~/.claude/.credentials.json; canonicalize it
        # so this link never dangles when an intermediate node is reset or deleted
        if [[ -e "$GLOBAL_CLAUDE_AUTH" ]]; then
            GLOBAL_CLAUDE_AUTH="$(readlink -f "$GLOBAL_CLAUDE_AUTH")"
            ln -s "$GLOBAL_CLAUDE_AUTH" "$NODE_DIR/.$AGENT_DIR/.credentials.json"
            echo "Created $NODE_DIR/.$AGENT_DIR/.credentials.json -> $GLOBAL_CLAUDE_AUTH"
        fi
    fi
done

# ------ config and database

if [[ "$RESET" == true ]]; then
    rm -f "$NODE_DIR/config.json"
fi

# write config (unset keys stored as null) and .status
if [[ "$RESET" == true ]] || [[ ! -f "$NODE_DIR/config.json" ]]; then
    fractal config _set \
        title="${TITLE:-null}" \
        project="$PROJECT_PATH" \
        root="$ROOT" \
        scope="${SCOPE:-null}" \
        base="${BASE:-null}" \
        meta="${META:-null}" \
        agent="${AGENT:-null}" \
        model="${MODEL:-null}" \
        max_iters="${MAX_ITERS:-null}" \
        max_depth="${MAX_DEPTH:-null}" \
        max_children="${MAX_CHILDREN:-null}" \
        max_descendants="${MAX_DESCENDANTS:-null}" \
        timeout="${TIMEOUT:-null}" \
        iter_timeout="${ITER_TIMEOUT:-null}" \
        step_timeout="${STEP_TIMEOUT:-null}" \
        interval="${INTERVAL:-null}" \
        sleep="${SLEEP:-null}" \
        wait="${WAIT:-null}" \
        max_cost="${MAX_COST:-null}" \
        max_iter_cost="${MAX_ITER_COST:-null}" \
        max_step_cost="${MAX_STEP_COST:-null}" \
        reserve_budget="${RESERVE_BUDGET:-null}" \
        sync="$SYNC" \
        local="$LOCAL" \
        detached="$DETACHED" \
        --path="$WORKTREE_DIR"
    echo idle >"$NODE_DIR/.status"
fi

EVENT_ID=$(fractal event _start init --path="$WORKTREE_DIR" 2>/dev/null || true)
if [[ -n "$EVENT_ID" ]]; then
    fractal event _end "$EVENT_ID" --path="$WORKTREE_DIR" \
        --status=completed 2>/dev/null || true
fi

if [[ ! -f "$MEMORY_DIR/_index.md" ]]; then
    wiki init --path="$MEMORY_DIR" \
        --settings='{"naming": {"validate": ["ascii", "identifier"]}}'
    echo "Created memory wiki at $MEMORY_DIR"
fi

echo ""
echo "Initialized $WORKTREE_DIR"
