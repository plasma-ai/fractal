#!/usr/bin/env bash
set -euo pipefail

# Squash-merge a node's branch into its parent
# --------------------------------------------

# ------ argument parsing

usage() {
    cat <<USAGE
Usage: merge.sh <path> [--continue]

Squash-merge a node's branch into its parent branch.
The full commit history is preserved on the node's branch.

Options:
    --continue   Finish a hand-resolved squash: after a conflicted merge,
                 redo 'git merge --squash <branch>' in the target worktree,
                 resolve and stage the conflicts, then pass this flag to
                 strip the seed, refresh indexes, commit, and advance the
                 merge-base exactly like a clean merge.
    --help|-h    Show this help message
USAGE
    exit 0
}

WORKTREE_DIR=""
CONTINUE=0

for arg in "$@"; do
    case "$arg" in
        --help | -h) usage ;;
        --continue) CONTINUE=1 ;;
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

# ------ resolve branch and git root

BRANCH=$(git -C "$WORKTREE_DIR" rev-parse --abbrev-ref HEAD)
COMMON_DIR=$(git -C "$WORKTREE_DIR" rev-parse --git-common-dir)
if [[ "$COMMON_DIR" = /* ]]; then
    REPO_DIR="$(cd "$COMMON_DIR/.." && pwd)"
else
    REPO_DIR="$(cd "$WORKTREE_DIR/$COMMON_DIR/.." && pwd)"
fi

# ------ determine merge target

BASE_BRANCH=$(fractal config _get base --path="$WORKTREE_DIR" 2>/dev/null || true)
if [[ -n "$BASE_BRANCH" ]]; then
    PARENT_BRANCH="$BASE_BRANCH"
elif [[ "$BRANCH" == *.* ]]; then
    PARENT_BRANCH="${BRANCH%.*}"
else
    # no base and no dotted parent -- refuse to guess from the
    # checked-out branch (it may have been switched, which would
    # merge into the wrong target)
    echo "Error: cannot determine a merge target for top-level branch $BRANCH;" \
        "set the node's base explicitly" >&2
    exit 1
fi

# ------ find parent worktree

# match the worktree line with substr (not $2) so a path containing
# spaces is preserved -- mirrors init.sh and Python worktree_map
PARENT_WORKTREE_DIR=$(git -C "$REPO_DIR" worktree list --porcelain \
    | awk -v b="refs/heads/$PARENT_BRANCH" \
        'index($0,"worktree ")==1{wt=substr($0,10)} $1=="branch" && $2==b{print wt}')
if [[ -z "$PARENT_WORKTREE_DIR" ]]; then
    echo "Error: no worktree found with branch $PARENT_BRANCH checked out" >&2
    exit 1
fi

if [[ "$CONTINUE" -eq 0 ]]; then
    # refuse if parent has uncommitted changes
    if [[ -n "$(git -C "$PARENT_WORKTREE_DIR" status --porcelain --untracked-files=no)" ]]; then
        echo "Error: parent worktree $PARENT_BRANCH has uncommitted changes;" \
            "commit or stash them before merging $BRANCH" >&2
        exit 1
    fi
else
    # a --continue expects a dirty parent -- it picks up an operator's
    # hand-redone squash after a conflicted merge -- so it validates that state
    # instead: require the squash marker, fully staged resolutions, and
    # provenance from this branch; continuing a stranger's squash would strip
    # the wrong seed and advance the wrong merge-base
    # the pre-merge base, captured before the squash lands: it is what the
    # node's own offering is measured against below, and the merge-base advance
    # would otherwise move it to the target's HEAD
    PRE_MERGE_BASE=$(git -C "$PARENT_WORKTREE_DIR" merge-base HEAD "$BRANCH" 2>/dev/null || true)
    SQUASH_MSG_FILE=$(git -C "$PARENT_WORKTREE_DIR" rev-parse --git-path SQUASH_MSG)
    [[ "$SQUASH_MSG_FILE" = /* ]] || SQUASH_MSG_FILE="$PARENT_WORKTREE_DIR/$SQUASH_MSG_FILE"
    if [[ ! -f "$SQUASH_MSG_FILE" ]]; then
        echo "Error: no squash merge is in progress in $PARENT_BRANCH's worktree;" \
            "run 'git merge --squash $BRANCH' there, resolve and stage the" \
            "conflicts, then re-run with --continue" >&2
        exit 1
    fi
    if [[ -n "$(git -C "$PARENT_WORKTREE_DIR" ls-files -u)" ]]; then
        echo "Error: unresolved conflicts remain in $PARENT_BRANCH's worktree;" \
            "resolve and stage them (git add), then re-run with --continue" >&2
        exit 1
    fi
    SQUASH_SHA=$(awk '/^commit /{print $2; exit}' "$SQUASH_MSG_FILE")
    if [[ -n "$SQUASH_SHA" ]] \
        && ! git -C "$PARENT_WORKTREE_DIR" merge-base --is-ancestor \
            "$SQUASH_SHA" "$BRANCH" 2>/dev/null; then
        echo "Error: the squash in progress in $PARENT_BRANCH's worktree does" \
            "not come from $BRANCH; commit or abort it before merging this node" >&2
        exit 1
    fi
fi

# ------ squash-merge

# log the merge on the target (it survives the merged child);
# event_start resolves active run lineage, so an idle target's
# row carries none (best-effort -- never block a merge)
EVENT_ID=$(fractal event _start merge \
    --metadata="$BRANCH -> $PARENT_BRANCH" \
    --path="$PARENT_WORKTREE_DIR" 2>/dev/null || true)
[[ "$EVENT_ID" =~ ^[0-9]+$ ]] || EVENT_ID=""
if [[ -z "$EVENT_ID" ]]; then
    echo "Warning: merge event for $BRANCH -> $PARENT_BRANCH was not recorded" >&2
fi
end_merge_event() {
    if [[ -n "$EVENT_ID" ]]; then
        fractal event _end "$EVENT_ID" --status="$1" \
            --path="$PARENT_WORKTREE_DIR" 2>/dev/null || true
    fi
}

# fail after the squash is staged: record the event, restore or preserve the
# target, and exit -- a fresh merge owns the staged state and resets it away,
# while a --continue's staged state is the operator's own conflict
# resolution, which a reset --hard would destroy; arguments are joined with
# spaces into the message, so long messages split across lines like echo's
fail_target() {
    end_merge_event failed
    if [[ "$CONTINUE" -eq 0 ]]; then
        git -C "$PARENT_WORKTREE_DIR" reset --hard HEAD || true
        echo "Error: $*; the parent worktree has been restored" >&2
    else
        echo "Error: $*; the staged squash is left in place --" \
            "fix and re-run with --continue" >&2
    fi
    exit 1
}

# advance the child's merge-base so a later re-merge only diffs new work --
# squash records no ancestry, so without this the next merge re-diffs from the
# original fork point and spuriously conflicts on every re-touched file; record
# the parent's post-merge commit on the child with an ours-merge (tree
# unchanged) only when the child worktree is clean and still on its own branch;
# a mid-iteration child is left untouched (the parent merge already succeeded);
# cleanliness is the commit content law's ('fractal commit --check'): an estate
# file the law refuses to commit (a parked credential) can never clear, so
# counting it as dirt would pin the base to the fork point forever
advance_merge_base() {
    CHILD_HEAD=$(git -C "$WORKTREE_DIR" rev-parse --abbrev-ref HEAD)
    if [[ "$CHILD_HEAD" != "$BRANCH" ]]; then
        echo "Warning: skipped advancing $BRANCH's merge-base (its worktree is on" \
            "$CHILD_HEAD, not $BRANCH); a later re-merge may re-diff from the fork point" >&2
    elif ! fractal commit --check --path="$WORKTREE_DIR" &>/dev/null; then
        echo "Warning: skipped advancing $BRANCH's merge-base (its worktree has" \
            "uncommitted changes); a later re-merge may re-diff from the fork point" >&2
    else
        # -q: drop git's own "Merge made by ..." line so the merge summary
        # below stays the single user-facing line
        git -C "$WORKTREE_DIR" merge -q -s ours --no-edit "$PARENT_BRANCH"
    fi
}

# name the paths a --continue's resolution settled against the node: ones it
# offered that the target now holds differently; the squash records no per-hunk
# ancestry, so the node keeps its own version and the next merge re-stages it
# cleanly -- an explicit rejection silently undone unless the operator pushes
# the resolution down to the node
warn_resolved_against_node() {
    # scope to the paths the node itself changed since the pre-merge base, the
    # way delete.sh scopes its unmerged check -- a symmetric tree diff would
    # also name everything the target owns and the node never had; exclude the
    # seed (the merge strips it) and the generated wiki indexes plus the
    # tool-owned .wiki/ dir (the merge regenerates them on the target, so the
    # node's copies always differ)
    [[ -n "$PRE_MERGE_BASE" ]] || return 0
    if [[ "$PROJECT_PATH" == "." ]]; then
        WIKI_PREFIX="wiki"
    else
        WIKI_PREFIX="$PROJECT_PATH/wiki"
    fi
    # read NUL-delimited so a path with spaces stays one entry and a non-ASCII
    # name is never C-quoted by core.quotePath into a pathspec matching nothing
    OFFERED_PATHS=()
    while IFS= read -r -d '' OFFERED_PATH; do
        OFFERED_PATHS+=("$OFFERED_PATH")
    done < <(git -C "$PARENT_WORKTREE_DIR" diff --name-only -z \
        "$PRE_MERGE_BASE" "$BRANCH" -- ":!$SEED_PREFIX" \
        ":(exclude,glob)$WIKI_PREFIX/**/_index.md" ":!$WIKI_PREFIX/.wiki" \
        2>/dev/null || true)
    [[ ${#OFFERED_PATHS[@]} -gt 0 ]] || return 0
    RESOLVED_PATHS=()
    while IFS= read -r -d '' RESOLVED_PATH; do
        RESOLVED_PATHS+=("$RESOLVED_PATH")
    done < <(git -C "$PARENT_WORKTREE_DIR" diff --name-only -z HEAD "$BRANCH" \
        -- "${OFFERED_PATHS[@]}" 2>/dev/null || true)
    [[ ${#RESOLVED_PATHS[@]} -gt 0 ]] || return 0
    RESOLVED_LIST=$(printf '%s, ' "${RESOLVED_PATHS[@]}")
    echo "Warning: $PARENT_BRANCH keeps its own content over $BRANCH's in" \
        "${#RESOLVED_PATHS[@]} file(s) (${RESOLVED_LIST%, }); $BRANCH still" \
        "carries its version, so a later re-merge re-stages it -- land the" \
        "resolution on $BRANCH (or retire it) to make the decision stick" >&2
}

if [[ "$CONTINUE" -eq 0 ]]; then
    # re-assert cleanliness immediately before arming the destructive trap: the trap
    # (and the conflict/commit-failure paths) reset --hard HEAD, which would clobber
    # any tracked edit the user made in the target after the check above -- for a
    # top-level node the target is the user's own root worktree; refusing here, before
    # the trap is armed, keeps that reset scoped to merge.sh's own staged squash (tree
    # verified clean immediately prior)
    if [[ -n "$(git -C "$PARENT_WORKTREE_DIR" status --porcelain --untracked-files=no)" ]]; then
        end_merge_event failed
        echo "Error: parent worktree $PARENT_BRANCH has uncommitted changes;" \
            "commit or stash them before merging $BRANCH" >&2
        exit 1
    fi

    # restore parent on interrupt so a half-merge never lands -- a signal
    # between the squash-stage and commit would otherwise leave a staged
    # index the parent absorbs into its next commit
    trap '
        git -C "$PARENT_WORKTREE_DIR" reset --hard HEAD || true
        end_merge_event failed
        echo "Error: merge of $BRANCH was interrupted;" \
            "the parent worktree has been restored" >&2
        exit 1
    ' INT TERM

    # squash-merge; reset on conflict (stdout silenced so the merge summary
    # below stays the single user-facing line; conflict diagnostics ride stderr)
    if ! git -C "$PARENT_WORKTREE_DIR" merge --squash "$BRANCH" >/dev/null; then
        # distinguish a real content conflict (unmerged index entries) from a
        # merge that aborted before staging anything (an untracked-file collision,
        # or a racing writer holding the parent index); only the conflict resets
        # -- a blanket reset --hard would wipe whatever a concurrent sibling
        # merge had staged in the shared parent worktree
        CONFLICTED=$(git -C "$PARENT_WORKTREE_DIR" ls-files -u)
        end_merge_event failed
        if [[ -n "$CONFLICTED" ]]; then
            git -C "$PARENT_WORKTREE_DIR" reset --hard HEAD
            echo "Error: merging $BRANCH into $PARENT_BRANCH produced conflicts;" \
                "the parent worktree has been restored; redo the squash there by" \
                "hand ('git merge --squash $BRANCH'), resolve and stage the" \
                "conflicts, then finish with --continue" >&2
        else
            echo "Error: merging $BRANCH into $PARENT_BRANCH failed before staging" \
                "anything (untracked files in the parent would be overwritten, or" \
                "another git process holds its index); resolve and retry" >&2
        fi
        exit 1
    fi
else
    # the interrupt trap in a --continue never resets: the staged squash is
    # the operator's hand-resolved state, recoverable by re-running
    trap '
        end_merge_event failed
        echo "Error: merge --continue of $BRANCH was interrupted;" \
            "the staged squash is left in place; re-run with --continue" >&2
        exit 1
    ' INT TERM
fi

# strip the node's seed from the staged merge to avoid orphaning it in parent
# (--quiet: drop git rm's per-file "rm '...'" lines so the merge summary
# below stays the single user-facing line)
PROJECT_PATH=$(cat "$REPO_DIR/.worktrees/.project/$BRANCH" 2>/dev/null || echo ".")
if [[ "$PROJECT_PATH" == "." ]]; then
    SEED_PREFIX=".fractal"
else
    SEED_PREFIX="$PROJECT_PATH/.fractal"
fi
# guarded like every armed-window command: a set -e exit here (index.lock
# contention, disk full) would bypass the restore and leave the squash staged
# for the parent's next commit to absorb silently
if ! git -C "$PARENT_WORKTREE_DIR" rm -rf --quiet --ignore-unmatch -- \
    "$SEED_PREFIX/$BRANCH" ":(glob)$SEED_PREFIX/$BRANCH.*/**"; then
    fail_target "stripping $BRANCH's seed from the staged merge failed"
fi

# nothing staged after seed strip means no-op merge -- a fresh merge found the
# child had nothing new, but a --continue's operator resolved every change the
# child offered back to the target's own content; that is still an adjudication
# of the child's work, so it runs the rest of the tail (merge-base advanced)
# and only the commit is skipped -- exiting as the fresh arm does would replay
# the very conflict the operator just resolved on the next merge
if git -C "$PARENT_WORKTREE_DIR" diff --cached --quiet; then
    trap - INT TERM
    # the squash markers are git's own state, cleared by the commit both arms
    # skip -- left behind they fake a squash still in progress, and a bare
    # git commit would prefill the stale squash message
    for MARKER in "$(git -C "$PARENT_WORKTREE_DIR" rev-parse --git-path SQUASH_MSG)" \
        "$(git -C "$PARENT_WORKTREE_DIR" rev-parse --git-path MERGE_MSG)" \
        "$(git -C "$PARENT_WORKTREE_DIR" rev-parse --git-path AUTO_MERGE)"; do
        [[ "$MARKER" = /* ]] || MARKER="$PARENT_WORKTREE_DIR/$MARKER"
        rm -f "$MARKER"
    done
    if [[ "$CONTINUE" -eq 0 ]]; then
        end_merge_event completed
        echo "Nothing to merge: $BRANCH has no changes for $PARENT_BRANCH"
        exit 0
    fi
    advance_merge_base
    warn_resolved_against_node
    end_merge_event completed
    echo "Nothing to commit for $PARENT_BRANCH: the resolution kept its own" \
        "content for every change $BRANCH offered"
    exit 0
fi

# the _index.md merge driver keeps ours per link block, dropping the merged
# branch's rows, so regenerate each tracked wiki's indexes from the merged
# filesystem and stage the refreshed bytes to ride the squash commit (an
# untracked wiki -- the git-excluded default seed -- carries no merge changes,
# and adding it would fail); the add is scoped to what the refresh owns --
# tracked-file updates plus any new _index.md and tool-owned .wiki/ state --
# so an untracked draft under the wiki never rides the merge commit; stdout
# is silenced so the merge summary below stays the single user-facing line;
# a failed refresh restores the parent exactly like a conflict
if command -v wiki &>/dev/null; then
    PARENT_PROJECT=$(fractal config _get project --path="$PARENT_WORKTREE_DIR" 2>/dev/null || echo ".")
    if [[ "$PARENT_PROJECT" == "." ]]; then
        WIKI_DIR="$PARENT_WORKTREE_DIR/wiki"
        MEMORY_DIR="$PARENT_WORKTREE_DIR/.fractal/$PARENT_BRANCH/memory"
    else
        WIKI_DIR="$PARENT_WORKTREE_DIR/$PARENT_PROJECT/wiki"
        MEMORY_DIR="$PARENT_WORKTREE_DIR/$PARENT_PROJECT/.fractal/$PARENT_BRANCH/memory"
    fi
    for INDEX_DIR in "$WIKI_DIR" "$MEMORY_DIR"; do
        # guarded: a set -e exit from a failed ls-files would strand the
        # staged squash without the restore below
        if ! TRACKED=$(git -C "$PARENT_WORKTREE_DIR" ls-files -- "$INDEX_DIR/_index.md"); then
            fail_target "reading the tracked wiki index under $INDEX_DIR" \
                "after merging $BRANCH failed"
        fi
        [[ -n "$TRACKED" ]] || continue
        if ! wiki update --path="$INDEX_DIR" >/dev/null \
            || ! git -C "$PARENT_WORKTREE_DIR" add -u -- "$INDEX_DIR" \
            || ! git -C "$PARENT_WORKTREE_DIR" add -- \
                ":(glob)$INDEX_DIR/**/_index.md" "$INDEX_DIR/.wiki"; then
            fail_target "refreshing the wiki indexes under $INDEX_DIR" \
                "after merging $BRANCH failed"
        fi
    done
fi

# nothing staged after the refresh means the squash offered only generated
# wiki state the parent regenerates as its own bytes -- an adjudicated no-op
# the pre-refresh guard cannot see, and the commit below would die on the
# empty index; the squash staged content, so the markers exist on both paths
# and are cleared like the no-op above
if git -C "$PARENT_WORKTREE_DIR" diff --cached --quiet; then
    trap - INT TERM
    for MARKER in "$(git -C "$PARENT_WORKTREE_DIR" rev-parse --git-path SQUASH_MSG)" \
        "$(git -C "$PARENT_WORKTREE_DIR" rev-parse --git-path MERGE_MSG)" \
        "$(git -C "$PARENT_WORKTREE_DIR" rev-parse --git-path AUTO_MERGE)"; do
        [[ "$MARKER" = /* ]] || MARKER="$PARENT_WORKTREE_DIR/$MARKER"
        rm -f "$MARKER"
    done
    if [[ "$CONTINUE" -eq 0 ]]; then
        end_merge_event completed
        echo "Nothing to merge: $BRANCH has no changes for $PARENT_BRANCH"
        exit 0
    fi
    advance_merge_base
    warn_resolved_against_node
    end_merge_event completed
    echo "Nothing to commit for $PARENT_BRANCH: the resolution kept its own" \
        "content for every change $BRANCH offered"
    exit 0
fi

# commit the squash-merge and report success (-q: drop git's own commit
# summary so the merge line below stays the single user-facing line)
if ! git -C "$PARENT_WORKTREE_DIR" commit -q -m "merge $BRANCH"; then
    fail_target "failed to commit the squash-merge of $BRANCH"
fi
trap - INT TERM

advance_merge_base

# only a hand-resolved squash can settle a hunk against the node: a clean merge
# takes the node's content wholesale, so its trees already agree
if [[ "$CONTINUE" -eq 1 ]]; then
    warn_resolved_against_node
fi

end_merge_event completed
echo "Squash-merged $BRANCH into $PARENT_BRANCH"
