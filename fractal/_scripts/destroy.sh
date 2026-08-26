#!/usr/bin/env bash
set -euo pipefail

# Destroy one fractal tree, or the repo's whole fractal with --all
# ----------------------------------------------------------------

usage() {
    cat <<USAGE
Usage: destroy.sh <repo> [options]

Destroy one fractal tree -- its worktrees, branches, and user node --
or, with --all, the repo's whole fractal.

Options:
    --branch=<branch>    Tree root branch (default: the current checkout)
    --all                Destroy every tree and remove .worktrees/
    --node-dir=<dir>     Tree data dir to remove, repo-relative (repeatable)
    --help|-h            Show this help message
USAGE
    exit 0
}

REPO=""
BRANCH=""
ALL=false
# the data dirs an --all sweep clears, repo-relative: which dirs are user
# roots is a config question, so the caller answers it and names them here
ROOT_DIRS=()

for arg in "$@"; do
    case "$arg" in
        --help | -h) usage ;;
        --branch=*) BRANCH="${arg#*=}" ;;
        --all) ALL=true ;;
        --node-dir=*) ROOT_DIRS+=("${arg#*=}") ;;
        *)
            if [[ -z "$REPO" ]]; then
                REPO="$arg"
            fi
            ;;
    esac
done

if [[ -z "$REPO" ]]; then
    echo "Error: repo is required" >&2
    exit 1
fi

# --node-dir carries data dirs for the --all sweep only -- a tree-scoped
# run must never honor it
if [[ "$ALL" != true && ${#ROOT_DIRS[@]} -gt 0 ]]; then
    echo "Error: --node-dir requires --all" >&2
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

# ------ derive the user node's data directory
# the user branch's node dir nests under the .worktrees/.project/<branch>
# project prefix (mirrors Node.node_dir); read the cache BEFORE the teardown
# below removes it. the caller names the user branch (the checkout may sit
# on another one); a standalone run falls back to the current branch
if [[ -z "$BRANCH" ]]; then
    BRANCH=$(git -C "$REPO" rev-parse --abbrev-ref HEAD)
fi
PROJECT="."
PROJECT_FILE="$WORKTREES_DIR/.project/$BRANCH"
if [[ -f "$PROJECT_FILE" ]]; then
    PROJECT=$(cat "$PROJECT_FILE")
fi
if [[ "$PROJECT" == "." ]]; then
    NODE_DIR="$REPO/.fractal/$BRANCH"
    WIKI_REL="wiki"
else
    NODE_DIR="$REPO/$PROJECT/.fractal/$BRANCH"
    WIKI_REL="$PROJECT/wiki"
fi

# find active worktrees -- worktree dirs are named by branch, so the
# <branch>.* scope matches only the tree's own nodes and leaves sibling
# trees standing
WORKTREES=()
if [[ -d "$WORKTREES_DIR" ]]; then
    for SUBDIR in "$WORKTREES_DIR"/*/; do
        [[ ! -d "$SUBDIR" ]] && continue
        if [[ "$ALL" != true ]]; then
            NAME=$(basename "$SUBDIR")
            [[ "$NAME" == "$BRANCH".* ]] || continue
        fi
        if [[ -f "$SUBDIR/.git" ]]; then
            WORKTREES+=("$(cd "$SUBDIR" && pwd)")
        fi
    done
fi

# nothing fractal present -- a clean no-op
if [[ "$ALL" == true ]]; then
    if [[ ! -d "$WORKTREES_DIR" && ! -d "$NODE_DIR" ]]; then
        echo "No fractal found. Nothing to destroy."
        exit 0
    fi
elif [[ ${#WORKTREES[@]} -eq 0 && ! -d "$NODE_DIR" ]]; then
    echo "No tree found on branch $BRANCH. Nothing to destroy."
    exit 0
fi

# ------ refuse while any node still runs in tmux
# guard every node BEFORE removing any, so a live session never strands a
# half-destroyed tree; grep -qxF (exact match), not tmux -t: -t resolves
# targets by prefix/fnmatch, so a short name false-matches longer session names
if [[ ${#WORKTREES[@]} -gt 0 ]]; then
    for WORKTREE in "${WORKTREES[@]}"; do
        WT_BRANCH=$(git -C "$WORKTREE" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
        TMUX_SESSION_NAME="${REPO_NAME//[.:]/-} (${WT_BRANCH//./-})"
        if tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -qxF "$TMUX_SESSION_NAME"; then
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
# pause landing after the sweep, mid-destroy, would slip through
if [[ ${#WORKTREES[@]} -gt 0 ]]; then
    for WORKTREE in "${WORKTREES[@]}"; do
        WT_BRANCH=$(git -C "$WORKTREE" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
        # the node dir nests under the .worktrees/.project/<branch> project
        # prefix (mirrors Node.node_dir)
        WT_PROJECT="."
        WT_PROJECT_FILE="$WORKTREES_DIR/.project/$WT_BRANCH"
        if [[ -f "$WT_PROJECT_FILE" ]]; then
            WT_PROJECT=$(cat "$WT_PROJECT_FILE")
        fi
        if [[ "$WT_PROJECT" == "." ]]; then
            STATUS_FILE="$WORKTREE/.fractal/$WT_BRANCH/.status"
        else
            STATUS_FILE="$WORKTREE/$WT_PROJECT/.fractal/$WT_BRANCH/.status"
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
# so a lock found mid-tear would strand a half-destroyed tree)
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

        # abort if removal fails -- the rm -rf below would orphan the git
        # worktree registration, which git worktree prune can't clean
        if ! git -C "$REPO" worktree remove --force "$WORKTREE" 2>/dev/null; then
            echo "Error: failed to remove worktree: $WORKTREE" >&2
            exit 1
        fi
        # >/dev/null: drop git's own "Deleted branch ... (was <sha>)"
        # so only the script's message below shows (no duplicate line)
        git -C "$REPO" branch -D "$WT_BRANCH" >/dev/null 2>&1 || true
        echo "Deleted $WORKTREE ($WT_BRANCH)"
    done
fi

# ------ remove user node data directories
# before removing .worktrees/ -- its .project/<branch> cache is what a rerun
# reads to re-derive NODE_DIR, so tearing the cache down first would strand
# this dir (config + central DB) on a crash between the two removals while a
# rerun reports nothing-to-destroy
# an --all sweep removes every tree's data dir, not just the anchor's; the
# caller named them (guarded expansion: an empty array reads as unset under
# set -u on bash 3.2)
NODE_DIRS=("$NODE_DIR")
for ROOT_DIR in ${ROOT_DIRS[@]+"${ROOT_DIRS[@]}"}; do
    [[ "$REPO/$ROOT_DIR" == "$NODE_DIR" ]] && continue
    NODE_DIRS+=("$REPO/$ROOT_DIR")
done
HAD_NODE=false
for DIR in "${NODE_DIRS[@]}"; do
    [[ -d "$DIR" ]] || continue
    HAD_NODE=true
    rm -rf "$DIR"
    # also strip the seed from git when it was tracked (fractal track
    # committed it on the top-level branch); --cached leaves the already-removed
    # tree alone and --ignore-unmatch makes this a no-op in the default
    # git-excluded case, paralleling how merge.sh strips tracked child seeds
    # (--quiet: drop git rm's per-file "rm '...'" lines so the removal
    # message below stays the single user-facing line)
    git -C "$REPO" rm -r --cached --quiet --ignore-unmatch -- "$DIR"
    # drop the containing .fractal/ when this was its last node
    rmdir "$(dirname "$DIR")" 2>/dev/null || true
    echo "Removed user node: $DIR"
done

git -C "$REPO" worktree prune
if [[ "$ALL" == true ]]; then
    rm -rf "$WORKTREES_DIR"
else
    # tree-scoped: the shared .worktrees/ plumbing survives for sibling
    # trees; only this tree's project-cache entries go
    rm -f "$WORKTREES_DIR/.project/$BRANCH" "$WORKTREES_DIR/.project/$BRANCH".*
    # the last tree takes the plumbing with it -- the caller strips the
    # exclude block once no tree remains, so a surviving .worktrees/ would
    # surface as untracked junk; rmdir, never rm -rf, so a non-empty dir
    # (a sibling tree, an out-of-band leftover) is left alone
    if rmdir "$WORKTREES_DIR/.project" 2>/dev/null; then
        rm -f "$WORKTREES_DIR/.lock"
        rmdir "$WORKTREES_DIR" 2>/dev/null || true
    fi
fi

if [[ ${#REMOTE_BRANCHES[@]} -gt 0 ]]; then
    echo "Remote branches left on origin: ${REMOTE_BRANCHES[*]}"
fi
if [[ "$ALL" == true ]]; then
    echo "Destroyed fractal: $REPO"
else
    echo "Destroyed tree: $BRANCH"
fi
# the wiki is committed, user-edited project memory -- never deleted
if [[ "$HAD_NODE" == true && -d "$REPO/$WIKI_REL" ]]; then
    echo "Left in place: $WIKI_REL/ (committed project memory)"
fi
