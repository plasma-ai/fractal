#!/usr/bin/env bash
set -euo pipefail

# Squash-merge a node's branch into its parent
# --------------------------------------------

# ------ argument parsing

usage() {
    cat <<USAGE
Usage: merge.sh <path> [options]

Squash-merge a node's branch into its parent branch.
The full commit history is preserved on the node's branch.

Options:
    --continue        Finish a hand-resolved squash: after a conflicted merge,
                      redo 'git merge --squash <branch>' in the target worktree,
                      resolve and stage the conflicts, then pass this flag to
                      run the merge's own tail -- .fractal/ restore and seed
                      strip, footprint check, index refresh, commit, merge-base
                      advance -- exactly like a clean merge.
    --ignore-scope    Land paths outside the node's scope instead of refusing
                      the merge.
    --help|-h         Show this help message
USAGE
    exit 0
}

WORKTREE_DIR=""
CONTINUE=0
IGNORE_SCOPE=0

for arg in "$@"; do
    case "$arg" in
        --help | -h) usage ;;
        --continue) CONTINUE=1 ;;
        --ignore-scope) IGNORE_SCOPE=1 ;;
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

# ------ seed prefix and scope roots

# a sub-project node nests its seed under <project>/.fractal
PROJECT_PATH=$(cat "$REPO_DIR/.worktrees/.project/$BRANCH" 2>/dev/null || echo ".")
if [[ "$PROJECT_PATH" == "." ]]; then
    SEED_PREFIX=".fractal"
else
    SEED_PREFIX="$PROJECT_PATH/.fractal"
fi
# the node's own seed and its descendants' at any depth -- the paths the
# squash never lands on the target and the advance grafts back onto the child
OWN_SEED_SPEC=("$SEED_PREFIX/$BRANCH" ":(glob)**/.fractal/$BRANCH.*/**")
# the node's scope roots, project-prefixed the way fractal commit prefixes
# them (mirrors fractal.core.commit.scope_boundaries); a "." root names the
# project itself and collapses the whole scope; an unset scope reads empty,
# and so does a failed read (fail closed: the restore then treats every
# .fractal/ path as the target's, and DROPPED names whatever that drops)
SCOPE_ROOTS=()
while IFS= read -r SCOPE_ROOT; do
    [[ -n "$SCOPE_ROOT" ]] || continue
    if [[ "$SCOPE_ROOT" == "." ]]; then
        SCOPE_ROOTS=()
        break
    fi
    if [[ "$PROJECT_PATH" == "." ]]; then
        SCOPE_ROOTS+=("$SCOPE_ROOT")
    else
        SCOPE_ROOTS+=("$PROJECT_PATH/$SCOPE_ROOT")
    fi
done < <(fractal config _get scope --path="$WORKTREE_DIR" 2>/dev/null || true)

# ------ leaked seed check

# a node seed the target tracks without owning it -- leaked there by a hand
# merge -- collides with that node's live seed on every later PREPARE merge;
# the user node owns no seed at all (its own dir is self-ignored), a node
# owns its own and its descendants'; this merge removes the merging node's
# own copy, the rest need a hand git rm
ROOT_BRANCH="${BRANCH%%.*}"
TARGET_IS_USER=$(fractal config _get user --path="$PARENT_WORKTREE_DIR" 2>/dev/null || echo false)
SEED_DIR_RE='^(.*\.fractal/[^/]+)/'
TRACKED_SEEDS=""
# read NUL-delimited so a path with spaces stays one entry and a non-ASCII
# name is never C-quoted by core.quotePath into a dir that matches no branch
while IFS= read -r -d '' TRACKED_PATH; do
    [[ "$TRACKED_PATH" =~ $SEED_DIR_RE ]] || continue
    TRACKED_SEEDS+="${BASH_REMATCH[1]}"$'\n'
done < <(git -C "$PARENT_WORKTREE_DIR" ls-files -z -- ":(glob)**/.fractal/$ROOT_BRANCH.*/**")
LEAKED_REMAINING=""
LEAKED_REMOVED=""
while IFS= read -r SEED_DIR; do
    [[ -n "$SEED_DIR" ]] || continue
    SEED_NAME="${SEED_DIR##*/}"
    if [[ "$TARGET_IS_USER" != "true" ]] \
        && [[ "$SEED_NAME" == "$PARENT_BRANCH" || "$SEED_NAME" == "$PARENT_BRANCH".* ]]; then
        continue
    fi
    if [[ "$SEED_NAME" == "$BRANCH" || "$SEED_NAME" == "$BRANCH".* ]]; then
        LEAKED_REMOVED+="$SEED_DIR "
    else
        LEAKED_REMAINING+="$SEED_DIR "
    fi
done < <(printf '%s' "$TRACKED_SEEDS" | sort -u)
if [[ -n "$LEAKED_REMOVED" ]]; then
    echo "Warning: $PARENT_BRANCH tracks $BRANCH's own seed, leaked by an earlier merge:" \
        "${LEAKED_REMOVED% }; this merge removes it" >&2
fi
if [[ -n "$LEAKED_REMAINING" ]]; then
    echo "Warning: $PARENT_BRANCH tracks node seed directories leaked by an earlier merge:" \
        "${LEAKED_REMAINING% }" >&2
    echo "Remove them with: git -C $PARENT_WORKTREE_DIR rm -r --cached ${LEAKED_REMAINING% }" >&2
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
# the target's post-merge commit on the child as a two-parent commit whose tree
# is the target's tree with the child's own seed (and its descendants') grafted
# back from the child's HEAD: the node converges to the target's adjudicated
# content -- a hunk resolved against it, a file dropped from the squash, a
# restore to base content all reach it -- and a stale copy of its seed on the
# target can never overwrite or delete the live one; the tree is built with
# plumbing under a private index, so no merge runs, no hook fires, and nothing
# conflicts; only when the child worktree is clean and still on its own branch
# -- a mid-iteration child is left untouched (the parent merge already
# succeeded); cleanliness is the commit content law's ('fractal commit
# --check'): an estate file the law refuses to commit (a parked credential) can
# never clear, so counting it as dirt would pin the base to the fork point
# forever; the squash already landed on the target, so a failure here must
# never fail the merge
skip_advance() {
    echo "Warning: skipped advancing $BRANCH's merge-base ($1);" \
        "a later re-merge may re-diff from the fork point" >&2
}
advance_merge_base() {
    CHILD_HEAD=$(git -C "$WORKTREE_DIR" rev-parse --abbrev-ref HEAD)
    if [[ "$CHILD_HEAD" != "$BRANCH" ]]; then
        skip_advance "its worktree is on $CHILD_HEAD, not $BRANCH"
        return 0
    fi
    if ! fractal commit --check --path="$WORKTREE_DIR" &>/dev/null; then
        skip_advance "its worktree has uncommitted changes"
        return 0
    fi
    CHILD_OLD=$(git -C "$WORKTREE_DIR" rev-parse HEAD)
    TARGET_HEAD=$(git -C "$PARENT_WORKTREE_DIR" rev-parse HEAD)
    # the child's seed entries as its (clean) index holds them, in the
    # index-info shape update-index reads; a file, not a variable, because a
    # NUL-separated listing cannot ride a bash variable
    if ! git -C "$WORKTREE_DIR" ls-files -s -z -- "${OWN_SEED_SPEC[@]}" >"$TMP_DIR/seed-info"; then
        skip_advance "listing $BRANCH's seed failed"
        return 0
    fi
    # start from the target's tree, drop any copy of this node's seed it
    # carries, graft the child's own back, and write the result -- all in a
    # private index so the child's own index is never touched
    if ! TREE=$(
        export GIT_INDEX_FILE="$TMP_DIR/advance-index"
        git -C "$WORKTREE_DIR" read-tree "$TARGET_HEAD" \
            && git -C "$WORKTREE_DIR" ls-files -z -- "${OWN_SEED_SPEC[@]}" \
            | git -C "$WORKTREE_DIR" update-index -z --force-remove --stdin \
            && git -C "$WORKTREE_DIR" update-index -z --index-info <"$TMP_DIR/seed-info" \
            && git -C "$WORKTREE_DIR" write-tree
    ); then
        skip_advance "building the post-squash tree failed"
        return 0
    fi
    if ! NEW_HEAD=$(git -C "$WORKTREE_DIR" commit-tree "$TREE" -p "$CHILD_OLD" -p "$TARGET_HEAD" \
        -m "merge $PARENT_BRANCH (post-squash)"); then
        skip_advance "recording the post-squash commit failed"
        return 0
    fi
    # reset --hard moves the branch and the worktree together and takes the
    # index lock first, so a lock another process holds leaves the ref in place
    if ! git -C "$WORKTREE_DIR" reset -q --hard "$NEW_HEAD"; then
        skip_advance "updating $BRANCH's worktree failed"
        return 0
    fi
}

# a conflict only under .fractal/ has a known answer: the restore below makes
# the target's HEAD authoritative for every .fractal/ path outside the node's
# scope roots and the node's own seed is always stripped, so resolve such
# entries the same way and let the tail run -- a copy of the node's seed the
# target's history carried and later purged is the case, and its squash would
# otherwise conflict before any strip could run; any other conflict stays the
# operator's (return 1 leaves the conflict path to report it)
SEED_PATH_RE='(^|/)\.fractal/'
resolve_seed_conflicts() {
    UNMERGED=()
    # read NUL-delimited so a path with spaces stays one entry and a non-ASCII
    # name is never C-quoted by core.quotePath into a pathspec matching nothing
    while IFS= read -r -d '' UNMERGED_PATH; do
        UNMERGED+=("$UNMERGED_PATH")
    done < <(git -C "$PARENT_WORKTREE_DIR" diff --name-only --diff-filter=U -z)
    [[ ${#UNMERGED[@]} -gt 0 ]] || return 1
    for UNMERGED_PATH in "${UNMERGED[@]}"; do
        [[ "$UNMERGED_PATH" =~ $SEED_PATH_RE ]] || return 1
        # safe under set -u even on bash 3.2: an empty array reads as unset
        for SCOPE_ROOT in ${SCOPE_ROOTS[@]+"${SCOPE_ROOTS[@]}"}; do
            [[ "$UNMERGED_PATH" == "$SCOPE_ROOT"/* ]] && return 1
        done
    done
    for UNMERGED_PATH in "${UNMERGED[@]}"; do
        if git -C "$PARENT_WORKTREE_DIR" cat-file -e "HEAD:$UNMERGED_PATH" 2>/dev/null; then
            git -C "$PARENT_WORKTREE_DIR" restore --staged --worktree --source=HEAD --quiet \
                -- "$UNMERGED_PATH" || return 1
        else
            git -C "$PARENT_WORKTREE_DIR" rm -q -f -- "$UNMERGED_PATH" || return 1
        fi
    done
    RESOLVED_LIST=$(printf '%s, ' "${UNMERGED[@]}")
    echo "Warning: resolved ${#UNMERGED[@]} conflicting path(s) under .fractal/ to" \
        "$PARENT_BRANCH's own content: ${RESOLVED_LIST%, }" >&2
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
        if [[ -z "$CONFLICTED" ]] || ! resolve_seed_conflicts; then
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

# ------ .fractal/ on the target

# scratch for the tail's NUL-separated listings and the advance's private index
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

# the squash never changes .fractal/ on the target: restore every .fractal dir
# at any depth to HEAD (a sub-project descendant's seed sits under
# <project>/.fractal/), minus a scope root under it -- a --meta node's scope
# is the target's own seed dir, the one upward flow under .fractal/ that is
# work; every step is guarded like the other armed-window commands: a set -e
# exit here would bypass the restore trap and leave the squash staged for the
# parent's next commit to absorb silently
RESTORE_SPEC=(":(glob)**/.fractal/**")
# safe under set -u even on bash 3.2: an empty array reads as unset
for SCOPE_ROOT in ${SCOPE_ROOTS[@]+"${SCOPE_ROOTS[@]}"}; do
    if [[ "$SCOPE_ROOT" == *".fractal/"* ]]; then
        RESTORE_SPEC+=(":(exclude)$SCOPE_ROOT")
    fi
done
# name what the restore drops outside the node's own machinery -- an edit
# to the target's estate or a profile is visible, not silent -- captured
# before the restore erases it from the index; --no-renames so a rename
# lists as its two halves
if ! DROPPED=$(git -C "$PARENT_WORKTREE_DIR" diff --cached --name-only --no-renames HEAD -- \
    "${RESTORE_SPEC[@]}" ":(exclude)$SEED_PREFIX/$BRANCH" ":(exclude,glob)**/.fractal/$BRANCH.*/**"); then
    fail_target "listing $BRANCH's changes under .fractal/ failed"
fi
# one command drops added paths from index and disk and returns modified,
# deleted, and unmerged paths to HEAD; the guard keeps restore from exiting 1
# on a pathspec that matches nothing (git >= 2.23)
if ! git -C "$PARENT_WORKTREE_DIR" diff --cached --quiet --no-renames HEAD -- "${RESTORE_SPEC[@]}"; then
    if ! git -C "$PARENT_WORKTREE_DIR" restore --staged --worktree --source=HEAD --quiet \
        -- "${RESTORE_SPEC[@]}"; then
        fail_target "restoring $PARENT_BRANCH's .fractal/ after the squash of $BRANCH failed"
    fi
fi
if [[ -n "$DROPPED" ]]; then
    echo "Warning: $BRANCH's squash changed paths under .fractal/ that the merge left as" \
        "they are: ${DROPPED//$'\n'/, }" >&2
fi
# strip the node's own seed and its descendants' from the staged merge to
# avoid orphaning them in the target; after the restore because it is the only
# pass that removes a copy of this node's seed the target already tracks (a
# leaked one), which the restore would otherwise put back from HEAD (--quiet:
# drop git rm's per-file "rm '...'" lines so the merge summary below stays the
# single user-facing line)
if ! git -C "$PARENT_WORKTREE_DIR" rm -rf --quiet --ignore-unmatch -- "${OWN_SEED_SPEC[@]}"; then
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
    end_merge_event completed
    echo "Nothing to commit for $PARENT_BRANCH: the resolution kept its own" \
        "content for every change $BRANCH offered"
    exit 0
fi

# ------ footprint check

# the squash is the one point that sees the node's whole offering (commit-time
# scope is bypassable: --ignore-scope, the force backstop, raw git, a PREPARE
# --no-ff of children), so judge the staged paths by the node's commit
# boundaries through the law fractal commit enforces; before the wiki refresh,
# which stages the target's own regenerated state; a failed listing or check
# fails closed, and a refusal restores the target like a conflict
if [[ "$IGNORE_SCOPE" -eq 0 ]]; then
    if ! git -C "$PARENT_WORKTREE_DIR" diff --cached --name-only -z --no-renames HEAD >"$TMP_DIR/footprint"; then
        fail_target "listing the paths of $BRANCH's squash failed"
    fi
    # exit 1 is the check's own answer (paths out of scope), anything else an
    # error; stderr is captured and replayed only on an error, since the CLI
    # closes every non-zero exit with a FAILED line that would read as noise
    SCOPE_RC=0
    OUT_OF_SCOPE=$(fractal node _scope --path="$WORKTREE_DIR" \
        <"$TMP_DIR/footprint" 2>"$TMP_DIR/scope-err") || SCOPE_RC=$?
    if [[ "$SCOPE_RC" -eq 1 ]]; then
        fail_target "the squash of $BRANCH changes paths outside its scope:" \
            "${OUT_OF_SCOPE//$'\n'/, }; widen the scope (fractal node config set" \
            "scope=<dirs> --path=$WORKTREE_DIR) or rerun with --ignore-scope"
    elif [[ "$SCOPE_RC" -ne 0 ]]; then
        cat "$TMP_DIR/scope-err" >&2
        fail_target "checking the scope of $BRANCH's squash failed"
    fi
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
end_merge_event completed
echo "Squash-merged $BRANCH into $PARENT_BRANCH"
