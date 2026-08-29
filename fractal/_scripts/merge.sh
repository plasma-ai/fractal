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
    --user-target     Judge the target as its tree's user node (Node.merge
                      passes it from the repo's record; the checkout probe is
                      the fallback for a direct call).
    --help|-h         Show this help message
USAGE
    exit 0
}

WORKTREE_DIR=""
CONTINUE=false
IGNORE_SCOPE=false
USER_TARGET=false

for arg in "$@"; do
    case "$arg" in
        --help | -h) usage ;;
        --continue) CONTINUE=true ;;
        --ignore-scope) IGNORE_SCOPE=true ;;
        --user-target) USER_TARGET=true ;;
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
# shell-quoted copies for every remedy an operator pastes back into a shell, so
# a path with a space stays one word (printf %q is bash 3.2)
WORKTREE_Q=$(printf '%q' "$WORKTREE_DIR")
PARENT_Q=$(printf '%q' "$PARENT_WORKTREE_DIR")

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
# a scope root that is, or lies under, a .fractal dir is work the squash may
# change there (a --meta node's scope is the target's own seed dir); every pass
# that keeps the target's .fractal/ as it is carves these roots out
SCOPE_EXCLUDES=()
# safe under set -u even on bash 3.2: an empty array reads as unset
for SCOPE_ROOT in ${SCOPE_ROOTS[@]+"${SCOPE_ROOTS[@]}"}; do
    if [[ "$SCOPE_ROOT" == .fractal || "$SCOPE_ROOT" == *"/.fractal" || "$SCOPE_ROOT" == *".fractal/"* ]]; then
        SCOPE_EXCLUDES+=(":(exclude)$SCOPE_ROOT")
    fi
done

# ------ leaked seed check

# a node seed the user node's branch tracks is a leak -- the root owns no seed
# (its own dir is self-ignored) and every node seed is stripped on the way up
# -- and it collides with that node's live seed on every later PREPARE merge; a
# node target is not judged, since its branch legitimately carries other nodes'
# seeds (its ancestors' by fork, its descendants' by PREPARE merges, a
# sibling's by the advance); this merge removes the merging node's own copy,
# the rest need a hand git rm; the user node's seed is self-ignored, so a root
# checked out in a linked worktree carries no config there to probe -- the
# caller's flag settles it, and a failed probe is said, not read as false
if [[ "$USER_TARGET" == true ]]; then
    TARGET_IS_USER=true
elif ! TARGET_IS_USER=$(fractal config _get user --path="$PARENT_WORKTREE_DIR" 2>/dev/null); then
    echo "Warning: could not read $PARENT_BRANCH's node config; treating it as a node target" >&2
    TARGET_IS_USER=false
fi
SEED_DIR_RE='^((.*/)?\.fractal/[^/]+)/'
LEAKED_REMAINING=""
LEAKED_REMOVED=""
REMEDY_DIRS=""
if [[ "$TARGET_IS_USER" == "true" ]]; then
    TRACKED_SEEDS=""
    # HEAD, not the index: a --continue's index carries the operator's hand
    # squash, which always stages the merging node's own seed, and a leak is by
    # definition something the target committed; ls-tree takes no pathspec
    # magic, so the tree's dotted seeds are picked out in the loop; read
    # NUL-delimited so a path with spaces stays one entry and a non-ASCII name
    # is never C-quoted by core.quotePath into a dir that matches no branch
    while IFS= read -r -d '' TRACKED_PATH; do
        [[ "$TRACKED_PATH" =~ $SEED_DIR_RE ]] || continue
        # the user node's branch is its tree's root, so its nodes are named
        # <target>.<...>; a --base merge into another tree's root judges that
        # root and the merging node's seed and descendants (its tree's names)
        [[ "${BASH_REMATCH[1]##*/}" == "$PARENT_BRANCH".* ]] \
            || [[ "${BASH_REMATCH[1]}" == "$SEED_PREFIX/$BRANCH" ]] \
            || [[ "${BASH_REMATCH[1]##*/}" == "$BRANCH".* ]] || continue
        TRACKED_SEEDS+="${BASH_REMATCH[1]}"$'\n'
    done < <(git -C "$PARENT_WORKTREE_DIR" ls-tree -r -z --name-only HEAD)
    while IFS= read -r SEED_DIR; do
        [[ -n "$SEED_DIR" ]] || continue
        SEED_NAME="${SEED_DIR##*/}"
        # what the strip removes: the node's seed at its own prefix and its
        # descendants' at any depth -- a same-named copy under another prefix
        # (the node re-created at a new project path) stays for the remedy
        if [[ "$SEED_DIR" == "$SEED_PREFIX/$BRANCH" || "$SEED_NAME" == "$BRANCH".* ]]; then
            LEAKED_REMOVED+="$SEED_DIR, "
        else
            LEAKED_REMAINING+="$SEED_DIR, "
            REMEDY_DIRS+=" $(printf '%q' "$SEED_DIR")"
        fi
    done < <(printf '%s' "$TRACKED_SEEDS" | sort -u)
fi
if [[ -n "$LEAKED_REMOVED" ]]; then
    echo "Warning: $PARENT_BRANCH tracks seeds of $BRANCH or its descendants, leaked by an" \
        "earlier merge: ${LEAKED_REMOVED%, }; this merge removes them" >&2
fi
if [[ -n "$LEAKED_REMAINING" ]]; then
    # rm -r, not --cached: the copy on the user node's disk is never a live seed
    # (live seeds sit in each node's own worktree), and a copy left on disk
    # would collide with the node's next squash
    echo "Warning: $PARENT_BRANCH tracks node seed directories leaked by an earlier merge:" \
        "${LEAKED_REMAINING%, }" >&2
    echo "Remove them with: git -C $PARENT_Q rm -r --$REMEDY_DIRS" \
        "&& git -C $PARENT_Q commit -m 'drop leaked node seeds'" >&2
fi

if [[ "$CONTINUE" != true ]]; then
    # refuse if parent has uncommitted changes
    if [[ -n "$(git -C "$PARENT_WORKTREE_DIR" status --porcelain --untracked-files=no)" ]]; then
        echo "Error: parent worktree $PARENT_BRANCH has uncommitted changes;" \
            "commit or stash them before merging $BRANCH" >&2
        exit 1
    fi
    # refuse when the squash would write over a file that exists on the
    # target's disk untracked: git treats an ignored file as expendable, so the
    # squash would overwrite it -- a private local.env, or the user node's own
    # live seed, self-ignored on the target but committable from a child -- and
    # the tail would then commit or delete it; mirrors git's own refusal for
    # untracked files, extended to ignored ones; judged from the merge-base, as
    # the squash itself diffs, over every path the node added or changed
    # (--no-renames so a moved file's destination is probed), skipping paths
    # HEAD tracks (they return to HEAD's content in the restore): a path
    # untracked with 'git rm -r' has no disk copy and is no collision, one
    # untracked with '--cached' (fractal untrack's remedy for the root's live
    # seed) is; scope roots are not carved out -- a --meta node's root is
    # HEAD-tracked on its target -- and neither is the node's own seed: an
    # ignored copy of it on the target's disk would be overwritten and then
    # deleted by the restore; a file sitting where the squash creates a
    # directory counts too, so the prefixes are probed like the advance does,
    # stopping at a prefix HEAD tracks (a file the node replaced with a
    # directory is the squash's own type change, not a collision)
    MERGE_BASE=$(git -C "$PARENT_WORKTREE_DIR" merge-base HEAD "$BRANCH" 2>/dev/null || echo HEAD)
    COLLIDING=""
    # read NUL-delimited so a path with spaces stays one entry and a non-ASCII
    # name is never C-quoted by core.quotePath into a path that exists nowhere
    while IFS= read -r -d '' CHANGED_PATH; do
        if git -C "$PARENT_WORKTREE_DIR" cat-file -e "HEAD:$CHANGED_PATH" 2>/dev/null; then
            continue
        fi
        if [[ -e "$PARENT_WORKTREE_DIR/$CHANGED_PATH" || -L "$PARENT_WORKTREE_DIR/$CHANGED_PATH" ]]; then
            COLLIDING+="$CHANGED_PATH, "
            continue
        fi
        PREFIX="$CHANGED_PATH"
        while [[ "$PREFIX" == */* ]]; do
            PREFIX="${PREFIX%/*}"
            git -C "$PARENT_WORKTREE_DIR" cat-file -e "HEAD:$PREFIX" 2>/dev/null && break
            if [[ -e "$PARENT_WORKTREE_DIR/$PREFIX" && ! -d "$PARENT_WORKTREE_DIR/$PREFIX" ]]; then
                COLLIDING+="$PREFIX, "
                break
            fi
        done
    done < <(git -C "$PARENT_WORKTREE_DIR" diff --name-only -z --no-renames --diff-filter=AM \
        "$MERGE_BASE" "$BRANCH")
    if [[ -n "$COLLIDING" ]]; then
        echo "Error: merging $BRANCH into $PARENT_BRANCH would overwrite untracked files in" \
            "$PARENT_BRANCH's worktree: ${COLLIDING%, }; move them aside or drop them from" \
            "$BRANCH before merging" >&2
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
    # a hand-resolved squash is fully staged by contract; an unstaged edit to a
    # tracked .fractal/ path would be rewritten by the restore below
    if ! git -C "$PARENT_WORKTREE_DIR" diff --quiet; then
        UNSTAGED=$(git -C "$PARENT_WORKTREE_DIR" diff --name-only -z | tr '\0' '\n')
        echo "Error: unstaged changes remain in $PARENT_BRANCH's worktree: ${UNSTAGED//$'\n'/, };" \
            "save any copy you need, then stage (git add) the ones that belong to the resolution" \
            "and discard the rest (git -C $PARENT_Q checkout -- <path>) -- the merge restores" \
            "every .fractal/ path to $PARENT_BRANCH's HEAD -- and re-run with --continue" >&2
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
    # the squash must cover the branch's whole offering: SQUASH_MSG lists every
    # commit it took, so a commit the node made after the hand squash (an
    # iteration, a nested merge) is one the advance would write over
    UNSQUASHED=$(comm -23 <(git -C "$PARENT_WORKTREE_DIR" rev-list HEAD.."$BRANCH" | sort) \
        <(awk '/^commit /{print $2}' "$SQUASH_MSG_FILE" | sort))
    if [[ -n "$UNSQUASHED" ]]; then
        echo "Error: $BRANCH has commits newer than the squash in progress in" \
            "$PARENT_BRANCH's worktree; redo the squash ('git -C $PARENT_Q reset --hard HEAD" \
            "&& git -C $PARENT_Q merge --squash $BRANCH'), resolve and stage the conflicts," \
            "then re-run with --continue" >&2
        exit 1
    fi
fi

# ------ scratch

# scratch for the tail's NUL-separated listings and the advance's private
# index, made before the event opens and the restore trap arms so a failure
# here leaves nothing to clean up
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

# ------ squash-merge

# log the merge on the target (it survives the merged child); event_start
# resolves active run lineage, so an idle target's row carries none
# (best-effort -- never block a merge); the trap is armed before the row
# opens, since bash runs it only once the substitution has returned the
# id -- an interrupt during the start still closes the row
EVENT_ID=""
end_merge_event() {
    if [[ "$EVENT_ID" =~ ^[0-9]+$ ]]; then
        fractal event _end "$EVENT_ID" --status="$1" \
            --path="$PARENT_WORKTREE_DIR" 2>/dev/null || true
    fi
}
trap 'end_merge_event failed; exit 1' INT TERM
EVENT_ID=$(fractal event _start merge \
    --metadata="$BRANCH -> $PARENT_BRANCH" \
    --path="$PARENT_WORKTREE_DIR" 2>/dev/null || true)
[[ "$EVENT_ID" =~ ^[0-9]+$ ]] || EVENT_ID=""
if [[ -z "$EVENT_ID" ]]; then
    echo "Warning: merge event for $BRANCH -> $PARENT_BRANCH was not recorded" >&2
fi

# restored means clean and out of the squash: reset's own exit status lies
# when a ref lock fails it after the index and worktree are already written
target_restored() {
    [[ -z "$(git -C "$PARENT_WORKTREE_DIR" status --porcelain --untracked-files=no)" ]] || return 1
    SQUASH_MARKER=$(git -C "$PARENT_WORKTREE_DIR" rev-parse --git-path SQUASH_MSG)
    [[ "$SQUASH_MARKER" = /* ]] || SQUASH_MARKER="$PARENT_WORKTREE_DIR/$SQUASH_MARKER"
    [[ ! -f "$SQUASH_MARKER" ]]
}
# the squash markers are git's own state, cleared by the commit the no-op
# arms skip and left behind by a reset -- they fake a squash still in
# progress, and a bare git commit would prefill the stale squash message;
# rm -rf: a stale marker may be a directory -- so a path git failed to
# answer (an empty word: a failed substitution in a for list does not trip
# set -e) is skipped, never resolved to the worktree root
clear_squash_markers() {
    for MARKER in "$(git -C "$PARENT_WORKTREE_DIR" rev-parse --git-path SQUASH_MSG)" \
        "$(git -C "$PARENT_WORKTREE_DIR" rev-parse --git-path MERGE_MSG)" \
        "$(git -C "$PARENT_WORKTREE_DIR" rev-parse --git-path AUTO_MERGE)"; do
        [[ -n "$MARKER" ]] || continue
        [[ "$MARKER" = /* ]] || MARKER="$PARENT_WORKTREE_DIR/$MARKER"
        rm -rf -- "$MARKER"
    done
}
# fail after the squash is staged: record the event, restore or preserve the
# target, and exit -- a fresh merge owns the staged state and resets it away,
# while a --continue's staged state is the operator's own conflict
# resolution, which a reset --hard would destroy; arguments are joined with
# spaces into the message, so long messages split across lines like echo's
fail_target() {
    end_merge_event failed
    if [[ "$CONTINUE" != true ]]; then
        # -q: drop reset's HEAD-is-now line so the
        # Error: below is the single user-facing line
        git -C "$PARENT_WORKTREE_DIR" reset -q --hard HEAD || true
        if target_restored; then
            echo "Error: $*; the parent worktree has been restored" >&2
        else
            echo "Error: $*; the parent worktree could NOT be restored -- run" \
                "'git -C $PARENT_Q reset --hard HEAD' before merging again" >&2
        fi
    else
        echo "Error: $*; the staged squash is left in place --" \
            "fix and re-run with --continue" >&2
    fi
    exit 1
}

# advance the child's merge-base so a later re-merge only diffs new work --
# squash records no ancestry, so without this the next merge re-diffs from the
# original fork point and spuriously conflicts on every re-touched file; record
# the target's post-squash commit on the child as a two-parent commit whose tree
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
    echo "Warning: skipped advancing $BRANCH's merge-base ($*);" \
        "a later re-merge may re-diff from the fork point" >&2
    ADVANCING=false
}
# the target is settled when this fires (a landed squash, or a no-op arm), so
# the merge is complete whatever the advance managed: finish an interrupted
# worktree update -- NEW_HEAD is set only once the clobber guard passed and
# the reset began, so repeating it is safe -- else leave the child at its old
# commit; warn only when an advance was underway, close the event as
# completed, print the arm's summary, and exit 0 -- the merge succeeded, only
# the advance was cut short
ADVANCING=false
CHILD_OLD=""
NEW_HEAD=""
SUMMARY=""
advance_trap() {
    if [[ "$ADVANCING" == true ]]; then
        # -q: drop reset's HEAD-is-now line so the
        # Warning: below is the single user-facing line
        if [[ -z "$NEW_HEAD" ]] || ! git -C "$WORKTREE_DIR" reset -q --hard "$NEW_HEAD" 2>/dev/null; then
            [[ -z "$CHILD_OLD" ]] || git -C "$WORKTREE_DIR" reset -q --hard "$CHILD_OLD" 2>/dev/null || true
            skip_advance "interrupted; check that its worktree is clean"
        fi
    fi
    end_merge_event completed
    [[ -z "$SUMMARY" ]] || echo "$SUMMARY"
    exit 0
}
advance_merge_base() {
    if ! CHILD_HEAD=$(git -C "$WORKTREE_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null); then
        skip_advance "reading $BRANCH's worktree failed"
        return 0
    fi
    if [[ "$CHILD_HEAD" != "$BRANCH" ]]; then
        skip_advance "its worktree is on $CHILD_HEAD, not $BRANCH"
        return 0
    fi
    # &>/dev/null: the check's verdict is its exit code; its own report
    # would break the single-line merge summary (skip_advance names it)
    if ! fractal commit --check --path="$WORKTREE_DIR" &>/dev/null; then
        skip_advance "its worktree has uncommitted changes"
        return 0
    fi
    # the commit law's excludes hide edits to tracked files of those shapes
    # (a force-added lock or status file), which reset --hard would overwrite
    if ! git -C "$WORKTREE_DIR" diff --quiet HEAD; then
        skip_advance "its worktree has uncommitted changes"
        return 0
    fi
    ADVANCING=true
    if ! CHILD_OLD=$(git -C "$WORKTREE_DIR" rev-parse HEAD 2>/dev/null) \
        || ! TARGET_HEAD=$(git -C "$PARENT_WORKTREE_DIR" rev-parse HEAD 2>/dev/null); then
        skip_advance "reading the commits to record failed"
        return 0
    fi
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
    if ! ADVANCE_HEAD=$(git -C "$WORKTREE_DIR" commit-tree "$TREE" -p "$CHILD_OLD" -p "$TARGET_HEAD" \
        -m "merge $PARENT_BRANCH (post-squash)"); then
        skip_advance "recording the post-squash commit failed"
        return 0
    fi
    # reset --hard writes every path the target tracks, over an untracked or
    # ignored file of the same name too (a private local.env a sibling landed
    # on the target); a collision skips the advance and keeps the node's copy;
    # the disk is probed rather than the untracked listing compared, so a
    # case-only alias on a case-insensitive filesystem, a directory sitting
    # where the target adds a file, and a file sitting where it adds a
    # directory all count -- the worktree is clean at CHILD_OLD, so anything
    # on disk at a path that tree lacks is untracked or ignored; on a
    # case-insensitive filesystem a hit may be a tracked case-variant
    # (the target replaced Readme.md with README.md), which reset renames
    # correctly -- not a collision; icase,literal so the path matches only
    # itself under case folding; likewise a hit the child tracks at or under
    # that path (the target turned a directory into a file) and a prefix it
    # tracks (a file into a directory) are type changes reset performs, not
    # collisions; an unset core.ignorecase is git's default, false, and a
    # failed read fails closed the same way -- a same-name hit then counts
    # as a collision and the advance is skipped
    IGNORE_CASE=$(git -C "$WORKTREE_DIR" config --get core.ignorecase 2>/dev/null || echo false)
    CLOBBERED=""
    # read NUL-delimited so a path with spaces stays one entry and a non-ASCII
    # name is never C-quoted by core.quotePath into a path that exists nowhere
    while IFS= read -r -d '' ADDED_PATH; do
        if [[ -e "$WORKTREE_DIR/$ADDED_PATH" || -L "$WORKTREE_DIR/$ADDED_PATH" ]]; then
            [[ -z "$(git -C "$WORKTREE_DIR" ls-files -- ":(literal)$ADDED_PATH")" ]] || continue
            if [[ "$IGNORE_CASE" == true ]] \
                && [[ -n "$(git -C "$WORKTREE_DIR" ls-files -- ":(icase,literal)$ADDED_PATH")" ]]; then
                continue
            fi
            CLOBBERED+="$ADDED_PATH, "
            continue
        fi
        PREFIX="$ADDED_PATH"
        while [[ "$PREFIX" == */* ]]; do
            PREFIX="${PREFIX%/*}"
            git -C "$WORKTREE_DIR" cat-file -e "$CHILD_OLD:$PREFIX" 2>/dev/null && break
            if [[ -e "$WORKTREE_DIR/$PREFIX" && ! -d "$WORKTREE_DIR/$PREFIX" ]]; then
                CLOBBERED+="$PREFIX, "
                break
            fi
        done
    done < <(git -C "$WORKTREE_DIR" diff --name-only -z --no-renames --diff-filter=A "$CHILD_OLD" "$ADVANCE_HEAD")
    if [[ -n "$CLOBBERED" ]]; then
        skip_advance "its worktree holds untracked files $PARENT_BRANCH now tracks:" \
            "${CLOBBERED%, }; move them aside, and the next merge that lands work advances it"
        return 0
    fi
    # reset --hard writes the index and worktree before it moves the ref, so a
    # ref lock or an interrupt after the checkout leaves the target's tree
    # against the old HEAD -- roll the worktree back to it before skipping;
    # NEW_HEAD is published to the trap only here, once the guard has passed
    NEW_HEAD="$ADVANCE_HEAD"
    # -q: drop reset's HEAD-is-now line so the
    # Warning: below is the single user-facing line
    if ! git -C "$WORKTREE_DIR" reset -q --hard "$NEW_HEAD"; then
        git -C "$WORKTREE_DIR" reset -q --hard "$CHILD_OLD" || true
        NEW_HEAD=""
        skip_advance "updating $BRANCH's worktree failed; check that its worktree is clean"
        return 0
    fi
    ADVANCING=false
}

# a conflict only under .fractal/ has a known answer: the restore below makes
# the target's HEAD authoritative for every .fractal/ path outside the node's
# scope roots, and the node's own seed is stripped from a user-node target, so
# resolve such entries the same way and let the tail run -- a copy of the
# node's seed the target's history carried and later purged is the case, and
# its squash would otherwise conflict before any strip could run; any other
# conflict stays the operator's (return 1 leaves the conflict path to report it)
SEED_PATH_RE='(^|/)\.fractal/'
RESOLVED=""
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
    # a resolved path outside the node's own machinery is an adjudication the
    # no-op arm must record like a restored one, or the same conflict and
    # warning return on every merge; own and descendant seeds are stripped
    for UNMERGED_PATH in "${UNMERGED[@]}"; do
        [[ "$UNMERGED_PATH" == "$SEED_PREFIX/$BRANCH"/* || "$UNMERGED_PATH" == *".fractal/$BRANCH."*/* ]] \
            || RESOLVED+="$UNMERGED_PATH"$'\n'
    done
    # --quiet / -q: the Warning: below names every resolved path
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

# git commit is the only step below that moves the target's HEAD, so an
# interrupt trap that finds it moved has found a landed squash: finish it like
# any other (advance, event completed, summary, exit 0) instead of reporting a
# restore or a squash left in place
TARGET_BEFORE=$(git -C "$PARENT_WORKTREE_DIR" rev-parse HEAD)
finish_landed() {
    [[ "$(git -C "$PARENT_WORKTREE_DIR" rev-parse HEAD)" != "$TARGET_BEFORE" ]] || return 0
    SUMMARY="Squash-merged $BRANCH into $PARENT_BRANCH"
    trap 'advance_trap' INT TERM
    advance_merge_base
    advance_trap
}

if [[ "$CONTINUE" != true ]]; then
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
    restore_trap() {
        finish_landed
        end_merge_event failed
        # -q: drop reset's HEAD-is-now line so the
        # Error: below is the single user-facing line
        git -C "$PARENT_WORKTREE_DIR" reset -q --hard HEAD || true
        if target_restored; then
            echo "Error: merge of $BRANCH was interrupted;" \
                "the parent worktree has been restored" >&2
        else
            echo "Error: merge of $BRANCH was interrupted; the parent worktree could NOT be" \
                "restored -- run 'git -C $PARENT_Q reset --hard HEAD' before merging again" >&2
        fi
        exit 1
    }
    trap 'restore_trap' INT TERM

    # squash-merge; reset on conflict (stdout silenced so the merge summary
    # below stays the single user-facing line; stderr captured and replayed
    # only on failure, where it carries git's abort reason -- on success it
    # carries only git's own "went well" notice)
    if ! SQUASH_STDERR=$(git -C "$PARENT_WORKTREE_DIR" merge --squash "$BRANCH" 2>&1 >/dev/null); then
        [[ -z "$SQUASH_STDERR" ]] || echo "$SQUASH_STDERR" >&2
        # distinguish a real content conflict (unmerged index entries) from a
        # merge that aborted before staging anything (an untracked-file collision,
        # or a racing writer holding the parent index); only the conflict resets
        # -- a blanket reset --hard would wipe whatever a concurrent sibling
        # merge had staged in the shared parent worktree
        CONFLICTED=$(git -C "$PARENT_WORKTREE_DIR" ls-files -u)
        if [[ -z "$CONFLICTED" ]] || ! resolve_seed_conflicts; then
            end_merge_event failed
            if [[ -n "$CONFLICTED" ]]; then
                # -q: drop reset's HEAD-is-now line so the
                # Error: below is the single user-facing line
                git -C "$PARENT_WORKTREE_DIR" reset -q --hard HEAD
                echo "Error: merging $BRANCH into $PARENT_BRANCH produced conflicts;" \
                    "the parent worktree has been restored; redo the squash there by" \
                    "hand ('git merge --squash $BRANCH'), resolve and stage the" \
                    "conflicts, then finish with --continue" >&2
            elif ! target_restored; then
                # git can die after writing the index (a stale or unwritable
                # SQUASH_MSG); the target was clean before the squash, so what
                # is staged is the squash's: reset it and clear the markers
                git -C "$PARENT_WORKTREE_DIR" reset -q --hard HEAD || true
                clear_squash_markers
                if target_restored; then
                    echo "Error: merging $BRANCH into $PARENT_BRANCH failed after staging;" \
                        "the parent worktree has been restored; resolve and retry" >&2
                else
                    echo "Error: merging $BRANCH into $PARENT_BRANCH failed after staging; the" \
                        "parent worktree could NOT be restored -- run 'git -C $PARENT_Q reset" \
                        "--hard HEAD' before merging again" >&2
                fi
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
        finish_landed
        end_merge_event failed
        echo "Error: merge --continue of $BRANCH was interrupted;" \
            "the staged squash is left in place; re-run with --continue" >&2
        exit 1
    ' INT TERM
fi

# ------ .fractal/ on the target

# the squash never changes .fractal/ on the target: restore every .fractal
# dir at any depth to HEAD (a sub-project descendant's seed sits under
# <project>/.fractal/), minus a scope root under it -- a --meta node's
# scope is the target's own seed dir, the one upward flow under .fractal/
# that is work; every step is guarded like the other armed-window
# commands: a set -e exit here would bypass the restore trap and leave the
# squash staged for the parent's next commit to absorb silently
# safe under set -u even on bash 3.2: an empty array reads as unset
RESTORE_SPEC=(":(glob)**/.fractal/**" ${SCOPE_EXCLUDES[@]+"${SCOPE_EXCLUDES[@]}"})
# name what the restore drops outside the node's own machinery -- an edit
# to the target's estate or a profile is visible, not silent -- captured
# before the restore erases it from the index, split by fate: a path the
# target tracks goes back to its content, a path it lacks is removed (the
# node's branch history keeps its copy); --no-renames so a rename lists as
# its two halves
DROPPED_SPEC=("${RESTORE_SPEC[@]}" ":(exclude)$SEED_PREFIX/$BRANCH" ":(exclude,glob)**/.fractal/$BRANCH.*/**")
if ! RESTORED=$(git -C "$PARENT_WORKTREE_DIR" diff --cached --name-only -z --no-renames \
    --diff-filter=a HEAD -- "${DROPPED_SPEC[@]}" | tr '\0' '\n') \
    || ! REMOVED=$(git -C "$PARENT_WORKTREE_DIR" diff --cached --name-only -z --no-renames \
        --diff-filter=A HEAD -- "${DROPPED_SPEC[@]}" | tr '\0' '\n'); then
    fail_target "listing $BRANCH's changes under .fractal/ failed"
fi
# one command drops added paths from index and disk and returns modified,
# deleted, and unmerged paths to HEAD; the guard keeps restore from exiting 1
# on a pathspec that matches nothing (git >= 2.23); --quiet: the Warning:
# lines below name every restored and removed path
if ! git -C "$PARENT_WORKTREE_DIR" diff --cached --quiet --no-renames HEAD -- "${RESTORE_SPEC[@]}"; then
    if ! git -C "$PARENT_WORKTREE_DIR" restore --staged --worktree --source=HEAD --quiet \
        -- "${RESTORE_SPEC[@]}"; then
        fail_target "restoring $PARENT_BRANCH's .fractal/ after the squash of $BRANCH failed"
    fi
fi
if [[ -n "$RESTORED" ]]; then
    echo "Warning: $BRANCH's squash changed paths under .fractal/ that the merge restored to" \
        "$PARENT_BRANCH's content: ${RESTORED//$'\n'/, }" >&2
fi
if [[ -n "$REMOVED" ]]; then
    echo "Warning: $BRANCH's squash added paths under .fractal/ that the merge removed, since" \
        "$PARENT_BRANCH does not track them: ${REMOVED//$'\n'/, }; $BRANCH's branch history" \
        "keeps its copy" >&2
fi
# strip the node's own seed and its descendants' from the user node's branch:
# the restore already dropped what the squash added, so this pass matters for
# a copy the target already tracks -- on the user node such a copy can only be
# a leak, while a node target tracks its descendants' seeds by PREPARE's
# no-ff merges and deleting that copy would reach the child's live seed on its
# next merge of the parent (the parent's own upward squash strips them);
# --quiet: drop git rm's per-file "rm '...'" lines so the merge summary below
# stays the single user-facing line
if [[ "$TARGET_IS_USER" == "true" ]] \
    && ! git -C "$PARENT_WORKTREE_DIR" rm -rf --quiet --ignore-unmatch -- "${OWN_SEED_SPEC[@]}"; then
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
    clear_squash_markers
    if [[ "$CONTINUE" != true ]]; then
        SUMMARY="Nothing to merge: $BRANCH has no changes for $PARENT_BRANCH"
        trap 'advance_trap' INT TERM
        # paths the restore dropped are an adjudication too: the advance moves
        # the child past them, or every later merge re-offers them
        [[ -z "$RESTORED$REMOVED$RESOLVED" ]] || advance_merge_base
    else
        SUMMARY="Nothing to commit for $PARENT_BRANCH: the resolution kept its own"
        SUMMARY+=" content for every change $BRANCH offered"
        trap 'advance_trap' INT TERM
        advance_merge_base
    fi
    end_merge_event completed
    trap - INT TERM
    echo "$SUMMARY"
    exit 0
fi

# ------ footprint check

# the squash is the one point that sees the node's whole offering (commit-time
# scope is bypassable: --ignore-scope, the force backstop, raw git, a PREPARE
# --no-ff of children), so judge the staged paths by the node's commit
# boundaries through the law fractal commit enforces; before the wiki refresh,
# which stages the target's own regenerated state; a failed listing or check
# fails closed, and a refusal restores the target like a conflict
if [[ "$IGNORE_SCOPE" != true ]]; then
    # after the restore every staged .fractal/ path is the merge's own -- a
    # scope-root edit or the seed strip's deletion -- so the rest is judged
    if ! git -C "$PARENT_WORKTREE_DIR" diff --cached --name-only -z --no-renames HEAD -- . \
        ":(exclude,glob)**/.fractal/**" >"$TMP_DIR/footprint"; then
        fail_target "listing the paths of $BRANCH's squash failed"
    fi
    # the worktree-root .gitattributes is admitted only as init's own edit --
    # HEAD's content followed by exactly the two lines the wiki tool appends --
    # the same whole-change rule fractal commit applies (mirrors
    # fractal.core.commit._attributes_is_init_edit); any other added or
    # removed line makes it an ordinary out-of-scope path
    STAGED_ATTRIBUTES=$(git -C "$PARENT_WORKTREE_DIR" show :.gitattributes 2>/dev/null || true)
    HEAD_ATTRIBUTES=$(git -C "$PARENT_WORKTREE_DIR" show HEAD:.gitattributes 2>/dev/null || true)
    ATTRIBUTES_FLAG=()
    ADDED_ATTRIBUTES="${STAGED_ATTRIBUTES#"$HEAD_ATTRIBUTES"}"
    if [[ "$STAGED_ATTRIBUTES" == "$HEAD_ATTRIBUTES"* ]] \
        && [[ -z "$HEAD_ATTRIBUTES" || "$ADDED_ATTRIBUTES" == $'\n'* ]] \
        && [[ "$(printf '%s\n' "$ADDED_ATTRIBUTES" | grep -v '^$' || true)" == $'# Wiki index merge driver\n**/_index.md merge=wiki' ]]; then
        ATTRIBUTES_FLAG=(--attributes-ok)
    fi
    # exit 1 is the check's own answer (paths out of scope), anything else an
    # error; stderr is captured and replayed only on an error, since the CLI
    # closes every non-zero exit with a FAILED line that would read as noise
    SCOPE_RC=0
    # safe under set -u even on bash 3.2: an empty array reads as unset
    OUT_OF_SCOPE=$(fractal node _scope --path="$WORKTREE_DIR" ${ATTRIBUTES_FLAG[@]+"${ATTRIBUTES_FLAG[@]}"} \
        <"$TMP_DIR/footprint" 2>"$TMP_DIR/scope-err") || SCOPE_RC=$?
    if [[ "$SCOPE_RC" -eq 1 && "$CONTINUE" != true ]]; then
        fail_target "the squash of $BRANCH changes paths outside its scope:" \
            "${OUT_OF_SCOPE//$'\n'/, }; widen the scope (fractal node config set" \
            "scope=<dirs> --path=$WORKTREE_Q, then fractal commit 'widen scope'" \
            "--path=$WORKTREE_Q) or rerun with --ignore-scope"
    elif [[ "$SCOPE_RC" -eq 1 ]]; then
        # a node commit after the hand squash refuses --continue, so widening
        # the scope means redoing the squash
        fail_target "the squash of $BRANCH changes paths outside its scope:" \
            "${OUT_OF_SCOPE//$'\n'/, }; re-run with --continue --ignore-scope to land them, or" \
            "widen the scope (fractal node config set scope=<dirs> --path=$WORKTREE_Q, then" \
            "fractal commit 'widen scope' --path=$WORKTREE_Q) and redo the squash (git -C" \
            "$PARENT_Q reset --hard HEAD && git -C $PARENT_Q merge --squash $BRANCH)"
    elif [[ "$SCOPE_RC" -ne 0 ]]; then
        cat "$TMP_DIR/scope-err" >&2
        fail_target "checking the scope of $BRANCH's squash failed"
    fi
fi

# ------ wiki refresh

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

# ------ commit and advance

# nothing staged after the refresh means the squash offered only generated
# wiki state the parent regenerates as its own bytes -- an adjudicated no-op
# the pre-refresh guard cannot see, and the commit below would die on the
# empty index; the squash staged content, so the markers exist on both paths
# and are cleared like the no-op above
if git -C "$PARENT_WORKTREE_DIR" diff --cached --quiet; then
    trap - INT TERM
    clear_squash_markers
    if [[ "$CONTINUE" != true ]]; then
        SUMMARY="Nothing to merge: $BRANCH has no changes for $PARENT_BRANCH"
        trap 'advance_trap' INT TERM
        # paths the restore dropped are an adjudication too: the advance moves
        # the child past them, or every later merge re-offers them
        [[ -z "$RESTORED$REMOVED$RESOLVED" ]] || advance_merge_base
    else
        SUMMARY="Nothing to commit for $PARENT_BRANCH: the resolution kept its own"
        SUMMARY+=" content for every change $BRANCH offered"
        trap 'advance_trap' INT TERM
        advance_merge_base
    fi
    end_merge_event completed
    trap - INT TERM
    echo "$SUMMARY"
    exit 0
fi

# commit the squash-merge and report success (-q: drop git's own commit
# summary so the merge line below stays the single user-facing line)
if ! git -C "$PARENT_WORKTREE_DIR" commit -q -m "merge $BRANCH"; then
    fail_target "failed to commit the squash-merge of $BRANCH"
fi
# the squash has landed: from here an interrupt must neither leave the child
# half checked out nor the event open, so the restore trap gives way to the
# advance trap until the event closes
SUMMARY="Squash-merged $BRANCH into $PARENT_BRANCH"
trap 'advance_trap' INT TERM

advance_merge_base
end_merge_event completed
trap - INT TERM
echo "$SUMMARY"
