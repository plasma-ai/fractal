#!/usr/bin/env bash
set -euo pipefail

# Remove a node's worktree and delete its branch
# ----------------------------------------------

usage() {
    cat <<USAGE
Usage: delete.sh <path> [options]

Remove a node's worktree and delete its branch.

Options:
    --merge-target=<branch>    Branch to warn unmerged commits against
                               (default: the node's base, else its dotted
                               parent)
    --help|-h                  Show this help message
USAGE
    exit 0
}

WORKTREE_DIR=""
MERGE_TARGET=""

for arg in "$@"; do
    case "$arg" in
        --help | -h) usage ;;
        --merge-target=*) MERGE_TARGET="${arg#*=}" ;;
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

# resolve identity
BRANCH=$(git -C "$WORKTREE_DIR" rev-parse --abbrev-ref HEAD)
COMMON_DIR=$(git -C "$WORKTREE_DIR" rev-parse --git-common-dir)
if [[ "$COMMON_DIR" = /* ]]; then
    REPO_DIR="$(cd "$COMMON_DIR/.." && pwd)"
else
    REPO_DIR="$(cd "$WORKTREE_DIR/$COMMON_DIR/.." && pwd)"
fi
REPO_NAME=${REPO_DIR##*/}
# fail closed on a read error: assume local (skip remote ops) so a --local node
# is never pushed/deleted remotely just because its config could not be read
LOCAL=$(fractal config _get local --path="$WORKTREE_DIR" 2>/dev/null || echo true)

# ------ fail if node is still running in tmux
# grep -qxF (exact match), not tmux -t: -t resolves targets by
# prefix/fnmatch, so a short name false-matches longer session names
TMUX_SESSION_NAME="${REPO_NAME//[.:]/-} (${BRANCH//./-})"

# the guard is per-backend (mirrors kill.sh): a headless node owns no
# session, so a matching name is another repo's sharing this basename and
# must not block the delete; the node dir nests under the
# .worktrees/.project/<branch> project prefix (mirrors Node.node_dir)
PROJECT="."
PROJECT_FILE="$REPO_DIR/.worktrees/.project/$BRANCH"
if [[ -f "$PROJECT_FILE" ]]; then
    PROJECT=$(cat "$PROJECT_FILE")
fi
if [[ "$PROJECT" == "." ]]; then
    HEADLESS_FILE="$WORKTREE_DIR/.fractal/$BRANCH/.headless"
else
    HEADLESS_FILE="$WORKTREE_DIR/$PROJECT/.fractal/$BRANCH/.headless"
fi

if [[ ! -f "$HEADLESS_FILE" ]] \
    && tmux list-sessions -F '#{session_name}' 2>/dev/null \
    | grep -qxF "$TMUX_SESSION_NAME"; then
    echo "Error: node is still running in tmux ($TMUX_SESSION_NAME)" >&2
    echo "Kill it first with: fractal node kill --path=<path>" >&2
    exit 1
fi

# ------ pre-check worktree removability before touching the remote
# a locked worktree can't be removed; with remote-first ordering (below) a failed
# removal would otherwise leave the remote gone while the node lingers, with no
# safe retry (Node.delete's exists() guard fails on a half-deleted node) -- bail
# here so a locked worktree never costs the remote
WORKTREE_GIT_DIR=$(git -C "$WORKTREE_DIR" rev-parse --absolute-git-dir 2>/dev/null || echo "")
if [[ -n "$WORKTREE_GIT_DIR" && -f "$WORKTREE_GIT_DIR/locked" ]]; then
    echo "Error: worktree is locked, cannot remove: $WORKTREE_DIR" >&2
    echo "  (unlock with: git -C \"$REPO_DIR\" worktree unlock \"$WORKTREE_DIR\")" >&2
    exit 1
fi

# ------ warn on unmerged commits before the destructive teardown
# `branch -D` force-deletes even with commits the parent never absorbed, so
# surface that loss on the automation path too (the interactive prompt warns only
# the user); resolve the merge target like merge.sh and warn unless the work is
# preserved there; a missing target is best-effort, never blocks the delete; a
# recursive caller threads the deletion root's surviving target instead: a
# descendant's self-derived target is its own parent, which dies in the same
# teardown, so the advice would name a deleted branch
if [[ -z "$MERGE_TARGET" ]]; then
    DELETE_BASE=$(fractal config _get base --path="$WORKTREE_DIR" 2>/dev/null || true)
    if [[ -n "$DELETE_BASE" ]]; then
        MERGE_TARGET="$DELETE_BASE"
    elif [[ "$BRANCH" == *.* ]]; then
        MERGE_TARGET="${BRANCH%.*}"
    fi
fi
if [[ -n "$MERGE_TARGET" ]] \
    && git -C "$REPO_DIR" show-ref --verify --quiet "refs/heads/$MERGE_TARGET"; then
    # derive PROJECT_PATH the way merge.sh does (a subdir project nests its seed
    # under PROJECT_PATH) so SEED_EXCLUDE below points at the real seed location
    PROJECT_PATH=$(cat "$REPO_DIR/.worktrees/.project/$BRANCH" 2>/dev/null || echo ".")
    if [[ "$PROJECT_PATH" == "." ]]; then
        SEED_PREFIX=".fractal"
        WIKI_PREFIX="wiki"
    else
        SEED_PREFIX="$PROJECT_PATH/.fractal"
        WIKI_PREFIX="$PROJECT_PATH/wiki"
    fi
    # a scope root that is, or lies under, a .fractal dir is work the merge
    # lands (a --meta node's scope is the target's own seed dir), so exclude
    # only the node's own seed and its descendants' instead of the whole
    # .fractal/ (mirrors merge.sh's SCOPE_ROOTS test: a "." root collapses
    # the scope, and an unset or unreadable scope reads empty)
    SEED_EXCLUDE=(":!$SEED_PREFIX")
    while IFS= read -r SCOPE_ROOT; do
        [[ -n "$SCOPE_ROOT" ]] || continue
        if [[ "$SCOPE_ROOT" == "." ]]; then
            SEED_EXCLUDE=(":!$SEED_PREFIX")
            break
        fi
        if [[ "$PROJECT_PATH" != "." ]]; then
            SCOPE_ROOT="$PROJECT_PATH/$SCOPE_ROOT"
        fi
        if [[ "$SCOPE_ROOT" == .fractal || "$SCOPE_ROOT" == *"/.fractal" || "$SCOPE_ROOT" == *".fractal/"* ]]; then
            SEED_EXCLUDE=(":(exclude)$SEED_PREFIX/$BRANCH" ":(exclude,glob)**/.fractal/$BRANCH.*/**")
        fi
    done < <(fractal config _get scope --path="$WORKTREE_DIR" 2>/dev/null || true)
    # the branch's work is preserved when it is an ancestor of the target (a fast
    # merge) or the target already matches it on the paths the branch itself
    # changed since it forked (a squash merge leaves no ancestry but lands that
    # content); scope the content check to the branch's own changed paths, not a
    # symmetric tree diff -- once a squash-merged child's parent keeps iterating in
    # other paths, a symmetric `diff TARGET BRANCH` false-fires though the child's
    # work is in the target; exclude the .fractal/ seed (merge-up strips it, so it
    # never counts as unmerged) and the wiki's generated indexes plus its whole
    # tool-owned .wiki/ dir (merge-up regenerates them on the target, so the
    # branch's copies always differ; that deliberately waives .wiki/ settings
    # too -- tool config, not work worth warning about) and skip when the base
    # can't resolve (best-effort, never block the delete); warn only when none
    # of these holds
    MERGE_BASE=$(git -C "$REPO_DIR" merge-base "$MERGE_TARGET" "$BRANCH" 2>/dev/null || true)
    if [[ -n "$MERGE_BASE" ]] \
        && ! git -C "$REPO_DIR" merge-base --is-ancestor "$BRANCH" "$MERGE_TARGET" 2>/dev/null; then
        # collect the non-seed paths the branch changed vs the base into an array
        # (read NUL-delimited: -z emits paths verbatim, so a space stays one
        # entry and a non-ASCII name is never C-quoted by core.quotePath into a
        # pathspec that matches nothing); an empty array means only the seed
        # changed -- nothing to warn about
        CHANGED_PATHS=()
        while IFS= read -r -d '' CHANGED_PATH; do
            CHANGED_PATHS+=("$CHANGED_PATH")
        done < <(git -C "$REPO_DIR" diff --name-only -z "$MERGE_BASE" "$BRANCH" \
            -- "${SEED_EXCLUDE[@]}" ":(exclude,glob)$WIKI_PREFIX/**/_index.md" \
            ":!$WIKI_PREFIX/.wiki" 2>/dev/null || true)
        # warn only when the target still differs from the branch on those paths
        # (after a squash merge the target already matches them -> no warning)
        if [[ ${#CHANGED_PATHS[@]} -gt 0 ]] \
            && ! git -C "$REPO_DIR" diff --quiet "$MERGE_TARGET" "$BRANCH" \
                -- "${CHANGED_PATHS[@]}" 2>/dev/null; then
            echo "Warning: $BRANCH has commits not merged into $MERGE_TARGET;" \
                "deleting discards them (merge first to keep them)" >&2
        fi
    fi
fi

# ------ delete remote branch
# remote first, local last: a remote-delete failure must leave the local node
# intact so the delete stays retriable (Node.delete's exists() guard needs the
# worktree present); the removability pre-check above guards the inverse failure
if [[ "$LOCAL" != true ]]; then
    if git -C "$REPO_DIR" ls-remote --exit-code --heads origin "$BRANCH" \
        >/dev/null 2>&1; then
        git -C "$REPO_DIR" push origin --delete "$BRANCH"
        echo "Deleted remote branch: $BRANCH"
    else
        echo "No remote branch to delete: $BRANCH"
    fi
fi

# ------ remove worktree and branch
# worktree first -- a checked-out branch can't be deleted; size captured
# before removal so every reap reports the disk it freed
SIZE=$(du -sh "$WORKTREE_DIR" 2>/dev/null | awk '{print $1}' || true)
SIZE=${SIZE:-?}
git -C "$REPO_DIR" worktree remove --force "$WORKTREE_DIR"
echo "Removed worktree: $WORKTREE_DIR ($SIZE)"

# >/dev/null: drop git's own "Deleted branch ... (was <sha>)"
# so only the script's message below shows (no duplicate line)
TIP=$(git -C "$REPO_DIR" rev-parse --short "$BRANCH")
git -C "$REPO_DIR" branch -D "$BRANCH" >/dev/null
echo "Deleted branch: $BRANCH (was $TIP)"

# ------ clean up .project/ cache entry from init.sh
rm -f "$REPO_DIR/.worktrees/.project/$BRANCH"
