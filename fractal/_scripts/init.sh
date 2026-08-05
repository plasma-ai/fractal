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
PROJECT=""
SCOPE=""
BASE=""
META=""
INHERIT=""
STEPS=""
CHARTER=""
AGENT=""
PROVIDER=""
MODEL=""
EFFORT=""
MAX_ITERS=""
MAX_DEPTH=""
MAX_CHILDREN=""
MAX_DESCENDANTS=""
TIMEOUT=""
ITER_TIMEOUT=""
STEP_TIMEOUT=""
STEP_RETRIES=""
STEP_RETRY_BACKOFF=""
INTERVAL=""
SLEEP=""
WAIT=""
MAX_COST=""
MAX_ITER_COST=""
MAX_STEP_COST=""
RESERVE_BUDGET=""
SYNC=""
DETACHED=""
LOCAL=false
BLIND=false
SEALED=false
RESET=false

usage() {
    cat <<USAGE
Usage: init.sh <name> <repo> [options]

Initialize an autonomous node.

Options:
    --title=<name>                     Human-readable display name (default: de-slugged name)
    --parent=<branch>                  Parent node branch (resolved by the caller)
    --root=<branch>                    Tree root branch (resolved by the caller)
    --project=<relpath>                Sub-project within the repo
                                       (default: inherit the parent's)
    --scope=<subdirs>                  Subdirectory scope within the worktree
                                       (comma-separated; repeatable)
    --base=<branch>                    Branch to start from
    --meta=<branch>                    Meta-configure the target node (stores its branch)
    --inherit=<surfaces>               Seed surfaces from the parent node instead of the
                                       package seed (comma-separated; repeatable): steps,
                                       scripts, skills, config, all
    --steps=<dir>                      Seed steps/ from the NN- prefixed step files
                                       (*.md) in <dir> instead of the package seed
    --charter=<file>                   Seed NODE.md from a profile charter instead of
                                       the package seed's placeholder
    --agent=<agent>                    Agent type
                                       (currently claude, codex, grok, opencode, or omp)
    --provider=<provider>              Provider route for the agent (e.g. openrouter)
    --model=<model>                    Model override (passed to agent CLI via --model)
    --effort=<effort>                  Reasoning-effort override (passed to agent CLI)
    --max-iters=<n>                    Per-run iteration cap (default: unlimited)
    --max-depth=<n>                    Maximum child node nesting depth (default: unlimited)
    --max-children=<n>                 Maximum number of child nodes (default: unlimited)
    --max-descendants=<n>              Maximum total descendants in this subtree
                                       (default: unlimited)
    --timeout=<duration>               Per-run time budget (e.g. 30s, 10m, 1.5h)
    --iter-timeout=<duration>          Per-iteration time budget (e.g. 30s, 10m, 1.5h)
    --step-timeout=<duration>          Per-step time budget (e.g. 30s, 10m)
    --step-retries=<n>                 Retries per failed step (default: 1; 0 disables)
    --step-retry-backoff=<duration>    Delay before each step retry (default: 10s)
    --interval=<duration>              Run iterations on a fixed schedule (e.g. 30m, 1h)
    --sleep=<duration>                 Delay between iterations (e.g. 30s, 5m)
    --wait=<duration>                  Sleep between approval-wait sync invocations
                                       (default: 1m)
    --max-cost=<usd>                   Maximum cost in USD per run (runs are isolated; a
                                       budget-ended run continues only with an explicit
                                       new --max-cost)
    --max-iter-cost=<usd>              Maximum cost per iteration in USD
    --max-step-cost=<usd>              Cost per step in USD (warn-only when unenforceable)
    --reserve-budget=<usd>             Budget reserved for cleanup (shifts reserve mode)
    --sync                             Enable sync mode before each step (default)
    --no-sync                          Disable sync mode before each step
    --detached                         Run each step as a separate invocation
    --no-detached                      Run steps in one continuous session (default)
    --local                            Skip pushing to remote after each commit
    --blind                            Subscribe to no channels
                                       (the parent still reads this node)
    --sealed                           Seal the node's mailbox (verifier isolation)
    --reset                            Delete node files and reinitialize
    --help|-h                          Show this help message
USAGE
    exit 0
}

for arg in "$@"; do
    case "$arg" in
        --help | -h) usage ;;
        --title=*) TITLE="${arg#*=}" ;;
        --parent=*) PARENT="${arg#*=}" ;;
        --root=*) ROOT="${arg#*=}" ;;
        --project=*) PROJECT="${arg#*=}" ;;
        --base=*) BASE="${arg#*=}" ;;
        --scope=*) SCOPE="${SCOPE:+$SCOPE,}${arg#*=}" ;;
        --agent=*) AGENT="${arg#*=}" ;;
        --provider=*) PROVIDER="${arg#*=}" ;;
        --model=*) MODEL="${arg#*=}" ;;
        --effort=*) EFFORT="${arg#*=}" ;;
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
        --step-retries=*)
            STEP_RETRIES="${arg#*=}"
            if [[ ! "$STEP_RETRIES" =~ ^[0-9]+$ ]]; then
                echo "Error: --step-retries requires a non-negative integer" >&2
                exit 1
            fi
            ;;
        --step-retry-backoff=*)
            STEP_RETRY_BACKOFF="${arg#*=}"
            if [[ ! "$STEP_RETRY_BACKOFF" =~ ^[0-9]*\.?[0-9]+(s|m|h|d)$ ]]; then
                echo "Error: --step-retry-backoff requires a duration with suffix" \
                    "(e.g. 10s, 1m)" >&2
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
        --inherit=*) INHERIT="${INHERIT:+$INHERIT,}${arg#*=}" ;;
        --steps=*) STEPS="${arg#*=}" ;;
        --charter=*) CHARTER="${arg#*=}" ;;
        --sync) SYNC=true ;;
        --no-sync) SYNC=false ;;
        --detached) DETACHED=true ;;
        --no-detached) DETACHED=false ;;
        --local) LOCAL=true ;;
        --blind) BLIND=true ;;
        --sealed) SEALED=true ;;
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
if [[ ! "$REPO_DIR" = /* ]]; then
    REPO_DIR="$(cd "$REPO_DIR" && pwd)"
fi
if [[ -z "$PARENT" ]]; then
    echo "Error: --parent is required" >&2
    exit 1
fi
if [[ -z "$ROOT" ]]; then
    echo "Error: --root is required" >&2
    exit 1
fi

# reject a sub-1s duration: it passes the per-flag format check above but
# the loop's duration validation rejects < 1s at launch, so catch it here at the init
# boundary rather than letting a bad value abort only when the loop starts
reject_subsecond() {
    local VALUE="$1"
    local LABEL="$2"
    [[ -z "$VALUE" ]] && return 0
    local SECS
    # LC_ALL=C: awk's string-to-number conversion follows the process locale,
    # so a comma-decimal locale would truncate a dot-decimal "0.5" to 0
    SECS=$(LC_ALL=C awk -v d="$VALUE" 'BEGIN {
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
reject_subsecond "$STEP_RETRY_BACKOFF" "--step-retry-backoff"
reject_subsecond "$INTERVAL" "--interval"
reject_subsecond "$SLEEP" "--sleep"
reject_subsecond "$WAIT" "--wait"

# ------ resolve source directories

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
NODE_SEED_DIR="$(cd "$SCRIPT_DIR/../_node" && pwd -P)"

# ------ validate repo root

# the caller (Node.init) resolves and passes the git root; reject a path inside
# fractal's own worktrees dir (a .worktrees ancestor whose parent is itself a
# git repo -- a worktree, not the repo root) so a node never anchors there; a
# repo that merely lives under an unrelated .worktrees path is fine
ANCESTOR="$REPO_DIR"
while [[ "$ANCESTOR" == */* && -n "${ANCESTOR%/*}" ]]; do
    if [[ "${ANCESTOR##*/}" == .worktrees && -e "${ANCESTOR%/*}/.git" ]]; then
        echo "Error: $REPO_DIR is inside ${ANCESTOR%/*}'s .worktrees/; pass the repo root instead" >&2
        exit 1
    fi
    ANCESTOR="${ANCESTOR%/*}"
done

# ------ parent branch

# the caller (Node.init) resolves the parent and passes --parent,
# so init.sh never re-derives it from the environment
PARENT_BRANCH="$PARENT"

# ------ branch and worktree

# match the worktree line with substr (not $2) so a path containing
# spaces is preserved -- mirrors merge.sh and Python worktree_map
PARENT_WORKTREE_DIR=$(git -C "$REPO_DIR" worktree list --porcelain \
    | awk -v branch="refs/heads/$PARENT_BRANCH" \
        'index($0,"worktree ")==1{wt=substr($0,10)} $1=="branch" && $2==branch{print wt}')
if [[ -z "$PARENT_WORKTREE_DIR" ]]; then
    PARENT_WORKTREE_DIR="$REPO_DIR"
fi
PARENT_PROJECT=$(cat "$REPO_DIR/.worktrees/.project/$PARENT_BRANCH" \
    2>/dev/null || echo ".")
# inherit parent's project path unless --project selects a sub-project
PROJECT_PATH="${PROJECT:-$PARENT_PROJECT}"
if [[ "$PARENT_PROJECT" == "." ]]; then
    PARENT_NODE_DIR="$PARENT_WORKTREE_DIR/.fractal/$PARENT_BRANCH"
else
    PARENT_NODE_DIR="$PARENT_WORKTREE_DIR/$PARENT_PROJECT/.fractal/$PARENT_BRANCH"
fi

if [[ ! -d "$PARENT_NODE_DIR" ]]; then
    echo "Error: no fractal node at '$PARENT_BRANCH'; run 'fractal init' first" >&2
    exit 1
fi

# ------ inheritance sources

# derive the file-surface flags; 'config' is inherited by the caller
# (Node.init fills the child's flags from the parent's config), so the
# script consumes only the file surfaces
INHERIT_STEPS=false
INHERIT_SCRIPTS=false
INHERIT_SKILLS=false
# disable globbing around the unquoted comma split -- a surface with glob
# metacharacters must reach the validation literally, not cwd-expanded
set -f
for SURFACE in ${INHERIT//,/ }; do
    case "$SURFACE" in
        steps) INHERIT_STEPS=true ;;
        scripts) INHERIT_SCRIPTS=true ;;
        skills) INHERIT_SKILLS=true ;;
        all)
            INHERIT_STEPS=true
            INHERIT_SCRIPTS=true
            INHERIT_SKILLS=true
            ;;
        config) ;;
        *)
            echo "Error: unknown --inherit surface: $SURFACE" \
                "(valid: steps, scripts, skills, config, all)" >&2
            exit 1
            ;;
    esac
done
set +f
# an inherited surface requires the parent to carry it -- the user/root
# node seeds no steps, scripts, or skills, so fail loudly (before any
# worktree is created) rather than falling back to the package seed silently
if [[ "$INHERIT_STEPS" == true ]] \
    && ! compgen -G "$PARENT_NODE_DIR/steps/*.md" >/dev/null; then
    echo "Error: --inherit=steps: parent has no step files at $PARENT_NODE_DIR/steps/" >&2
    exit 1
fi
if [[ "$INHERIT_SCRIPTS" == true && ! -d "$PARENT_NODE_DIR/scripts" ]]; then
    echo "Error: --inherit=scripts: parent has no scripts dir at $PARENT_NODE_DIR/scripts/" >&2
    exit 1
fi
if [[ "$INHERIT_SKILLS" == true && ! -d "$PARENT_NODE_DIR/skills" ]]; then
    echo "Error: --inherit=skills: parent has no skills dir at $PARENT_NODE_DIR/skills/" >&2
    exit 1
fi
# an explicit steps dir must carry step files -- fail loudly (before any
# worktree is created) rather than seeding an empty steps/; globbed with the
# dir quoted so a metacharacter in its name stays literal (compgen -G would
# pattern-expand it)
if [[ -n "$STEPS" ]]; then
    STEPS_MATCH=("$STEPS/"*.md)
    if [[ ! -e "${STEPS_MATCH[0]}" ]]; then
        echo "Error: --steps: no step files at $STEPS/" >&2
        exit 1
    fi
fi

# branch = <parent>.<name>
BRANCH="$PARENT_BRANCH.$NAME"

WORKTREES_DIR="$REPO_DIR/.worktrees"

# ------ validate project wiki precondition

# base ref must have a committed wiki; top-level nodes also require a clean tree
BASE_REF="${BASE:-$PARENT_BRANCH}"
# the ref must exist before the wiki check reads through it -- cat-file fails
# identically for a missing ref and a missing wiki, and a --base typo must
# name the real problem, not prescribe creating a wiki
if ! git -C "$REPO_DIR" rev-parse --verify --quiet "$BASE_REF^{commit}" >/dev/null; then
    echo "Error: base branch '$BASE_REF' does not exist" >&2
    exit 1
fi
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
    echo "Commit them before initializing a top-level node" >&2
    exit 1
fi

# enforce one fractal per branch -- a reused branch keeps its recorded project
if [[ -f "$WORKTREES_DIR/.project/$BRANCH" ]]; then
    EXISTING_PROJECT=$(cat "$WORKTREES_DIR/.project/$BRANCH")
    if [[ "$EXISTING_PROJECT" != "$PROJECT_PATH" ]]; then
        echo "Error: branch '$BRANCH' already maps to project '$EXISTING_PROJECT';" \
            "one branch maps to a single project — use a separate branch" >&2
        exit 1
    fi
fi

# ------ create worktree

mkdir -p "$WORKTREES_DIR"

WORKTREE_DIR="$WORKTREES_DIR/$BRANCH"
if [[ -d "$WORKTREE_DIR" ]]; then
    echo "Reusing existing worktree at $WORKTREE_DIR"
    if [[ "$RESET" != true ]]; then
        echo "Warning: $BRANCH already initialized; pass --reset to refresh" \
            "seed files and config" >&2
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
# record the incarnation's fork point for the init event below: the fork sha
# on branch creation, and the reused tip on a re-init (--reset, or a re-added
# worktree over an existing branch) -- history rows persist across both, so
# an unstamped re-init would let the base anchor fall back to the original
# fork and reach past the re-init into the dead incarnation's contribution;
# nothing commits between here and the event, so HEAD is the sha this
# incarnation diffs against
FORK_SHA="$(git -C "$WORKTREE_DIR" rev-parse HEAD)"

# set per-node commit identity: a worktree-scoped user.name (the dotted node
# name) attributes every commit the node makes to the node itself, while the
# unset email inherits the user's own; set on reuse too, so a pre-existing
# worktree picks the identity up on its next re-init
git -C "$REPO_DIR" config extensions.worktreeConfig true
git -C "$WORKTREE_DIR" config --worktree user.name "$BRANCH"

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
# a profile charter seeds a deployment-ready NODE.md; the package seed's
# placeholder charter otherwise
if [[ ! -f "$NODE_DIR/NODE.md" ]]; then
    if [[ -n "$CHARTER" ]]; then
        cp "$CHARTER" "$NODE_DIR/NODE.md"
    else
        cp "$NODE_SEED_DIR/NODE.md" "$NODE_DIR/NODE.md"
    fi
    echo "Created $NODE_DIR/NODE.md"
fi

if [[ "$RESET" == true ]]; then
    rm -rf "$NODE_DIR/steps"
fi
# seed from an explicit --steps dir when given, else the parent's live steps
# when requested, else the package seed
if [[ -n "$STEPS" ]]; then
    STEPS_SRC="$STEPS"
elif [[ "$INHERIT_STEPS" == true ]]; then
    STEPS_SRC="$PARENT_NODE_DIR/steps"
else
    STEPS_SRC="$NODE_SEED_DIR/steps"
fi
mkdir -p "$NODE_DIR/steps"
for FILE in "$STEPS_SRC/"*.md; do
    [[ -f "$FILE" ]] || continue
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

# the sanctioned git-ignored scratch dir (the info/exclude block covers it)
if [[ "$RESET" == true ]]; then
    rm -rf "$NODE_DIR/tmp"
fi
mkdir -p "$NODE_DIR/tmp"

if [[ "$RESET" == true ]]; then
    rm -rf "$MEMORY_DIR"
fi

if [[ "$RESET" == true ]]; then
    rm -rf "$NODE_DIR/scripts"
fi
# seed only the mutable, per-node scripts (setup/test/lint);
# skip the underscore-prefixed machinery
# inherit the parent's live scripts when requested, else the package seed
if [[ "$INHERIT_SCRIPTS" == true ]]; then
    SCRIPTS_SRC="$PARENT_NODE_DIR/scripts"
else
    SCRIPTS_SRC="$NODE_SEED_DIR/scripts"
fi
if [[ -d "$SCRIPTS_SRC" ]]; then
    mkdir -p "$NODE_DIR/scripts"
    for SRC in "$SCRIPTS_SRC"/*; do
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
# inherit the parent's live skill set when requested, else the package seed
# -- the copy is wholesale (a skill absent at the source is never copied; no
# union across sources). An existing child skill dir is never touched, so a
# parent edit reaches an existing child only through --reset
if [[ "$INHERIT_SKILLS" == true ]]; then
    SKILLS_SRC="$PARENT_NODE_DIR/skills"
else
    SKILLS_SRC="$NODE_SEED_DIR/skills"
fi
if [[ -d "$SKILLS_SRC" ]]; then
    mkdir -p "$NODE_DIR/skills"
    for SKILL_SRC in "$SKILLS_SRC"/*/; do
        [[ -d "$SKILL_SRC" ]] || continue
        SKILL_NAME=$(basename "$SKILL_SRC")
        if [[ ! -d "$NODE_DIR/skills/$SKILL_NAME" ]]; then
            cp -RL "$SKILL_SRC" "$NODE_DIR/skills/$SKILL_NAME"
            echo "Created $NODE_DIR/skills/$SKILL_NAME/"
        fi
    done
fi

# set up each agent dir -- seed its config and symlink in skills, recreated
# each init and gitignored; a node's base agent may be overridden per step
# (agent: frontmatter), so any node may run any agent; the seeding (config
# precedence, skills links, provider auth links) lives with each backend in
# fractal.core.agent / fractal.impl
SEED_ARGS=("$NODE_DIR" "--parent=$PARENT_NODE_DIR")
if [[ "$RESET" == true ]]; then
    SEED_ARGS+=("--reset")
fi
fractal node _seed "${SEED_ARGS[@]}"

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
        provider="${PROVIDER:-null}" \
        model="${MODEL:-null}" \
        effort="${EFFORT:-null}" \
        max_iters="${MAX_ITERS:-null}" \
        max_depth="${MAX_DEPTH:-null}" \
        max_children="${MAX_CHILDREN:-null}" \
        max_descendants="${MAX_DESCENDANTS:-null}" \
        timeout="${TIMEOUT:-null}" \
        iter_timeout="${ITER_TIMEOUT:-null}" \
        step_timeout="${STEP_TIMEOUT:-null}" \
        step_retries="${STEP_RETRIES:-null}" \
        step_retry_backoff="${STEP_RETRY_BACKOFF:-null}" \
        interval="${INTERVAL:-null}" \
        sleep="${SLEEP:-null}" \
        wait="${WAIT:-null}" \
        max_cost="${MAX_COST:-null}" \
        max_iter_cost="${MAX_ITER_COST:-null}" \
        max_step_cost="${MAX_STEP_COST:-null}" \
        reserve_budget="${RESERVE_BUDGET:-null}" \
        sync="${SYNC:-null}" \
        detached="${DETACHED:-null}" \
        local="$LOCAL" \
        blind="$BLIND" \
        sealed="$SEALED" \
        --path="$WORKTREE_DIR"
    echo idle >"$NODE_DIR/.status"
fi

# the fork sha rides the init event's metadata -- the node's base diff
# anchor, resolved newest-first so a re-init of a deleted branch name reads
# its own fork, never a dead namesake's
EVENT_ID=$(fractal event _start init --metadata="$FORK_SHA" \
    --path="$WORKTREE_DIR" 2>/dev/null || true)
[[ "$EVENT_ID" =~ ^[0-9]+$ ]] || EVENT_ID=""
if [[ -n "$EVENT_ID" ]]; then
    fractal event _end "$EVENT_ID" --path="$WORKTREE_DIR" \
        --status=completed 2>/dev/null || true
else
    echo "Warning: init event for $BRANCH was not recorded" >&2
fi

if [[ ! -f "$MEMORY_DIR/_index.md" ]]; then
    # --quiet: drop wiki's own "Initialized wiki at ..." line so the
    # created-memory message below is the single user-facing line
    wiki init --quiet --path="$MEMORY_DIR" \
        --settings='{"naming": {"validate": ["ascii", "identifier"]}}'
    echo "Created memory wiki at $MEMORY_DIR"
fi

echo ""
echo "Initialized $WORKTREE_DIR"

# surface the next steps: the task contract location and the start command
echo ""
echo "Next: author the node's task in $NODE_DIR/NODE.md"
echo "(its Instructions and Completion Requirements sections start blank),"
echo "then start the loop: fractal node start $BRANCH"
