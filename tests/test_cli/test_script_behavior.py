"""Script-internal behavior of the node lifecycle shells (``_scripts/``).

Drives ``fractal/_scripts/{init,start,resume,merge,delete}.sh`` against repos
built by the real CLI, pinning edges the end-to-end lifecycle tests don't reach:

- **``init.sh`` worktree resolution** parses ``git worktree list --porcelain``
  with ``substr`` (not ``$2``), so a repo path containing a space resolves the
  parent worktree intact instead of truncating at the first space.
- **``init.sh`` worktree-anchor guard** rejects only fractal's own worktrees
  (a ``.worktrees`` ancestor whose parent is itself a git repo), so a repo
  that merely lives under a ``.worktrees``-named path still spawns nodes.
- **``init.sh`` skill inheritance** seeds a child's ``skills/`` from the
  package unless the spawn passes ``--inherit=skills``, which copies the
  parent's set wholesale; the snapshot is one-shot, so a ``--reset``
  re-inherits only when the flag is passed again.
- **``init.sh`` reseeding** treats a fresh worktree whose fork point already
  carries files under the node's seed dir -- a PREPARE-merged copy of a
  deleted node of the same name, or a partial leak as slight as a lone
  ``NODE.md`` -- like ``--reset``, warning that the stale copy is reseeded
  and removing the dir whole, so the init's own flags and charter land
  instead of the dead incarnation's and no stray file of the copy survives.
- **``resume.sh`` backend selection** relaunches a paused headless node through
  ``start.sh --headless --resume`` and a paused tmux node through plain
  ``start.sh --resume``; its still-parking tmux guard is per-backend, so a
  same-named session from another repo sharing the basename never blocks a
  headless resume.
- **``start.sh``/``resume.sh`` headless second-launch vet** hands the guard to
  ``node _launch``'s identity-checked liveness law: a live recorded group
  refuses the relaunch (a resume with the retry-never-kill wording), and an
  unanswerable identity probe refuses naming the ``ps`` check rather than
  reading ignorance as dead.
- **``start.sh`` headless marker ownership** leaves the ``.headless`` write to
  ``node _launch``, which records it beside ``.pgid`` around the spawn -- so a
  handoff that fails leaves no marker and the record only ever names a backend
  the node actually launched with; a tmux relaunch whose ``new-session``
  refuses rolls the pre-cleared marker back for the same reason.
- **``kill.sh`` per-backend teardown** reaps a headless node's recorded
  ``.pgid``/``.step_pgid`` groups without consulting tmux at all, so a
  same-named session from another repo sharing the basename survives the kill
  and the node's own loop is the one reaped.
- **``delete.sh`` per-backend running guard** blocks the teardown on a bare
  tmux session-name match only for a tmux node; a headless node owns no
  session, so a same-named session from another repo sharing the basename
  never refuses its delete.
- **``merge.sh`` interrupt safety** re-asserts the target worktree is clean
  immediately before the destructive squash, so an edit that lands in the
  target *during* the merge is refused -- never absorbed into the squash commit
  nor discarded by the recovery ``reset --hard``.
- **``merge.sh`` scratch** is made before the merge event opens and the
  restore trap arms, so a failed ``mktemp`` exits with the target clean and
  no squash state behind.
- **``merge.sh`` merge-base advance** judges the child worktree by the commit
  content law (``fractal commit --check``), so an estate file the law refuses
  (a parked ``.env``) never blocks the advance -- only work a commit would
  take (dirty tracked edits, committable untracked files) skips it.
- **``merge.sh`` merge-base advance shape** records the target's post-squash
  commit on the child as a real two-parent commit whose tree is the target's
  with the child's own and descendant seeds grafted back from its HEAD: the
  child converges to the target outside its own machinery (a file the target
  changed since the fork is never re-offered as the child's stale copy, and a
  profile the target gained reaches the child), the seed trees are unchanged
  byte for byte, and a child index or branch ref another process holds skips
  the advance with the warning instead of failing the landed merge (a ref
  lock rolls the half-written worktree back), as does a read of the child's
  worktree git cannot answer (the landed squash is still reported and its
  event closed, never aborted past the commit) and an edit to a tracked file
  of a shape the commit law excludes (a force-tracked ``config.json.lock``,
  which the law's own check reads as clean), as does a private ignored file
  the target now tracks, which the advance would otherwise overwrite -- the
  disk is probed, so a directory sitting where the target adds a file, a
  file where it adds a directory, and a case-only alias count too -- with
  rename detection off, so a target rename onto the child's private path is
  a collision, while a disk hit the child's index tracks as a case-variant
  is the reset's own rename, not one, and a hit at or under a path the
  child's tree tracks, or at a prefix it tracks, is a type change the target
  made (a file into a directory, or back) that the reset performs -- the
  index read as text, so no NUL byte leaks onto stderr; a fresh "Nothing to
  merge" whose restore dropped paths advances the child too, so the drop is
  never re-offered, and so does one whose only conflict resolved itself on
  a foreign ``.fractal/`` path (an edit to the node's own seed is no
  adjudication and leaves the child put); into a node target that tracks
  the child's seed from a
  PREPARE fold, the advance strips the target's copy before grafting the
  live seed, so a seed file the child dropped never comes back; and once
  the squash commit is on the target an interrupt finishes or rolls back
  the child's update, closes the event, and reports the landed squash with
  exit 0 rather than failing a complete merge.
- **``merge.sh`` interrupts once the target is settled** are judged by the
  target, not the step in flight: a SIGINT while ``git commit`` runs its
  post-commit hook (the ref moved, the child not yet reaped) finds a landed
  squash in either arm and finishes it instead of reporting a restore or a
  squash left in place; one in the event close after the clobber guard
  skipped the advance leaves the child untouched with the skip's single
  warning; and one in a no-op merge's event close reports the no-op alone.
- **``merge.sh`` ``.fractal/`` restore** returns every ``.fractal`` directory
  on the target to its HEAD after the squash, minus the merging node's scope
  roots under it (a ``--meta`` node's edit to the target's seed still lands,
  and so does a ``.fractal``-scoped node's profile edit), then strips the
  node's own seed and descendants at any depth and project prefix -- so a
  child's write into the target's estate or a foreign seed never lands, with
  a warning naming the dropped paths by fate (restored to the target's
  content, or removed as a path it never tracked; non-ASCII names printed as
  they are); the strip runs only on the user node, so a node target keeps
  the copy of a child's seed its PREPARE merge tracked and the child's own
  merge of the parent keeps its live seed; any path the target holds
  untracked (the user node's live seed, a file where the squash creates a
  directory, an ignored ``local.env`` a scoped node force-added) refuses the
  merge before the squash instead of being overwritten -- judged from the
  merge-base as the squash is, over every path the node added or changed
  with no scope carve-out, so a path the target dropped from disk since the
  fork is no collision while one kept on disk with ``--cached`` is, the
  node's own seed carved out no more than any other path (an ignored copy of
  it on the target's disk refuses, at the repo root or under a sub-project's
  prefix), and the prefix probe stopping at a path HEAD tracks (a file the
  node turned into a directory is the squash's own type change, not a
  collision); a conflict only on restorable paths resolves the same way
  instead of failing the merge -- a child's edit to its node parent's own
  contract among them, resolved to the parent's content, which the advance
  then carries back to the child -- while one beside a real conflict stays
  the operator's; and seed directories of other nodes the *user node* tracks
  draw a warning with a pasteable ``git rm -r`` remedy -- read from HEAD, so
  a ``--continue``'s staged seed is never one; only on the root, since a
  node target's branch legitimately carries its ancestors', descendants',
  and siblings' seeds; picked out by the root's own name plus the merging
  node's own and its descendants', so a ``--base`` merge into another tree's
  root still names a leaked copy of the node's seed as this merge's removal;
  and a same-named copy of the node's own seed under another project prefix
  among them, since the strip removes only the seed at the node's own
  prefix.
- **``merge.sh`` remedies** quote every path with ``printf %q``, so a line
  the operator pastes back into a shell stays whole under a repo path with a
  space -- and the CLI relays the script's stderr as written, so the quoting
  reaches the operator with single backslashes.
- **``merge.sh`` failure after the squash** judges "restored" by the target's
  state (clean, no squash marker) rather than the reset's exit status, which
  a ref lock fails after the index and worktree are already written; a
  squash git abandons after writing the index (a stale ``SQUASH_MSG`` it
  cannot write) is the same shape without a conflict -- the target was clean
  before, so the staged squash is reset and the markers cleared, and the
  failure is reported as one after staging, never as one before staging that
  would leave the squash for the target's next commit to absorb.
- **``merge.sh`` footprint check** refuses a squash that changes paths outside
  the node's scope roots (its project wiki excepted, every ``.fractal/`` path
  left out of the listing, and the worktree-root ``.gitattributes`` admitted
  only as init's own edit -- HEAD's content, leading blank line or trailing
  whitespace included, plus exactly the two lines the wiki tool appends, so
  a foreign line beside them is out of scope even on a target with no
  ``.gitattributes`` at all), naming the paths and both remedies, in both
  arms -- a fresh merge restores the target, a ``--continue`` leaves the
  staged squash -- unless ``--ignore-scope`` is passed; a repo-root node
  with no scope is unrestricted, a sub-project node with none is bounded to
  its project dir (its own wiki in, the repo-root wiki out), and one with
  roots to ``<project>/<root>``.
- **``merge.sh`` post-refresh no-op** re-checks the staged squash after the
  target's wiki index refresh: a squash the refresh fully reverts (a re-merge
  offering only regenerated wiki state, e.g. a legacy-tracked ``.wiki/cache``)
  lands on the designed "Nothing to merge" exit instead of dying on the empty
  index, advancing the child past a path the restore dropped beside that
  state exactly as the pre-refresh arm does -- and with the cache never
  entering history, disjoint sibling wiki work merges without a conflict at
  all.
- **``merge.sh`` pre-refresh no-op** clears git's squash markers on the fresh
  "Nothing to merge" exit: a re-merge whose squash staged only the stripped
  seed skips the commit that would consume ``SQUASH_MSG``, and left behind it
  prefills a later bare ``git commit`` in the target with the stale squash
  message; a marker path git cannot answer is skipped rather than resolved,
  as an empty word, to the target's worktree root.
- **``merge.sh --continue``** finishes an operator's hand-resolved squash after
  a conflicted merge with the merge's own tail -- ``.fractal/`` restore and
  seed strip, footprint check, commit, merge-base advance -- and refuses when
  no squash is in progress, unresolved conflicts remain, the staged squash
  comes from another node's branch, an unstaged edit to a tracked path
  remains (the restore would rewrite it), or the node has commits newer than
  the hand squash (the advance would record them as adjudicated away) -- the
  unstaged refusal names ``git add`` or ``checkout --`` per path (never a
  stash the restore would not see), and the footprint refusal names both
  ``--continue --ignore-scope`` and the redo of the squash. A resolution that
  keeps the target's content for everything the node offered stages nothing,
  and still finishes that tail minus the commit, so the target is left
  neither mid-squash nor primed to replay the resolved conflict. Every
  resolution reaches the node through the advance -- a third version, a
  restore of the fork-point content, an added file dropped from the squash --
  so a re-merge never undoes the decision; and a foreign ``.fractal/`` edit
  the hand squash carries is restored with the same warning a clean merge
  prints.
- **``fractal node merge``** around the script holds one merge lock per repo,
  so two sibling merges racing into one target both land instead of
  interleaving their index writes, and forwards a pid-targeted SIGINT to the
  script rather than killing it mid-squash, so the target never ends up with
  a half-merge staged and the merge event left active.
- **``merge.sh`` event start** arms its interrupt trap before the call that
  opens the merge event, so a process-group SIGINT landing while that call
  still runs closes the row it opened as failed, with the target untouched.
- **``merge.sh`` user-target verdict** comes from the caller: a root checked
  out in a linked worktree carries no node config to probe (its seed is
  self-ignored), so ``fractal node merge`` passes ``--user-target`` from the
  repo's record and the leaked-seed strip still runs there, while a direct
  call whose probe cannot read the config says so and treats the target as a
  node.
- **``fractal destroy``** takes the merge lock file down with the last tree's
  ``.worktrees/`` plumbing, so the directory never survives as untracked junk.
- **``delete.sh`` unmerged warning** surfaces commits the parent never absorbed
  on the automation path (the interactive prompt warns only the user) -- for
  non-ASCII file names too, which ``core.quotePath`` would otherwise C-quote
  into pathspecs that match nothing -- while excluding the generated wiki
  state that merge-up regenerates on the target.

Each test builds its own fresh repo (the merge/delete edges are destructive) and
shells the scripts directly with the CLI env so ``fractal`` resolves.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import subprocess
import time
from typing import Optional

import pytest

import fractal
from tests._helpers import _git

from .conftest import _await_progress, _cli_env, _fractal_bin, _reap_group, _run

__all__ = [
    'test_init_resolves_parent_worktree_under_a_space_path',
    'test_init_allows_a_repo_under_a_worktrees_path',
    'test_init_inherits_parent_skills_on_request',
    'test_init_reseeds_a_fresh_worktree_over_a_stale_seed_copy',
    'test_init_reseeds_over_a_partial_leaked_copy_with_a_profile_charter',
    'test_resume_reselects_the_recorded_backend',
    'test_headless_relaunch_vets_the_recorded_group',
    'test_headless_handoff_failure_records_no_backend',
    'test_tmux_relaunch_failure_keeps_the_backend_record',
    'test_kill_reaps_only_the_recorded_group_for_a_headless_node',
    'test_delete_running_guard_is_per_backend',
    'test_merge_preserves_a_target_edit_that_lands_during_the_merge',
    'test_merge_re_merges_an_iterating_child_without_conflict',
    'test_merge_re_merge_of_a_merged_node_is_a_no_op',
    'test_merge_re_merge_offering_only_the_seed_is_a_no_op',
    'test_merge_no_op_marker_clearing_never_touches_the_target_root',
    'test_merge_sibling_wiki_work_lands_without_conflict',
    'test_failed_merge_restore_removes_the_staged_child_additions',
    'test_merge_leaves_the_target_clean_when_scratch_creation_fails',
    'test_merge_advances_the_merge_base_past_a_refused_estate_file',
    'test_merge_skips_the_merge_base_advance_for_dirty_tracked_work',
    'test_merge_skips_the_advance_over_a_tracked_excluded_shape_edit',
    'test_merge_advance_records_the_target_tree_on_the_child',
    'test_merge_advance_keeps_the_childs_own_seed_intact',
    'test_merge_advance_into_a_node_target_takes_the_live_seed',
    'test_merge_advance_brings_the_targets_profiles_to_the_child',
    'test_merge_skips_the_advance_when_the_child_index_is_locked',
    'test_merge_skips_the_advance_when_reading_the_child_worktree_fails',
    'test_merge_skips_the_advance_over_a_private_ignored_file',
    'test_merge_skips_the_advance_over_a_path_the_target_renamed_onto',
    'test_merge_advance_moves_a_tracked_case_variant',
    'test_merge_advance_performs_a_type_change_the_target_made',
    'test_merge_fresh_no_op_advances_past_dropped_fractal_paths',
    'test_merge_fresh_no_op_advances_past_a_resolved_foreign_conflict',
    'test_merge_continue_finishes_a_hand_resolved_squash',
    'test_merge_continue_finishes_a_target_only_resolution',
    'test_merge_continue_lands_the_resolution_on_the_node',
    'test_merge_continue_restores_a_foreign_seed_edit_the_hand_squash_carries',
    'test_merge_continue_refuses_unstaged_target_edits',
    'test_merge_continue_refuses_commits_newer_than_the_squash',
    'test_merge_continue_refuses_unresolved_conflicts',
    'test_merge_continue_refuses_a_siblings_squash',
    'test_merge_leaves_the_targets_fractal_dir_as_it_is',
    'test_merge_refuses_over_any_untracked_file_the_squash_would_overwrite',
    'test_merge_refuses_over_a_seed_file_the_root_untracked_but_kept',
    'test_merge_refuses_over_an_ignored_copy_of_the_nodes_own_seed',
    'test_merge_lands_a_meta_nodes_edit_to_the_targets_seed',
    'test_merge_lands_a_fractal_scoped_nodes_profile_edit',
    'test_merge_lands_a_multi_root_scope_with_a_profile_root',
    'test_merge_resolves_a_conflict_on_the_nodes_own_seed',
    'test_merge_removes_an_own_seed_leaked_after_the_fork',
    'test_merge_refuses_a_mixed_conflict',
    'test_merge_warns_about_leaked_seed_dirs_on_the_target',
    'test_merge_leaves_a_same_named_seed_under_another_prefix_for_the_remedy',
    'test_merge_warns_about_its_own_seed_leaked_onto_another_trees_root',
    'test_merge_collision_guard_skips_a_seed_the_target_dropped_since_the_fork',
    'test_merge_collision_guard_skips_a_prefix_the_target_tracks',
    'test_merge_into_a_node_target_never_judges_its_ancestors_seed',
    'test_merge_into_a_node_target_keeps_its_tracked_descendant_seed',
    'test_merge_into_a_node_target_resolves_a_conflict_on_its_own_seed',
    'test_merge_remedies_quote_a_path_with_a_space',
    'test_merge_warnings_print_a_non_ascii_path_readably',
    'test_merge_refuses_a_squash_outside_the_nodes_scope',
    'test_merge_admits_init_attributes_over_a_targets_own_lines',
    'test_merge_strips_a_leaked_cross_project_descendant_seed_without_a_scope_refusal',
    'test_merge_continue_refuses_a_squash_outside_the_nodes_scope',
    'test_merge_bounds_a_sub_project_node_to_its_project',
    'test_merge_strips_a_nested_descendant_seed_from_a_no_ff_parent',
    'test_merge_reports_the_target_restored_past_a_ref_lock',
    'test_merge_resets_a_squash_git_aborted_after_staging',
    'test_merge_interrupt_during_the_commit_hook_finishes_the_merge',
    'test_merge_interrupt_after_a_skipped_advance_warns_once',
    'test_merge_interrupt_in_a_no_op_merges_event_close_reports_the_no_op',
    'test_merge_interrupt_during_the_event_start_fails_the_event',
    'test_merge_serializes_concurrent_sibling_merges',
    'test_merge_interrupt_never_leaves_a_half_merge',
    'test_merge_interrupt_after_the_squash_finishes_the_merge',
    'test_merge_footprint_refusal_quotes_a_path_with_a_space',
    'test_merge_into_a_root_checked_out_in_a_linked_worktree',
    'test_destroy_removes_the_merge_lock_with_the_worktrees_dir',
    'test_delete_warns_on_unmerged_commits',
    'test_delete_does_not_warn_after_squash_merge',
    'test_delete_does_not_warn_after_squash_merge_then_target_advances',
    'test_delete_warns_on_unmerged_non_ascii_work',
    'test_delete_warns_on_unmerged_wiki_page_work',
    'test_delete_does_not_warn_on_merge_regenerated_wiki_state',
]


# ------ init.sh: worktree paths with spaces


def test_init_resolves_parent_worktree_under_a_space_path(
    tmp_path: pathlib.Path,
) -> None:
    """A repo under a space-containing path still resolves the parent worktree.

    ``init.sh`` reads the parent worktree path from ``git worktree list
    --porcelain``; splitting on whitespace (``$2``) would truncate a path like
    ``.../my dir/repo`` at the space, leaving a derived parent node dir that does
    not exist and failing every child ``node init`` with "no fractal node".
    Reading the path with ``substr`` keeps it whole, so the child initializes.
    """
    # the space is in a *parent* directory (the repo's own name must be a valid
    # project identifier); the parent worktree path then contains a space
    repo = _init_tree(tmp_path / 'a space' / 'myrepo')
    result = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert result.returncode == 0, result.stderr
    # the child worktree was created (the parent worktree path resolved intact)
    assert (repo / '.worktrees' / 'main.task').is_dir(), result.stdout


# ------ init.sh: the worktree-anchor guard


def test_init_allows_a_repo_under_a_worktrees_path(
    tmp_path: pathlib.Path,
) -> None:
    """A repo living under an unrelated ``.worktrees`` path still spawns nodes.

    ``init.sh`` guards against anchoring a node inside a fractal worktree, but
    a guard matching ``.worktrees`` anywhere in the absolute path would reject
    outright a standalone repo that merely lives under a ``.worktrees``-named
    directory. The guard fires only for fractal's own worktrees: a
    ``.worktrees`` ancestor whose parent is itself a git repo.
    """
    # the .worktrees component is an ordinary directory, not a fractal
    # worktrees dir (its parent is no git repo)
    repo = _init_tree(tmp_path / '.worktrees' / 'myrepo')
    result = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert result.returncode == 0, result.stderr
    assert (repo / '.worktrees' / 'main.task').is_dir(), result.stdout


# ------ init.sh: parent skill inheritance


def test_init_inherits_parent_skills_on_request(tmp_path: pathlib.Path) -> None:
    """``--inherit=skills`` mirrors the parent's skill set; the default is the seed.

    ``init.sh`` seeds a child's skills from the package unless the spawn
    passes ``--inherit=skills``, which copies the parent node's ``skills/``
    wholesale: an edit reaches the child and a deleted skill stays deleted,
    never resurrected from the seed. The snapshot is a one-shot input --
    reaching an *existing* child requires ``--reset`` (a plain re-init is
    refused), and a reset re-inherits only when ``--inherit`` is passed
    again; without it the reset returns the child to the package seed.
    """
    repo = _init_tree(tmp_path / 'skillsrepo')
    # the user node has no skills dir; a top-level node seeds from the package
    init = _run(repo, 'node', 'init', 'parent', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    parent_wt = repo / '.worktrees' / 'main.parent'
    parent_skills = parent_wt / '.fractal' / 'main.parent' / 'skills'
    seed_skills = _scripts_dir().parent / '_node' / 'skills'
    assert sorted(p.name for p in parent_skills.iterdir()) == sorted(
        p.name for p in seed_skills.iterdir()
    )

    # the parent curates its set: appends guidance to one skill, drops another
    sentinel = 'Parent-curated guidance.'
    skill_md = parent_skills / 'fractal' / 'SKILL.md'
    skill_md.write_text(
        skill_md.read_text(encoding='utf-8') + f'\n{sentinel}\n',
        encoding='utf-8',
    )
    shutil.rmtree(parent_skills / 'radio')

    # a flagless child still seeds from the package -- the curated edit does
    # not arrive and the dropped skill is present
    stock_init = _run(parent_wt, 'node', 'init', 'kid', '--agent', 'claude', '--local')
    assert stock_init.returncode == 0, stock_init.stderr
    child_wt = repo / '.worktrees' / 'main.parent.kid'
    child_skills = child_wt / '.fractal' / 'main.parent.kid' / 'skills'
    child_md = (child_skills / 'fractal' / 'SKILL.md').read_text(encoding='utf-8')
    assert sentinel not in child_md
    assert (child_skills / 'radio').is_dir()

    # an --inherit=skills sibling copies the curated set wholesale -- the edit
    # arrives and the deleted skill is absent (no union with the seed)
    kin_init = _run(
        parent_wt,
        'node',
        'init',
        'kin',
        '--inherit',
        'skills',
        '--agent',
        'claude',
        '--local',
    )
    assert kin_init.returncode == 0, kin_init.stderr
    kin_wt = repo / '.worktrees' / 'main.parent.kin'
    kin_skills = kin_wt / '.fractal' / 'main.parent.kin' / 'skills'
    kin_md = (kin_skills / 'fractal' / 'SKILL.md').read_text(encoding='utf-8')
    assert sentinel in kin_md
    assert not (kin_skills / 'radio').exists()

    # the parent keeps evolving; a plain re-init is refused (the existing
    # child stays untouched), and a --reset without --inherit returns the
    # child to the package seed -- the snapshot never re-arms itself
    revised = 'Revised parent guidance.'
    skill_md.write_text(
        skill_md.read_text(encoding='utf-8') + f'\n{revised}\n',
        encoding='utf-8',
    )
    reinit = _run(parent_wt, 'node', 'init', 'kin', '--agent', 'claude', '--local')
    assert reinit.returncode != 0, reinit.stdout
    kin_md = (kin_skills / 'fractal' / 'SKILL.md').read_text(encoding='utf-8')
    assert revised not in kin_md
    reseed = _run(
        parent_wt, 'node', 'init', 'kin', '--reset', '--agent', 'claude', '--local'
    )
    assert reseed.returncode == 0, reseed.stderr
    kin_md = (kin_skills / 'fractal' / 'SKILL.md').read_text(encoding='utf-8')
    assert sentinel not in kin_md
    assert (kin_skills / 'radio').is_dir()
    # --reset --inherit=skills re-inherits the parent's latest state
    reset = _run(
        parent_wt,
        'node',
        'init',
        'kin',
        '--reset',
        '--inherit',
        'skills',
        '--agent',
        'claude',
        '--local',
    )
    assert reset.returncode == 0, reset.stderr
    kin_md = (kin_skills / 'fractal' / 'SKILL.md').read_text(encoding='utf-8')
    assert revised in kin_md


# ------ init.sh: reseeding over a tracked stale seed copy


def test_init_reseeds_a_fresh_worktree_over_a_stale_seed_copy(
    tmp_path: pathlib.Path,
) -> None:
    """A fresh worktree whose fork point carries the node's seed is reseeded.

    A parent that folds a child in with a real merge (as its PREPARE step
    does) tracks the child's seed on its branch. Deleted and spawned again
    under the same name, the new node forks from a tip that already carries
    ``.fractal/<branch>/config.json`` -- the dead incarnation's. Adopting
    those files as an existing seed would drop every flag of this init, so
    the init warns naming the stale copy and reseeds like ``--reset``: the
    config carries the new flags, and the copy goes whole -- a stray file
    beside the seed's own does not survive either.
    """
    repo = _init_tree(tmp_path / 'reseedrepo')
    init = _run(repo, 'node', 'init', 'parent', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    parent = repo / '.worktrees' / 'main.parent'
    _git(parent, 'add', '-A')
    _git(parent, 'commit', '-m', 'settle parent estate')
    # the parent spawns a child (the loop's spawn sets _NODE to the caller's
    # seed dir), folds it in for real, and deletes it
    node_dir = parent / '.fractal' / 'main.parent'
    spawn = _run(
        repo, 'node', 'init', 'c', '--agent', 'claude', '--local', _NODE=str(node_dir)
    )
    assert spawn.returncode == 0, spawn.stderr
    child = repo / '.worktrees' / 'main.parent.c'
    _git(child, 'add', '-A')
    _git(child, 'commit', '-m', 'child seed')
    _git(parent, 'merge', '--no-ff', '--no-edit', 'main.parent.c')
    deleted = _run(repo, 'node', 'delete', f'--path={child}', '--force')
    assert deleted.returncode == 0, deleted.stderr
    tracked = _git(parent, 'ls-files', '.fractal/main.parent.c').stdout
    assert '.fractal/main.parent.c/config.json' in tracked
    # a stray file lands beside the stale copy, under the same seed dir
    stray = parent / '.fractal' / 'main.parent.c' / 'stray.txt'
    stray.write_text('left beside the dead incarnation\n', encoding='utf-8')
    _git(parent, 'add', '-f', '.fractal/main.parent.c/stray.txt')
    _git(parent, 'commit', '-m', 'stray file under the stale seed')
    # the same name again, with flags the stale copy does not carry, so the
    # reseed is visible in config.json
    max_iters = 3
    again = _run(
        repo,
        'node',
        'init',
        'c',
        '--agent',
        'codex',
        '--max-iters',
        f'{max_iters}',
        '--local',
        _NODE=str(node_dir),
    )
    assert again.returncode == 0, again.stderr

    # the init warned that it reseeded the stale copy, and the config is
    # this init's, not the dead incarnation's
    seed = child / '.fractal' / 'main.parent.c'
    assert (
        f'Warning: main.parent already carries a seed for main.parent.c at {seed}'
        ' (a copy of an earlier node of this name); reseeding it'
    ) in again.stdout, (again.stdout, again.stderr)
    config = json.loads((seed / 'config.json').read_text(encoding='utf-8'))
    assert config['agent'] == 'codex'
    assert config['max_iters'] == max_iters
    assert not (seed / 'stray.txt').exists()


def test_init_reseeds_over_a_partial_leaked_copy_with_a_profile_charter(
    tmp_path: pathlib.Path,
) -> None:
    """A partial leaked copy -- a lone ``NODE.md`` -- is reseeded under a profile.

    A leak need not be whole: a root tracking only ``.fractal/<branch>/NODE.md``
    carries no ``config.json`` to mark a seed, yet a fresh worktree forking
    from it holds that stale charter -- and a charter is copied only when
    absent, so a ``--profile`` deployment charter would be dropped for the
    leaked text without a word. The reseed keys on the seed dir itself, so
    the init warns and the profile charter is the node's.
    """
    repo = _init_tree(tmp_path / 'partialleakrepo')
    # the root tracks a lone stale charter under the node's seed dir
    leaked = repo / '.fractal' / 'main.task' / 'NODE.md'
    leaked.parent.mkdir(parents=True)
    leaked.write_text('STALE LEAKED CHARTER\n', encoding='utf-8')
    _git(repo, 'add', '-f', '.fractal/main.task/NODE.md')
    _git(repo, 'commit', '-m', 'leaked main.task NODE.md')
    # a profile carrying a deployment-ready charter
    profile = repo / '.fractal' / 'profiles' / 'deploy' / 'NODE.md'
    profile.parent.mkdir(parents=True)
    profile.write_text(
        '# deploy\n\n## Instructions\n\nPROFILE CHARTER\n\n'
        '## Completion Requirements\n\nDone.\n',
        encoding='utf-8',
    )
    _git(repo, 'add', '-f', '.fractal/profiles')
    _git(repo, 'commit', '-m', 'deploy profile')
    init = _run(
        repo,
        'node',
        'init',
        'task',
        '--agent',
        'claude',
        '--local',
        '--profile',
        'deploy',
    )
    assert init.returncode == 0, init.stderr

    # the init warned that it reseeded the stale copy, and the charter is
    # the profile's, not the leaked text
    seed = repo / '.worktrees' / 'main.task' / '.fractal' / 'main.task'
    assert (
        f'Warning: main already carries a seed for main.task at {seed}'
        ' (a copy of an earlier node of this name); reseeding it'
    ) in init.stdout, (init.stdout, init.stderr)
    charter = (seed / 'NODE.md').read_text(encoding='utf-8')
    assert 'PROFILE CHARTER' in charter
    assert 'STALE' not in charter


# ------ resume.sh: backend selection


@pytest.mark.parametrize(
    argnames=('headless', 'listed'),
    argvalues=[(False, False), (True, False), (False, True), (True, True)],
    ids=['tmux', 'headless', 'tmux-parking', 'headless-collision'],
)
def test_resume_reselects_the_recorded_backend(
    tmp_path: pathlib.Path,
    headless: bool,
    listed: bool,
) -> None:
    """A paused node resumes through the backend recorded by its marker.

    ``resume.sh`` delegates the actual relaunch to ``start.sh``. A headless
    marker must add ``--headless`` before ``--resume``; without the marker the
    relaunch remains on tmux. The still-parking tmux guard is per-backend:
    without the marker a listed same-named session refuses the resume (retry,
    never kill), while a marker'd node ignores it -- a headless node owns no
    session, so the name belongs to another repo sharing the basename. A PATH
    shim records the final handoff without starting either runtime.
    """
    repo = _init_tree(tmp_path / f'resumerepo-{headless}')
    node_dir = repo / '.fractal' / 'main'
    if headless:
        (node_dir / '.headless').write_text('headless\n', encoding='utf-8')

    capture = tmp_path / f'resume-{headless}.txt'
    bindir = tmp_path / f'resume-bin-{headless}'
    bindir.mkdir()
    bash = bindir / 'bash'
    bash.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$@" >"$RESUME_CAPTURE"\n',
        encoding='utf-8',
    )
    bash.chmod(0o755)
    tmux = bindir / 'tmux'
    if listed:
        session = f'{repo.name} (main)'
        tmux.write_text(f"#!/bin/sh\nprintf '%s\\n' '{session}'\n", encoding='utf-8')
    else:
        tmux.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
    tmux.chmod(0o755)

    env = _cli_env()
    env['PATH'] = f'{bindir}{os.pathsep}{env["PATH"]}'
    env['RESUME_CAPTURE'] = f'{capture}'
    resume_sh = _scripts_dir() / 'resume.sh'
    real_bash = shutil.which('bash') or 'bash'
    result = subprocess.run(
        [real_bash, f'{resume_sh}', f'{repo}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=env,
    )

    if listed and not headless:
        # the guard refused before any handoff, with the retry wording
        assert result.returncode != 0, (result.stdout, result.stderr)
        assert 'still running or parking' in result.stderr, result.stderr
        assert not capture.exists()
        return
    assert result.returncode == 0, (result.stdout, result.stderr)
    expected = [f'{_scripts_dir() / "start.sh"}', f'{repo}']
    if headless:
        expected.append('--headless')
    expected.append('--resume')
    assert capture.read_text(encoding='utf-8').splitlines() == expected


# ------ start.sh + resume.sh: the headless second-launch vet


@pytest.mark.parametrize(
    argnames=('script', 'probe', 'message'),
    argvalues=[
        ('start.sh', 'live', 'headless node process already exists'),
        ('start.sh', 'unknown', 'process identity probe gave no answer'),
        ('resume.sh', 'live', 'the loop is still running or parking'),
    ],
    ids=['start-live', 'start-unknown', 'resume-live'],
)
def test_headless_relaunch_vets_the_recorded_group(
    tmp_path: pathlib.Path,
    script: str,
    probe: str,
    message: str,
) -> None:
    """A headless relaunch refuses while the recorded group answers the law.

    Both scripts hand their headless arm to ``node _launch``, which judges
    the ``.pgid`` record with the identity-checked liveness law: a live
    recorded group refuses the second launch (a resume with the
    retry-never-kill wording), and an identity probe with no answer refuses
    naming the ``ps`` check instead of reading ignorance as dead. The record
    and the group both survive the refusal.
    """
    repo = _init_tree(tmp_path / 'guardrepo')
    node_dir = repo / '.fractal' / 'main'
    (node_dir / '.headless').write_text('headless\n', encoding='utf-8')
    bindir = tmp_path / 'guard-bin'
    bindir.mkdir()
    if probe == 'unknown':
        # a ps that answers nothing leaves the group's identity unverifiable
        ps = bindir / 'ps'
        ps.write_text(
            '#!/bin/sh\necho "ps: unavailable" >&2\nexit 1\n',
            encoding='utf-8',
        )
        ps.chmod(0o755)
    env = _cli_env()
    env['PATH'] = f'{bindir}{os.pathsep}{env["PATH"]}'
    # a live group whose record postdates its leader, i.e. the node's own
    leader = subprocess.Popen(['sleep', '300'], start_new_session=True)
    pgid_file = node_dir / '.pgid'
    try:
        pgid_file.write_text(f'{leader.pid}\n', encoding='utf-8')
        args = ['--headless'] if script == 'start.sh' else []
        result = subprocess.run(
            ['bash', f'{_scripts_dir() / script}', f'{repo}', *args],
            cwd=f'{repo}',
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode != 0, (result.stdout, result.stderr)
        assert message in result.stderr, result.stderr
        assert pgid_file.read_text(encoding='utf-8') == f'{leader.pid}\n'
        assert leader.poll() is None
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        leader.wait()


# ------ start.sh: the headless backend record


def test_headless_handoff_failure_records_no_backend(
    tmp_path: pathlib.Path,
) -> None:
    """A failed headless handoff leaves no ``.headless`` marker behind.

    ``start.sh``'s headless arm delegates the marker write to
    ``node _launch``, which records it beside ``.pgid`` around the spawn --
    so a handoff that dies before launching anything records no backend, and
    a later bare relaunch never follows a marker for a launch that never
    happened. A ``fractal`` PATH shim stands in for the failing handoff.
    """
    repo = _init_tree(tmp_path / 'markerrepo')
    node_dir = repo / '.fractal' / 'main'
    bindir = tmp_path / 'marker-bin'
    bindir.mkdir()
    shim = bindir / 'fractal'
    shim.write_text(
        '#!/bin/sh\necho "launch refused" >&2\nexit 1\n',
        encoding='utf-8',
    )
    shim.chmod(0o755)
    env = _cli_env()
    env['PATH'] = f'{bindir}{os.pathsep}{env["PATH"]}'
    result = subprocess.run(
        ['bash', f'{_scripts_dir() / "start.sh"}', f'{repo}', '--headless'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert 'launch refused' in result.stderr, result.stderr
    assert not (node_dir / '.headless').exists()


def test_tmux_relaunch_failure_keeps_the_backend_record(
    tmp_path: pathlib.Path,
) -> None:
    """A failed tmux relaunch restores the pre-cleared ``.headless`` marker.

    Only a tmux launch that actually starts clears the marker: ``start.sh``
    clears it before ``new-session`` so a successful handoff re-records the
    backend, and a refused spawn must roll the record back -- a silently
    erased marker would flip the next bare continue onto tmux for a launch
    that never happened. A ``tmux`` PATH shim answers the version probe and
    lists no sessions, but refuses ``new-session``.
    """
    repo = _init_tree(tmp_path / 'rollbackrepo')
    node_dir = repo / '.fractal' / 'main'
    (node_dir / '.headless').write_text('headless\n', encoding='utf-8')
    bindir = tmp_path / 'rollback-bin'
    bindir.mkdir()
    tmux = bindir / 'tmux'
    tmux.write_text(
        '#!/bin/sh\n'
        'case "$1" in\n'
        "    -V) echo 'tmux 3.4' ;;\n"
        '    new-session) exit 1 ;;\n'
        'esac\n',
        encoding='utf-8',
    )
    tmux.chmod(0o755)
    env = _cli_env()
    env['PATH'] = f'{bindir}{os.pathsep}{env["PATH"]}'
    result = subprocess.run(
        ['bash', f'{_scripts_dir() / "start.sh"}', f'{repo}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert (node_dir / '.headless').read_text(encoding='utf-8') == 'headless\n'


# ------ kill.sh: the per-backend teardown


def test_kill_reaps_only_the_recorded_group_for_a_headless_node(
    tmp_path: pathlib.Path,
) -> None:
    """A headless kill reaps the recorded groups and never touches tmux.

    ``kill.sh``'s pane and session teardown is per-backend (mirroring
    ``resume.sh``'s guard): a headless node owns no session, so a same-named
    session from another repo sharing the basename must survive its kill --
    the pane lookup, session check, and session destroy are all skipped and
    the recorded ``.pgid`` group is the one reaped. A ``tmux`` PATH shim that
    lists the colliding session records every invocation; none may happen.
    """
    repo = _init_tree(tmp_path / 'killrepo')
    node_dir = repo / '.fractal' / 'main'
    (node_dir / '.headless').write_text('headless\n', encoding='utf-8')
    capture = tmp_path / 'kill-tmux.txt'
    bindir = tmp_path / 'kill-bin'
    bindir.mkdir()
    tmux = bindir / 'tmux'
    session = f'{repo.name} (main)'
    tmux.write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$*" >>"{capture}"\n'
        f"printf '%s\\n' '{session}'\n",
        encoding='utf-8',
    )
    tmux.chmod(0o755)
    env = _cli_env()
    env['PATH'] = f'{bindir}{os.pathsep}{env["PATH"]}'
    # a live group whose record postdates its leader, i.e. the node's own
    leader = subprocess.Popen(['sleep', '300'], start_new_session=True)
    pgid_file = node_dir / '.pgid'
    pgid_file.write_text(f'{leader.pid}\n', encoding='utf-8')
    try:
        result = subprocess.run(
            ['bash', f'{_scripts_dir() / "kill.sh"}', f'{repo}'],
            cwd=f'{repo}',
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        # the recorded group drew the TERM and the record handle is dropped
        assert leader.wait(timeout=10) != 0
        assert not pgid_file.exists()
        # tmux was never consulted -- the collision session survives
        assert not capture.exists()
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        leader.wait()


# ------ delete.sh: the per-backend running guard


@pytest.mark.parametrize(
    argnames='headless',
    argvalues=[False, True],
    ids=['tmux', 'headless'],
)
def test_delete_running_guard_is_per_backend(
    tmp_path: pathlib.Path,
    headless: bool,
) -> None:
    """A colliding session name blocks a delete only for a tmux node.

    ``delete.sh`` refuses teardown while the node's session is listed, but the
    guard is per-backend (mirroring ``kill.sh``): a headless node owns no
    session, so a same-named session from another repo sharing the basename
    must not refuse its delete. A ``tmux`` PATH shim lists the colliding name
    for both backends; only the tmux node's delete is refused.
    """
    repo = _init_tree(tmp_path / f'delrepo-{headless}')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    if headless:
        node_dir = worktree / '.fractal' / 'main.task'
        (node_dir / '.headless').write_text('headless\n', encoding='utf-8')
    bindir = tmp_path / f'del-bin-{headless}'
    bindir.mkdir()
    tmux = bindir / 'tmux'
    session = f'{repo.name} (main-task)'
    tmux.write_text(f"#!/bin/sh\nprintf '%s\\n' '{session}'\n", encoding='utf-8')
    tmux.chmod(0o755)
    env = _cli_env()
    env['PATH'] = f'{bindir}{os.pathsep}{env["PATH"]}'
    result = subprocess.run(
        ['bash', f'{_scripts_dir() / "delete.sh"}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=env,
    )

    if headless:
        # the name belongs to another repo's session -- the delete proceeds
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert not worktree.exists()
    else:
        # a tmux node's listed session refuses the teardown, node intact
        assert result.returncode != 0, (result.stdout, result.stderr)
        assert 'still running in tmux' in result.stderr, result.stderr
        assert worktree.exists()


# ------ merge.sh: an edit landing in the target during the merge


def test_merge_preserves_a_target_edit_that_lands_during_the_merge(
    tmp_path: pathlib.Path,
) -> None:
    """An edit to the target *during* a merge is refused, never lost or absorbed.

    ``merge.sh`` checks the target clean once at the top, then squashes. For a
    top-level node the target is the user's own root worktree, so an edit landing
    in the window before the squash must not be silently absorbed into the merge
    commit -- nor discarded by the recovery ``reset --hard``. The merge
    re-asserts cleanliness immediately before staging, so it refuses and the
    edit survives as an uncommitted change.

    The window is reproduced deterministically by shadowing ``fractal`` with a
    pass-through wrapper that dirties the target on the ``event _start merge``
    call ``merge.sh`` makes after its first clean check.
    """
    repo = _init_tree(tmp_path / 'mergerepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the child makes a real, non-empty change so the squash has content to merge
    (worktree / 'tracked.txt').write_text('original\nchild change\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child edits tracked')

    # a pass-through fractal shim that simulates a user editing the target the
    # instant the merge logs its start event (after merge.sh's first clean check)
    target_file = repo / 'tracked.txt'
    shim = _fractal_shim_dirtying(tmp_path, target_file, on='event _start merge')
    env = _cli_env()
    path = env['PATH']
    env['PATH'] = f'{shim}{os.pathsep}{path}'
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=env,
    )

    # the merge refused, and the edit that landed in the window survives intact --
    # neither committed into a squash nor wiped by a recovery reset --hard
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert 'WINDOW EDIT' in target_file.read_text(encoding='utf-8'), result.stderr
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() != 'merge main.task'


def test_merge_re_merges_an_iterating_child_without_conflict(
    tmp_path: pathlib.Path,
) -> None:
    """A child that keeps iterating on the same file re-merges cleanly.

    Squash records no ancestry, so a naive re-merge re-diffs from the original
    fork point and conflicts (add/add, then modify/modify) on every file the
    child re-touched. ``merge.sh`` advances the child's merge-base after each
    successful squash by recording the target's post-squash commit on the
    child, so the next merge diffs only the child's new work -- the re-merge
    succeeds and the target picks up the later content.
    """
    repo = _init_tree(tmp_path / 'remergerepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # first iteration: the child adds a file and squash-merges it into main
    (worktree / 'f.txt').write_text('line1\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child v1')
    merge_sh = _scripts_dir() / 'merge.sh'
    first = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert first.returncode == 0, first.stderr

    # second iteration: the child re-touches the same file and squash-merges again
    (worktree / 'f.txt').write_text('line1\nline2\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child v2')
    second = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the re-merge diffs only the new work, so it lands without a spurious
    # conflict and the target carries the child's later content
    assert second.returncode == 0, (second.stdout, second.stderr)
    merged = (repo / 'f.txt').read_text(encoding='utf-8')
    assert merged == 'line1\nline2\n', second.stderr


@pytest.mark.parametrize(
    argnames='dropped',
    argvalues=[False, True],
    ids=['wiki-state-only', 'with-a-dropped-path'],
)
def test_merge_re_merge_of_a_merged_node_is_a_no_op(
    tmp_path: pathlib.Path,
    dropped: bool,
) -> None:
    """A re-merge offering only regenerated wiki state exits 0 as a no-op.

    A tree whose baseline force-tracked the wiki tool's self-ignored
    ``.wiki/cache/`` churns that cache in every refresh (it embeds per-page
    mtimes), so a merged node that refreshes its wiki again offers nothing
    but cache bytes the target's own index refresh regenerates: the refresh
    reverts the staged squash to ``HEAD``, and a commit attempted anyway
    would die on the empty index -- a false hard failure on the designed
    "Nothing to merge" outcome. The merge re-checks the staged squash after
    the refresh and lands the no-op exit, leaving no squash state behind.
    That arm advances the child exactly as the pre-refresh one does: a
    write into a foreign seed the restore dropped beside the churn is an
    adjudication the child's merge-base moves past, while churn alone
    leaves the child's HEAD where it was.
    """
    repo = _init_tree(tmp_path / 'noopremergerepo')
    # a legacy baseline that force-tracked the derived cache past its ignore
    wiki_dir = repo / 'wiki'
    subprocess.run(
        ['wiki', 'update', f'--path={wiki_dir}'],
        capture_output=True,
        check=True,
        env=_cli_env(),
    )
    _git(repo, 'add', '-A')
    _git(repo, 'add', '-f', '--', 'wiki/.wiki/cache')
    _git(repo, 'commit', '-m', 'legacy baseline tracks the cache')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the child does real wiki work and refreshes the tracked cache -- fresh
    # worktree mtimes, so its cache bytes differ from the target's copy
    page = worktree / 'wiki' / 'topic.md'
    page.write_text(
        '---\nname: topic\ndesc: A topic page.\n---\n\n# topic\n\n***\n',
        encoding='utf-8',
    )
    wiki_dir = worktree / 'wiki'
    subprocess.run(
        ['wiki', 'update', f'--path={wiki_dir}'],
        capture_output=True,
        check=True,
        env=_cli_env(),
    )
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child wiki page')
    merge_sh = _scripts_dir() / 'merge.sh'
    first = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert first.returncode == 0, (first.stdout, first.stderr)
    merged_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    # the advance converged the child to the target, so only a fresh refresh
    # -- a page touched, its mtime re-embedded -- makes the cache differ again
    os.utime(page, None)
    subprocess.run(
        ['wiki', 'update', f'--path={wiki_dir}'],
        capture_output=True,
        check=True,
        env=_cli_env(),
    )
    foreign = worktree / '.fractal' / 'main.other' / 'x.md'
    if dropped:
        foreign.parent.mkdir()
        foreign.write_text('into a foreign seed\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'refresh the cache')
    child_head = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    # the churn is staged content the seed strip cannot empty, so only the
    # re-check after the refresh can find the no-op
    churn = subprocess.run(
        ['git', 'diff', '--quiet', 'main', '--', 'wiki/.wiki/cache'],
        cwd=f'{worktree}',
        capture_output=True,
        text=True,
    )
    assert churn.returncode == 1, churn.stderr

    # the re-merge stages only the cache churn, which the refresh reverts
    second = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    assert second.returncode == 0, (second.stdout, second.stderr)
    assert 'Nothing to merge' in second.stdout
    # no commit landed, and no squash state remains to fake a merge in
    # progress or prefill a bare git commit's message
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == merged_head
    assert _git(repo, 'status', '--porcelain').stdout == ''
    assert not (repo / '.git' / 'SQUASH_MSG').exists()
    assert not (repo / '.git' / 'MERGE_MSG').exists()
    # the dropped path was warned about and the child converged past it;
    # churn alone is no adjudication and leaves the child put
    if dropped:
        assert 'the merge removed, since' in second.stderr, second.stderr
        assert '.fractal/main.other/x.md' in second.stderr, second.stderr
        subject = _git(worktree, 'log', '-1', '--format=%s').stdout.strip()
        assert subject == 'merge main (post-squash)', (subject, second.stderr)
        assert not foreign.exists()
    else:
        assert 'the merge removed' not in second.stderr, second.stderr
        assert _git(worktree, 'rev-parse', 'HEAD').stdout.strip() == child_head
    assert _git(worktree, 'status', '--porcelain').stdout == ''


def test_merge_re_merge_offering_only_the_seed_is_a_no_op(
    tmp_path: pathlib.Path,
) -> None:
    """A re-merge whose squash stages only the stripped seed leaves no markers.

    Once a merge lands and the merge-base advances, the child's only diff
    against the target is its own seed, which the merge strips from the staged
    squash: the merge exits 0 on the designed "Nothing to merge" outcome before
    the index refresh. The squash still wrote ``SQUASH_MSG``, which only the
    skipped commit would consume -- left behind it fakes a squash still in
    progress and prefills a later bare ``git commit`` in the target with the
    stale squash message, so the no-op exit clears it.
    """
    repo = _init_tree(tmp_path / 'seednoopremergerepo')
    # settle the wiki so the merge's index refresh stages nothing of its own:
    # the seed must be the squash's whole offering to no-op before the refresh
    settle = subprocess.run(
        ['wiki', 'update', '--path', f'{repo}/wiki'],
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert settle.returncode == 0, settle.stderr
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'settle wiki')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the child commits real work; the first merge lands it (with the node's
    # inherited scaffolding) and advances the merge-base
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    merge_sh = _scripts_dir() / 'merge.sh'
    first = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert first.returncode == 0, (first.stdout, first.stderr)
    merged_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()

    # the re-merge stages only the seed, which the strip empties back out
    second = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    assert second.returncode == 0, (second.stdout, second.stderr)
    assert 'Nothing to merge' in second.stdout
    # no commit landed, and no squash state remains to fake a merge in
    # progress or prefill a bare git commit's message
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == merged_head
    assert not (repo / '.git' / 'SQUASH_MSG').exists()
    assert not (repo / '.git' / 'MERGE_MSG').exists()


def test_merge_no_op_marker_clearing_never_touches_the_target_root(
    tmp_path: pathlib.Path,
) -> None:
    """A squash marker path git cannot answer is skipped, never the target's root.

    The no-op arms clear git's squash markers at the paths ``rev-parse
    --git-path`` answers, and a failed answer is an empty word: joined onto
    the target's worktree dir it names the root itself, where a recursive
    remove would take the user's checkout, ``.git`` included. The clearing
    skips an empty answer, so a git that cannot resolve the markers leaves
    the target whole and the no-op still exits 0.

    The answers are failed with a ``git`` shim that refuses ``--git-path``
    and runs the real git for everything else.
    """
    repo = _init_tree(tmp_path / 'markerrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the scaffolding lands first, so the re-merge offers only the seed
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    merge_sh = _scripts_dir() / 'merge.sh'
    first = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert first.returncode == 0, (first.stdout, first.stderr)
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    bindir = _git_shim(
        tmp_path,
        'if [[ " $* " == *" --git-path "* ]]; then\n'
        '    echo "fatal: the marker paths cannot be resolved" >&2\n'
        '    exit 128\n'
        'fi\n',
    )
    env = _cli_env()
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    second = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=env,
    )

    # the no-op exits 0 with the target whole: its git dir, tracked files,
    # and the user node's estate all in place, and its HEAD unmoved
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert 'Nothing to merge' in second.stdout, second.stdout
    assert (repo / '.git').is_dir()
    assert (repo / 'tracked.txt').read_text(encoding='utf-8') == 'original\n'
    assert (repo / 'wiki' / '_index.md').is_file()
    assert (repo / '.fractal' / 'main' / 'config.json').is_file()
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == main_head
    assert _git(repo, 'status', '--porcelain').stdout == ''


def test_merge_sibling_wiki_work_lands_without_conflict(
    tmp_path: pathlib.Path,
) -> None:
    """Two siblings touching only their own wiki pages both merge cleanly.

    Each sibling's commit refreshes the generated wiki state, and the
    derived ``.wiki/cache/`` self-ignores -- the stage never force-tracks
    it -- so the only shared surface the merges touch is the ``_index.md``
    link block the merge driver already resolves: neither merge conflicts,
    both pages land on the target, and the cache never enters history.
    """
    repo = _init_tree(tmp_path / 'siblingrepo')
    # settle the wiki so each sibling inherits committed settings and the
    # merges' shared surface is only the generated link block
    settle = subprocess.run(
        ['wiki', 'update', '--path', f'{repo}/wiki'],
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert settle.returncode == 0, settle.stderr
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'settle wiki')
    # both siblings fork from the same tip, then work and merge in turn
    for name in ('alpha', 'beta'):
        init = _run(repo, 'node', 'init', name, '--agent', 'claude', '--local')
        assert init.returncode == 0, init.stderr
    merge_sh = _scripts_dir() / 'merge.sh'
    for name in ('alpha', 'beta'):
        worktree = repo / '.worktrees' / f'main.{name}'
        (worktree / 'wiki' / f'{name}.md').write_text(
            f'---\nname: {name}\ndesc: A {name} page.\n---\n\n# {name}\n\n***\n',
            encoding='utf-8',
        )
        wiki_dir = worktree / 'wiki'
        subprocess.run(
            ['wiki', 'update', f'--path={wiki_dir}'],
            capture_output=True,
            check=True,
            env=_cli_env(),
        )
        _git(worktree, 'add', '-A')
        _git(worktree, 'commit', '-m', f'{name} wiki page')
        result = subprocess.run(
            ['bash', f'{merge_sh}', f'{worktree}'],
            cwd=f'{repo}',
            capture_output=True,
            text=True,
            env=_cli_env(),
        )
        assert result.returncode == 0, (result.stdout, result.stderr)

    # both pages are on the target, and the derived cache never entered
    # history to manufacture a conflict between the disjoint offerings
    tracked = _git(repo, 'ls-files', 'wiki').stdout
    for name in ('alpha', 'beta'):
        assert (repo / 'wiki' / f'{name}.md').is_file()
        assert f'wiki/{name}.md' in tracked
    assert '.wiki/cache' not in tracked


def test_failed_merge_restore_removes_the_staged_child_additions(
    tmp_path: pathlib.Path,
) -> None:
    """A merge that fails after staging a child's new file leaves no residue.

    The squash stages (and writes to the target working tree) every file
    the child added; a downstream failure then restores with
    ``git reset --hard HEAD``. The restore must leave the target with no
    trace of the abandoned merge -- the child's staged addition removed,
    yet any pre-existing untracked file untouched -- so a later retry is
    never blocked by an "untracked working tree files would be
    overwritten" residue.

    The downstream failure is forced deterministically by shadowing
    ``wiki`` with a failing stub, so the post-squash index refresh fails.
    """
    repo = _init_tree(tmp_path / 'mergeresiduerepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the child adds a NEW file (untracked in the parent) so a stranded
    # residue would block the retry
    (worktree / 'newfile.md').write_text('# new\n\nchild work.\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child adds newfile')
    # a pre-existing untracked file in the parent must survive the restore
    (repo / 'keep.txt').write_text('operator scratch\n', encoding='utf-8')

    # shadow wiki with a failing stub so the post-squash index refresh fails
    stub = tmp_path / 'stub'
    stub.mkdir()
    wiki_stub = stub / 'wiki'
    wiki_stub.write_text('#!/usr/bin/env bash\nexit 1\n', encoding='utf-8')
    wiki_stub.chmod(0o755)
    env = _cli_env()
    path = env['PATH']
    env['PATH'] = f'{stub}{os.pathsep}{path}'
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=env,
    )

    # the merge failed and restored: the child's staged addition is gone,
    # the pre-existing untracked scratch file survives, and the parent
    # carries no commit for the merge
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert not (repo / 'newfile.md').exists(), (
        'staged child addition stranded as untracked residue',
        result.stderr,
    )
    assert (repo / 'keep.txt').read_text(encoding='utf-8') == 'operator scratch\n'
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() != 'merge main.task'


def test_merge_leaves_the_target_clean_when_scratch_creation_fails(
    tmp_path: pathlib.Path,
) -> None:
    """A scratch dir that cannot be made fails the merge before it touches the target.

    The tail's NUL-separated listings and the advance's private index live in
    a ``mktemp`` scratch dir. Made after the squash, a failure there would
    fire the restore over a squash the merge had already staged -- or, before
    the trap arms, strand it for the target's next commit to absorb. Made
    first, a failing ``mktemp`` leaves nothing to clean up: no commit, nothing
    staged, and no squash state.

    The failure is forced deterministically by shadowing ``mktemp`` with a
    failing stub.
    """
    repo = _init_tree(tmp_path / 'scratchrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()

    # shadow mktemp with a failing stub so the scratch dir cannot be made
    stub = tmp_path / 'stub'
    stub.mkdir()
    mktemp_stub = stub / 'mktemp'
    mktemp_stub.write_text(
        '#!/usr/bin/env bash\necho "mktemp: refused" >&2\nexit 1\n', encoding='utf-8'
    )
    mktemp_stub.chmod(0o755)
    env = _cli_env()
    path = env['PATH']
    env['PATH'] = f'{stub}{os.pathsep}{path}'
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=env,
    )

    # the merge failed before the squash: HEAD unmoved, nothing staged or left
    # on disk, and no squash state to fake a merge in progress
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == main_head
    assert _git(repo, 'status', '--porcelain').stdout == ''
    assert not (repo / '.git' / 'SQUASH_MSG').exists()


# ------ merge.sh: the merge-base advance and the commit content law


def test_merge_advances_the_merge_base_past_a_refused_estate_file(
    tmp_path: pathlib.Path,
) -> None:
    """A refused estate file never blocks the merge-base advance.

    The commit content law leaves a file a node parked in its estate (a
    ``.env``, a credential) untracked forever: ``fractal commit`` refuses it
    by name and ``commit --check`` reads clean with it present. A raw
    porcelain gate would count it as dirt, skip the advance after *every*
    merge, and re-diff each re-merge from the original fork point --
    spuriously conflicting on every already-merged file until an operator
    hand-resolves. The advance judges cleanliness the way the law does, so
    the base advances past the parked file and the re-merge lands clean.
    """
    repo = _init_tree(tmp_path / 'estaterepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # first iteration: real work, committed with init's scaffolding settled
    (worktree / 'f.txt').write_text('line1\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child v1')
    # the node parks a refused estate file -- the law never lets it commit
    estate = worktree / '.fractal' / 'main.task'
    (estate / '.env').write_text('SECRET=1\n', encoding='utf-8')
    merge_sh = _scripts_dir() / 'merge.sh'
    first = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert first.returncode == 0, first.stderr

    # the advance happened despite the parked file: no skip warning, and the
    # parent's merge commit is an ancestor of the child
    assert 'skipped advancing' not in first.stderr, first.stderr
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    ancestor = subprocess.run(
        ['git', 'merge-base', '--is-ancestor', main_head, 'HEAD'],
        cwd=f'{worktree}',
        capture_output=True,
        text=True,
    )
    assert ancestor.returncode == 0, ancestor.stderr

    # second iteration: the child re-touches the same file and merges again
    (worktree / 'f.txt').write_text('line1\nline2\n', encoding='utf-8')
    _git(worktree, 'add', 'f.txt')
    _git(worktree, 'commit', '-m', 'child v2')
    second = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the re-merge diffs only the new work -- no spurious conflict on the
    # already-merged file -- and the target carries the later content
    assert second.returncode == 0, (second.stdout, second.stderr)
    merged = (repo / 'f.txt').read_text(encoding='utf-8')
    assert merged == 'line1\nline2\n', second.stderr


def test_merge_skips_the_merge_base_advance_for_dirty_tracked_work(
    tmp_path: pathlib.Path,
) -> None:
    """Genuinely dirty tracked work still skips the merge-base advance.

    The law-based cleanliness gate must not over-correct: an uncommitted
    edit to a tracked file is work a commit would take, so the advance stays
    skipped (with the warning) and the mid-iteration child's branch and
    worktree are left untouched.
    """
    repo = _init_tree(tmp_path / 'dirtyrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # committed work, then an uncommitted edit on top -- a mid-iteration tree
    (worktree / 'f.txt').write_text('line1\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child v1')
    (worktree / 'f.txt').write_text('line1\nuncommitted\n', encoding='utf-8')
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the merge lands, but the advance is skipped and warned about: the
    # parent's merge commit is no ancestor of the child
    assert result.returncode == 0, result.stderr
    assert 'skipped advancing' in result.stderr, (result.stdout, result.stderr)
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    ancestor = subprocess.run(
        ['git', 'merge-base', '--is-ancestor', main_head, 'HEAD'],
        cwd=f'{worktree}',
        capture_output=True,
        text=True,
    )
    assert ancestor.returncode != 0, 'merge-base advanced past dirty tracked work'


@pytest.mark.parametrize(
    argnames='edited',
    argvalues=[True, False],
    ids=['edited-on-disk', 'committed-clean'],
)
def test_merge_skips_the_advance_over_a_tracked_excluded_shape_edit(
    tmp_path: pathlib.Path,
    edited: bool,
) -> None:
    """An edit to a tracked file of a shape the law excludes still skips the advance.

    The commit content law never stages the runtime shapes -- a
    ``config.json.lock``, a ``.status`` -- so ``fractal commit --check``
    reads clean over an edit to one a node force-tracked, and the advance's
    ``reset --hard`` would write the target's copy over that edit. The
    advance asks git for the tracked diff as well: such an edit skips it
    with the uncommitted-changes warning and leaves the child's HEAD and the
    on-disk edit alone, while the same file committed clean lets the
    advance run.
    """
    repo = _init_tree(tmp_path / 'lockshaperepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the node force-tracks its config write lock, a shape the law excludes
    lock = worktree / '.fractal' / 'main.task' / 'config.json.lock'
    lock.write_text('', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'add', '-f', '--', '.fractal/main.task/config.json.lock')
    _git(worktree, 'commit', '-m', 'settle node scaffolding, lock tracked')
    if edited:
        lock.write_text('held\n', encoding='utf-8')
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', 'f.txt')
    _git(worktree, 'commit', '-m', 'child work')
    # the law reads the tree clean either way
    check = _run(worktree, 'commit', '--check')
    assert check.returncode == 0, (check.stdout, check.stderr)
    child_head = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the merge landed either way
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    assert (repo / 'f.txt').read_text(encoding='utf-8') == 'child work\n'
    if edited:
        # the advance was skipped and warned about; the edit is untouched
        assert 'skipped advancing' in result.stderr, result.stderr
        assert 'its worktree has uncommitted changes' in result.stderr, result.stderr
        assert _git(worktree, 'rev-parse', 'HEAD').stdout.strip() == child_head
        assert lock.read_text(encoding='utf-8') == 'held\n'
    else:
        # committed clean, the advance ran and left the tree clean
        assert 'skipped advancing' not in result.stderr, result.stderr
        subject = _git(worktree, 'log', '-1', '--format=%s').stdout.strip()
        assert subject == 'merge main (post-squash)', (subject, result.stderr)
        assert lock.read_text(encoding='utf-8') == ''
        assert _git(worktree, 'status', '--porcelain').stdout == ''


# ------ merge.sh: the merge-base advance adopts the target's tree


def test_merge_advance_records_the_target_tree_on_the_child(
    tmp_path: pathlib.Path,
) -> None:
    """The merge-base advance lands the target's post-squash tree on the child.

    Squash records no ancestry, so the merge advances the child's merge-base
    by recording the target's post-squash commit on the child. An advance
    that grafted ancestry without content would leave every file the target
    changed since the fork "changed on the child's side" relative to the new
    base, so the next three-way merge takes the child's stale copy whenever
    the target does not touch the file again. The advance is a real
    two-parent commit (child, target) whose tree is the target's: outside its
    own seed the child holds exactly what the target holds.

    Were the child's tree left as it was, a file the target changed since
    the fork would differ from the new base on the child's side only, and
    the second squash would carry the child's stale copy onto the target as
    if the child had edited it -- silently, with a clean merge. The advance
    adopts the target's content, so the second squash offers only the
    child's new work and the target's change stays.
    """
    repo = _init_tree(tmp_path / 'advancerepo')
    (repo / 'README.md').write_text('v1\n', encoding='utf-8')
    _git(repo, 'add', 'README.md')
    _git(repo, 'commit', '-m', 'readme v1')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the target moves on in a file the child never touches
    (repo / 'README.md').write_text('v2\n', encoding='utf-8')
    _git(repo, 'add', 'README.md')
    _git(repo, 'commit', '-m', 'readme v2')
    # the child's work, with init's scaffolding settled so the advance runs
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    child_head = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'skipped advancing' not in result.stderr, result.stderr

    # the advance is a two-parent commit on the child, parents (child, target),
    # under the distinctive subject that keeps it apart from the node's own
    # merges of the target
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    parents = _git(worktree, 'log', '-1', '--format=%P').stdout.split()
    assert parents == [child_head, main_head]
    subject = _git(worktree, 'log', '-1', '--format=%s').stdout.strip()
    assert subject == 'merge main (post-squash)'
    # its tree is the target's everywhere outside the child's own seed, on the
    # branch and in the worktree
    converged = subprocess.run(
        ['git', 'diff', '--quiet', 'main', 'HEAD', '--', '.', ':!.fractal'],
        cwd=f'{worktree}',
        capture_output=True,
        text=True,
    )
    assert converged.returncode == 0, converged.stdout
    assert (worktree / 'README.md').read_text(encoding='utf-8') == 'v2\n'

    # second iteration: more child work, and neither side touches the README
    (worktree / 'f.txt').write_text('child v2\n', encoding='utf-8')
    _git(worktree, 'add', 'f.txt')
    _git(worktree, 'commit', '-m', 'child v2')
    second = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the re-merge lands the new work and leaves the target's README alone
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert (repo / 'f.txt').read_text(encoding='utf-8') == 'child v2\n'
    assert (repo / 'README.md').read_text(encoding='utf-8') == 'v2\n', second.stderr


def test_merge_advance_keeps_the_childs_own_seed_intact(
    tmp_path: pathlib.Path,
) -> None:
    """The advance grafts the child's own and descendant seeds back unchanged.

    The target never carries the merging node's machinery -- the squash
    strips the node's own ``.fractal/<branch>/`` and any descendant seed -- so
    a verbatim adoption of the target's tree would delete the child's live
    seed, and a stale copy of that seed the target happens to track (a hand
    merge leaked it) must never win over the live one. The advance takes
    those directories from the child's own HEAD, so the seed trees are
    unchanged byte for byte while the stale copy leaves the target.
    """
    repo = _init_tree(tmp_path / 'seedrepo')
    # a stale copy of the node's seed, leaked onto the target before the fork
    stale = repo / '.fractal' / 'main.task' / 'NODE.md'
    stale.parent.mkdir(parents=True)
    stale.write_text('# stale contract\n', encoding='utf-8')
    _git(repo, 'add', '.fractal/main.task')
    _git(repo, 'commit', '-m', 'leaked seed copy')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the live seed diverges from the leaked copy, and the child carries a
    # descendant's seed (as a parent that merged a child of its own does)
    (worktree / '.fractal' / 'main.task' / 'NODE.md').write_text(
        '# live contract\n',
        encoding='utf-8',
    )
    descendant = worktree / '.fractal' / 'main.task.sub'
    descendant.mkdir()
    (descendant / 'NODE.md').write_text('# descendant contract\n', encoding='utf-8')
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    seed_trees = {
        name: _git(worktree, 'rev-parse', f'HEAD:.fractal/{name}').stdout.strip()
        for name in ('main.task', 'main.task.sub')
    }
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'skipped advancing' not in result.stderr, result.stderr
    # the stale copy is named as this node's own leak, the one the merge removes
    assert (
        'tracks seeds of main.task or its descendants, leaked by an earlier merge'
        in result.stderr
    ), result.stderr
    assert '.fractal/main.task; this merge removes them' in result.stderr, result.stderr

    # the advanced branch and the worktree carry the child's own seed trees,
    # byte for byte
    for name, tree in seed_trees.items():
        assert (
            _git(worktree, 'rev-parse', f'HEAD:.fractal/{name}').stdout.strip() == tree
        )
    live = (worktree / '.fractal' / 'main.task' / 'NODE.md').read_text(encoding='utf-8')
    assert live == '# live contract\n'
    # and the target tracks neither the stale copy nor the descendant seed
    tracked = _git(
        repo, 'ls-files', '.fractal/main.task', '.fractal/main.task.sub'
    ).stdout
    assert tracked.strip() == ''
    assert not (repo / '.fractal' / 'main.task').exists()


def test_merge_advance_into_a_node_target_takes_the_live_seed(
    tmp_path: pathlib.Path,
) -> None:
    """The advance into a parent tracking the child's seed keeps the live seed.

    A parent node folds its child in with a real merge (its PREPARE step),
    so its branch tracks a copy of the child's seed, and the child's upward
    squash never changes that copy -- the restore returns every ``.fractal/``
    path to the target's HEAD, and a node target is never stripped. The
    advance starts from the target's tree, so a verbatim adoption would hand
    the child that stale copy: a seed file the child dropped since the fold
    would come back. The advance strips the target's copy of the child's
    seed before grafting the child's own from its HEAD, so the child
    converges to the target everywhere but its seed, which stays byte for
    byte its live one, with its worktree clean.
    """
    repo = _init_tree(tmp_path / 'foldrepo')
    init = _run(repo, 'node', 'init', 'p', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    parent = repo / '.worktrees' / 'main.p'
    _git(parent, 'add', '-A')
    _git(parent, 'commit', '-m', 'settle node scaffolding')
    # the parent spawns a child (the loop's spawn sets _NODE to the caller's
    # seed dir), which settles its own scaffolding
    node_dir = parent / '.fractal' / 'main.p'
    spawn = _run(
        repo, 'node', 'init', 'c', '--agent', 'claude', '--local', _NODE=str(node_dir)
    )
    assert spawn.returncode == 0, spawn.stderr
    child = repo / '.worktrees' / 'main.p.c'
    _git(child, 'add', '-A')
    _git(child, 'commit', '-m', 'settle node scaffolding')
    # the parent folds the child in with a real merge, tracking its seed
    _git(parent, 'merge', '--no-ff', '--no-edit', 'main.p.c')
    # the child drops a seed file it can live without and commits work,
    # leaving its tree clean for the advance
    dropped = child / '.fractal' / 'main.p.c' / 'skills' / 'radio' / 'SKILL.md'
    _git(child, 'rm', '--quiet', f'{dropped.relative_to(child)}')
    (child / 'c.txt').write_text('child work\n', encoding='utf-8')
    _git(child, 'add', 'c.txt')
    _git(child, 'commit', '-m', 'drop a skill, add work')
    live_seed = _git(child, 'rev-parse', 'HEAD:.fractal/main.p.c').stdout.strip()
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{child}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the work landed on the parent, whose fold-tracked copy of the seed is
    # left as it was
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'skipped advancing' not in result.stderr, result.stderr
    assert _git(parent, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.p.c'
    assert (parent / 'c.txt').read_text(encoding='utf-8') == 'child work\n'
    assert (
        parent / '.fractal' / 'main.p.c' / 'skills' / 'radio' / 'SKILL.md'
    ).is_file()
    # the advance moved the child onto the parent's tree with its own live
    # seed grafted back -- the dropped file stays dropped -- and left it clean
    subject = _git(child, 'log', '-1', '--format=%s').stdout.strip()
    assert subject == 'merge main.p (post-squash)'
    grafted = _git(child, 'rev-parse', 'HEAD:.fractal/main.p.c').stdout.strip()
    assert grafted == live_seed
    assert not dropped.exists()
    assert _git(child, 'status', '--porcelain').stdout == ''


def test_merge_advance_brings_the_targets_profiles_to_the_child(
    tmp_path: pathlib.Path,
) -> None:
    """A profile the target gained after the fork reaches the child on the advance.

    ``.fractal/profiles/<name>/`` is read from the root worktree and travels
    with the target's tree like any other file, so a child forked before it
    existed receives it only through the advance -- which carries the
    target's content, not just its ancestry.
    """
    repo = _init_tree(tmp_path / 'profilerepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the target gains a profile after the fork
    profile = repo / '.fractal' / 'profiles' / 'p' / 'steps' / '00-X.md'
    profile.parent.mkdir(parents=True)
    profile.write_text('# X\n', encoding='utf-8')
    _git(repo, 'add', '.fractal/profiles')
    _git(repo, 'commit', '-m', 'add profile')
    # the child's work, with init's scaffolding settled so the advance runs
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'skipped advancing' not in result.stderr, result.stderr

    # the profile is on the child's branch and in its worktree
    tracked = _git(worktree, 'ls-files', '.fractal/profiles').stdout
    assert '.fractal/profiles/p/steps/00-X.md' in tracked
    child_copy = worktree / '.fractal' / 'profiles' / 'p' / 'steps' / '00-X.md'
    assert child_copy.read_text(encoding='utf-8') == '# X\n'


@pytest.mark.parametrize(
    argnames='lock',
    argvalues=['index.lock', 'refs/heads/main.task.lock'],
    ids=['index-lock', 'ref-lock'],
)
def test_merge_skips_the_advance_when_the_child_index_is_locked(
    tmp_path: pathlib.Path,
    lock: str,
) -> None:
    """A child index or ref another process holds skips the advance, never the merge.

    The advance rewrites the child's branch and worktree after the squash has
    already landed on the target, so a failure there -- an ``index.lock`` or
    the branch's ref lock held by another git process, which the commit
    content law's cleanliness check tolerates -- must neither fail the merge
    nor leave the child half-moved. ``reset --hard`` writes the index and
    worktree before it moves the ref, so a ref lock in particular leaves the
    target's tree checked out against the child's old HEAD; the merge rolls
    the worktree back, warns that the advance was skipped, leaves the child's
    HEAD where it was, and still exits 0 with the target's commit in place.
    """
    repo = _init_tree(tmp_path / 'lockedrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the target moves on after the fork, so an advance that reaches the
    # child's worktree is visible there
    (repo / 'README.md').write_text('target moved on\n', encoding='utf-8')
    _git(repo, 'add', 'README.md')
    _git(repo, 'commit', '-m', 'target readme')
    (worktree / 'tracked.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    child_head = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    # another git process holds the lock for the whole merge (--git-path
    # resolves the per-worktree index and the shared branch ref alike)
    lock_path = pathlib.Path(
        _git(worktree, 'rev-parse', '--git-path', lock).stdout.strip()
    )
    if not lock_path.is_absolute():
        lock_path = worktree / lock_path
    lock_path.touch()
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    lock_path.unlink()

    # the merge landed and warned; the child's ref never moved, and its
    # worktree is its own HEAD's -- clean, without the target's file
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'skipped advancing' in result.stderr, (result.stdout, result.stderr)
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    assert (repo / 'tracked.txt').read_text(encoding='utf-8') == 'child work\n'
    assert _git(worktree, 'rev-parse', 'HEAD').stdout.strip() == child_head
    assert _git(worktree, 'status', '--porcelain').stdout == ''
    assert (worktree / 'tracked.txt').read_text(encoding='utf-8') == 'child work\n'
    assert not (worktree / 'README.md').exists()


def test_merge_skips_the_advance_when_reading_the_child_worktree_fails(
    tmp_path: pathlib.Path,
) -> None:
    """A failed read of the child's worktree skips the advance, never the merge.

    The advance opens by asking the child's worktree which branch it has
    checked out, after the squash has already landed on the target. A read
    git cannot answer -- the worktree gone or corrupted since the merge's
    own branch read -- skips the advance with a warning naming the failed
    read, reports the landed squash with exit 0, and closes the event
    completed; an unguarded read would abort the script past the point of
    no return, the event left open and the landed squash unreported.

    The read is failed with a ``git`` shim that counts the script's own
    ``rev-parse --abbrev-ref HEAD`` calls in the child's worktree -- the
    first is the merge's branch read, the second the advance's -- and fails
    the second, running the real git for everything else; the CLI's own
    reads run under python, not bash, and pass straight through.
    """
    repo = _init_tree(tmp_path / 'readfailrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    child_head = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    reads = tmp_path / 'git_shim' / 'reads'
    bindir = _git_shim(
        tmp_path,
        f'if [[ "$*" == "-C {worktree} rev-parse --abbrev-ref HEAD" '
        '&& "$(ps -o comm= -p "$PPID")" == *bash* ]]; then\n'
        f'    echo "$*" >> "{reads}"\n'
        f'    if [[ "$(wc -l < "{reads}")" -ge 2 ]]; then\n'
        f'        echo "fatal: not a git repository: {worktree}" >&2\n'
        '        exit 128\n'
        '    fi\n'
        'fi\n',
    )
    env = _cli_env()
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=env,
    )

    # the shim failed exactly the advance's read; the merge landed and was
    # reported, the skip named the failed read, and the child never moved
    assert len(reads.read_text(encoding='utf-8').splitlines()) == 2
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'Squash-merged main.task into main' in result.stdout, result.stdout
    assert 'skipped advancing' in result.stderr, result.stderr
    assert "reading main.task's worktree failed" in result.stderr, result.stderr
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    assert (repo / 'f.txt').read_text(encoding='utf-8') == 'child work\n'
    assert _git(worktree, 'rev-parse', 'HEAD').stdout.strip() == child_head
    assert _git(worktree, 'status', '--porcelain').stdout == ''
    activity = _run(repo, 'node', 'activity', 'main', '--json')
    assert activity.returncode == 0, activity.stderr
    rows = json.loads(activity.stdout)
    merges = [row['status'] for row in rows if row['event'] == 'merge']
    assert merges == ['completed'], rows


@pytest.mark.parametrize(
    argnames=('ignored', 'private', 'landed', 'named'),
    argvalues=[
        # the same file on both sides
        ('local.env', 'local.env', 'local.env', 'local.env'),
        # a private directory where the target adds a file
        ('build/', 'build/out.bin', 'build', 'build'),
        # a private file where the target adds a directory
        ('out', 'out', 'out/report.txt', 'out'),
        # a name differing only by case
        ('LOCAL.ENV', 'LOCAL.ENV', 'local.env', 'local.env'),
    ],
    ids=['file', 'dir-in-the-way', 'file-in-the-way', 'case-alias'],
)
def test_merge_skips_the_advance_over_a_private_ignored_file(
    tmp_path: pathlib.Path,
    ignored: str,
    private: str,
    landed: str,
    named: str,
) -> None:
    """An ignored path the target now tracks skips the advance and stays private.

    The advance ends in ``reset --hard``, which writes every path the target
    tracks over an untracked or ignored file of the same name in the child's
    worktree. A sibling that force-added its own copy of an ignored path
    lands it on the target, so the next sibling's advance would silently
    replace that sibling's private copy with the first's. The guard probes
    the disk rather than comparing listings, so every shape of collision
    counts: the same file, a private directory where the target adds a file,
    a private file where it adds a directory, and a name differing only by
    case on a case-insensitive filesystem. The merge lands, and the advance
    is skipped with a warning naming the path in the way, leaving the
    private copy and the child's HEAD untouched.
    """
    # a case-only alias collides only where the filesystem folds case
    if private != landed and private.casefold() == landed.casefold():
        _require_case_folding(tmp_path)
    repo = _init_tree(tmp_path / 'privaterepo')
    (repo / '.gitignore').write_text(f'{ignored}\n', encoding='utf-8')
    _git(repo, 'add', '.gitignore')
    _git(repo, 'commit', '-m', f'ignore {ignored}')
    for name in ('a', 'b'):
        init = _run(repo, 'node', 'init', name, '--agent', 'claude', '--local')
        assert init.returncode == 0, init.stderr
        _git(repo / '.worktrees' / f'main.{name}', 'add', '-A')
        _git(repo / '.worktrees' / f'main.{name}', 'commit', '-m', 'settle scaffolding')
    sibling = repo / '.worktrees' / 'main.a'
    worktree = repo / '.worktrees' / 'main.b'
    # the first sibling force-tracks its copy past the ignore and lands it
    (sibling / landed).parent.mkdir(parents=True, exist_ok=True)
    (sibling / landed).write_text('A-secret\n', encoding='utf-8')
    _git(sibling, 'add', '-f', landed)
    _git(sibling, 'commit', '-m', 'track a secret')
    merge_sh = _scripts_dir() / 'merge.sh'
    first = subprocess.run(
        ['bash', f'{merge_sh}', f'{sibling}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert first.returncode == 0, (first.stdout, first.stderr)
    # the second holds its own private copy, ignored, beside committed work
    (worktree / private).parent.mkdir(parents=True, exist_ok=True)
    (worktree / private).write_text('B-private\n', encoding='utf-8')
    (worktree / 'b.txt').write_text('b work\n', encoding='utf-8')
    _git(worktree, 'add', 'b.txt')
    _git(worktree, 'commit', '-m', 'b work')
    child_head = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the merge landed; the advance was skipped naming the path in the way,
    # and the private copy and the child's HEAD are untouched
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'Squash-merged' in result.stdout
    assert 'skipped advancing' in result.stderr, result.stderr
    assert f'now tracks: {named};' in result.stderr, result.stderr
    assert (worktree / private).read_text(encoding='utf-8') == 'B-private\n'
    assert _git(worktree, 'rev-parse', 'HEAD').stdout.strip() == child_head
    assert _git(worktree, 'status', '--porcelain').stdout == ''


def test_merge_skips_the_advance_over_a_path_the_target_renamed_onto(
    tmp_path: pathlib.Path,
) -> None:
    """A target rename onto a child's private path is a collision like any add.

    The clobber guard lists the paths the advance would add to the child's
    worktree with rename detection off: a file the target moved onto a path
    the child holds ignored is a rename to git, but to the child's disk it is
    a new path the ``reset --hard`` would write over its private copy. The
    advance is skipped naming the destination, and the private copy and the
    child's HEAD are untouched.
    """
    repo = _init_tree(tmp_path / 'renamerepo')
    (repo / '.gitignore').write_text('private/\n', encoding='utf-8')
    (repo / 'public').mkdir()
    (repo / 'public' / 'data.txt').write_text('shared data\n', encoding='utf-8')
    _git(repo, 'add', '.gitignore', 'public')
    _git(repo, 'commit', '-m', 'public data, private ignored')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    # the target moves the file onto the ignored path
    (repo / 'private').mkdir()
    _git(repo, 'mv', 'public/data.txt', 'private/data.txt')
    _git(repo, 'commit', '-m', 'move the data private')
    # the child holds its own private copy there, ignored, beside committed work
    (worktree / 'private').mkdir()
    (worktree / 'private' / 'data.txt').write_text('B-private\n', encoding='utf-8')
    (worktree / 'b.txt').write_text('b work\n', encoding='utf-8')
    _git(worktree, 'add', 'b.txt')
    _git(worktree, 'commit', '-m', 'b work')
    child_head = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the merge landed; the advance was skipped naming the rename's
    # destination, and the private copy and the child's HEAD are untouched
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'Squash-merged' in result.stdout
    assert 'skipped advancing' in result.stderr, result.stderr
    assert 'now tracks: private/data.txt;' in result.stderr, result.stderr
    private = (worktree / 'private' / 'data.txt').read_text(encoding='utf-8')
    assert private == 'B-private\n'
    assert _git(worktree, 'rev-parse', 'HEAD').stdout.strip() == child_head
    assert _git(worktree, 'status', '--porcelain').stdout == ''
    assert (repo / 'b.txt').read_text(encoding='utf-8') == 'b work\n'


def test_merge_advance_moves_a_tracked_case_variant(tmp_path: pathlib.Path) -> None:
    """A tracked path differing only by case from a target add is no collision.

    On a case-insensitive filesystem the disk probe for a path the target
    adds hits the child's file of the other spelling -- one the child's
    index tracks, when the target replaced ``Readme.md`` with an unrelated
    ``README.md``. The reset renames such a file correctly, so the guard
    asks the child's index (case-folded, literal) before calling the hit a
    collision: the advance runs and the child converges to the target's
    spelling and content.
    """
    # a case-only alias is a disk hit only where the filesystem folds case
    _require_case_folding(tmp_path)
    repo = _init_tree(tmp_path / 'caserepo')
    (repo / 'Readme.md').write_text('mixed case\n', encoding='utf-8')
    _git(repo, 'add', 'Readme.md')
    _git(repo, 'commit', '-m', 'mixed-case readme')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    # the target replaces the file with an unrelated one of the other spelling
    _git(repo, 'rm', '--quiet', 'Readme.md')
    (repo / 'README.md').write_text('upper case\n', encoding='utf-8')
    _git(repo, 'add', 'README.md')
    _git(repo, 'commit', '-m', 'replace the readme')
    (worktree / 'b.txt').write_text('b work\n', encoding='utf-8')
    _git(worktree, 'add', 'b.txt')
    _git(worktree, 'commit', '-m', 'b work')
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the advance ran: the child is on the post-squash commit, tracking and
    # holding the target's spelling with the target's content
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'skipped advancing' not in result.stderr, result.stderr
    subject = _git(worktree, 'log', '-1', '--format=%s').stdout.strip()
    assert subject == 'merge main (post-squash)'
    tracked = _git(worktree, 'ls-files').stdout.splitlines()
    assert 'README.md' in tracked, tracked
    assert 'Readme.md' not in tracked, tracked
    assert (worktree / 'README.md').read_text(encoding='utf-8') == 'upper case\n'
    assert _git(worktree, 'status', '--porcelain').stdout == ''


@pytest.mark.parametrize(
    argnames=('before', 'after'),
    argvalues=[('out', 'out/x'), ('out/x', 'out')],
    ids=['file-to-dir', 'dir-to-file'],
)
def test_merge_advance_performs_a_type_change_the_target_made(
    tmp_path: pathlib.Path,
    before: str,
    after: str,
) -> None:
    """A path the target turned from file to directory, or back, is no collision.

    The clobber guard probes the child's disk for every path the advance adds,
    and a type change on the target hits the child's own tracked copy: a
    directory where the target now has a file, or a file at the prefix under
    which it now has a directory. Both are changes the ``reset --hard``
    performs, so the guard asks the child's tree before calling the hit a
    collision -- a path it tracks at or under the hit, or a prefix it tracks,
    is not in the way -- and the advance runs, leaving the child with the
    target's shape. The index listings behind that answer are read as text,
    so no NUL byte leaks into the script's own stderr on the way -- the
    case-folded probe a case-insensitive checkout adds included.
    """
    repo = _init_tree(tmp_path / 'typechangeadvancerepo')
    (repo / before).parent.mkdir(parents=True, exist_ok=True)
    (repo / before).write_text(f'{before}\n', encoding='utf-8')
    _git(repo, 'add', 'out')
    _git(repo, 'commit', '-m', f'track {before}')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    # the child's index folds case, so the guard's case-folded probe runs too
    _git(worktree, 'config', 'core.ignorecase', 'true')
    # the target swaps the shape under out
    _git(repo, 'rm', '-r', '--quiet', 'out')
    (repo / after).parent.mkdir(parents=True, exist_ok=True)
    (repo / after).write_text(f'{after}\n', encoding='utf-8')
    _git(repo, 'add', 'out')
    _git(repo, 'commit', '-m', f'{before} becomes {after}')
    # the clean child still tracks the old shape beside committed work
    (worktree / 'b.txt').write_text('b work\n', encoding='utf-8')
    _git(worktree, 'add', 'b.txt')
    _git(worktree, 'commit', '-m', 'b work')
    assert _git(worktree, 'ls-files', 'out').stdout.split() == [before]
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the merge landed and the advance ran: the child is on the post-squash
    # commit holding the target's shape, and nothing leaked onto stderr
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'Squash-merged' in result.stdout
    assert 'skipped advancing' not in result.stderr, result.stderr
    assert 'ignored null byte' not in result.stderr, result.stderr
    subject = _git(worktree, 'log', '-1', '--format=%s').stdout.strip()
    assert subject == 'merge main (post-squash)'
    assert _git(worktree, 'ls-files', 'out').stdout.split() == [after]
    assert (worktree / after).is_file()
    assert (worktree / after).read_text(encoding='utf-8') == f'{after}\n'
    assert _git(worktree, 'status', '--porcelain').stdout == ''


def test_merge_fresh_no_op_advances_past_dropped_fractal_paths(
    tmp_path: pathlib.Path,
) -> None:
    """A "Nothing to merge" whose restore dropped paths still advances the child.

    A node whose only offering is a write into a foreign seed lands nothing:
    the restore drops the path and the squash is empty. The drop is an
    adjudication like any other, so the child's merge-base advances past it
    -- otherwise every later merge re-offers the same path and repeats the
    removal warning forever. The child converges, and the next merge is a
    silent no-op.
    """
    repo = _init_tree(tmp_path / 'noopadvancerepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the scaffolding lands first, so the foreign write is the only offering
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    merge_sh = _scripts_dir() / 'merge.sh'
    first = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert first.returncode == 0, (first.stdout, first.stderr)
    foreign = worktree / '.fractal' / 'main.other' / 'f'
    foreign.parent.mkdir()
    foreign.write_text('into a foreign seed\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'write into a foreign seed')
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    second = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # nothing landed, the drop was warned about, and the child converged:
    # its HEAD is the advance and the foreign write is gone from its tree
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert 'Nothing to merge' in second.stdout, second.stdout
    assert 'the merge removed, since' in second.stderr, second.stderr
    assert '.fractal/main.other/f' in second.stderr, second.stderr
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == main_head
    subject = _git(worktree, 'log', '-1', '--format=%s').stdout.strip()
    assert subject == 'merge main (post-squash)'
    assert not foreign.exists()
    assert _git(worktree, 'status', '--porcelain').stdout == ''

    # the next merge offers nothing and warns of nothing
    third = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert third.returncode == 0, (third.stdout, third.stderr)
    assert 'Nothing to merge' in third.stdout, third.stdout
    assert 'the merge removed' not in third.stderr, third.stderr
    subject = _git(worktree, 'log', '-1', '--format=%s').stdout.strip()
    assert subject == 'merge main (post-squash)'


@pytest.mark.parametrize(
    argnames=('seed', 'advances'),
    argvalues=[('main.other', True), ('main.task', False)],
    ids=['foreign-seed', 'own-seed'],
)
def test_merge_fresh_no_op_advances_past_a_resolved_foreign_conflict(
    tmp_path: pathlib.Path,
    seed: str,
    advances: bool,
) -> None:
    """A "Nothing to merge" whose only conflict resolved itself advances the child.

    A node whose only offering is an edit to a leaked foreign seed the target
    has since removed with the merge's own remedy hits a modify/delete
    conflict the merge resolves to the target's answer, and the squash then
    stages nothing. The resolution is an adjudication like a restored path,
    so the child's merge-base advances past it -- otherwise the same conflict
    and its warning return on every later merge. The same edit to the node's
    own seed is no adjudication: the target never tracks that seed, so the
    edit reaches the squash as an addition the strip empties back out, and
    the no-op leaves the child's HEAD where it was.
    """
    repo = _init_tree(tmp_path / 'resolvednooprepo')
    if advances:
        # a foreign seed leaked onto the root before the fork
        leaked = repo / '.fractal' / seed / 'NODE.md'
        leaked.parent.mkdir(parents=True)
        leaked.write_text('# leaked contract\n', encoding='utf-8')
        _git(repo, 'add', '-f', f'.fractal/{seed}')
        _git(repo, 'commit', '-m', 'leaked seed copy')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the scaffolding lands first, so the seed edit is the only offering
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    merge_sh = _scripts_dir() / 'merge.sh'
    first = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert first.returncode == 0, (first.stdout, first.stderr)
    # the node edits the seed file alone, while the root drops its leaked
    # copy with the printed remedy
    edited = worktree / '.fractal' / seed / 'NODE.md'
    edited.write_text('child edit\n', encoding='utf-8')
    _git(worktree, 'commit', '-a', '-m', 'edit the seed only')
    if advances:
        _git(repo, 'rm', '-r', '--quiet', f'.fractal/{seed}')
        _git(repo, 'commit', '-m', 'drop leaked node seeds')
    child_head = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    second = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # nothing landed; the child converged past the resolved foreign path, or
    # stayed put for its own seed
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert 'Nothing to merge' in second.stdout, second.stdout
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == main_head
    assert _git(repo, 'status', '--porcelain').stdout == ''
    if advances:
        assert 'resolved 1 conflicting path(s) under .fractal/' in second.stderr, (
            second.stderr
        )
        subject = _git(worktree, 'log', '-1', '--format=%s').stdout.strip()
        assert subject == 'merge main (post-squash)'
        assert not edited.exists()
    else:
        assert 'resolved' not in second.stderr, second.stderr
        assert _git(worktree, 'rev-parse', 'HEAD').stdout.strip() == child_head
        assert edited.read_text(encoding='utf-8') == 'child edit\n'
    assert _git(worktree, 'status', '--porcelain').stdout == ''

    # the next merge offers nothing and resolves nothing
    third = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert third.returncode == 0, (third.stdout, third.stderr)
    assert 'Nothing to merge' in third.stdout, third.stdout
    assert 'resolved' not in third.stderr, third.stderr


# ------ merge.sh: finishing a hand-resolved squash with --continue


def test_merge_continue_finishes_a_hand_resolved_squash(
    tmp_path: pathlib.Path,
) -> None:
    """``--continue`` finishes a hand-resolved squash exactly like a clean merge.

    A conflicted merge restores the target and leaves the resolution to the
    operator, whose hand-rolled finish must otherwise reproduce the merge's
    own tail -- seed strip, commit, merge-base advance -- and a hand-rolled
    seed strip (``reset`` then ``rm``) leaves seed residue in the target
    working tree whenever the ``rm`` misses a path the squash materialized.
    ``--continue`` picks up the staged, hand-resolved squash and runs that
    tail itself: the seed lands neither in the commit nor on disk, the
    resolution is committed like a clean merge, and the child's merge-base
    advances so the next merge diffs only new work. Without a squash in
    progress the continue refuses instead of committing whatever happens to
    be staged.
    """
    repo = _init_tree(tmp_path / 'continuerepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    merge_sh = _scripts_dir() / 'merge.sh'

    # without a squash in progress the continue refuses
    premature = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}', '--continue'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert premature.returncode != 0, premature.stdout
    assert 'no squash merge is in progress' in premature.stderr

    # parent and child edit the same line so the merge conflicts
    (repo / 'tracked.txt').write_text('parent line\n', encoding='utf-8')
    _git(repo, 'add', 'tracked.txt')
    _git(repo, 'commit', '-m', 'parent edits tracked')
    (worktree / 'tracked.txt').write_text('child line\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child edits tracked')
    conflicted = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert conflicted.returncode != 0, conflicted.stdout
    assert '--continue' in conflicted.stderr

    # the operator redoes the squash by hand (it conflicts and stages the
    # child's seed into the target working tree), resolves, and stages
    redo = subprocess.run(
        ['git', 'merge', '--squash', 'main.task'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
    )
    assert redo.returncode != 0, redo.stdout
    assert (repo / '.fractal' / 'main.task').is_dir()
    (repo / 'tracked.txt').write_text('resolved line\n', encoding='utf-8')
    _git(repo, 'add', 'tracked.txt')

    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}', '--continue'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the continue commits the resolution like a clean merge, with the seed
    # stripped from the commit and the working tree both -- and the seed the
    # hand squash staged is never read as a leak (the check reads HEAD)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'leaked by an earlier merge' not in result.stderr, result.stderr
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    assert (repo / 'tracked.txt').read_text(encoding='utf-8') == 'resolved line\n'
    committed = _git(repo, 'show', '--stat', '--format=', 'HEAD').stdout
    assert '.fractal/' not in committed
    assert not (repo / '.fractal' / 'main.task').exists()
    # the child's merge-base advanced: the parent's merge commit is now an
    # ancestor of the child, so the next merge diffs only new work
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    ancestor = subprocess.run(
        ['git', 'merge-base', '--is-ancestor', main_head, 'HEAD'],
        cwd=f'{worktree}',
        capture_output=True,
        text=True,
    )
    assert ancestor.returncode == 0, ancestor.stderr


def test_merge_continue_finishes_a_target_only_resolution(
    tmp_path: pathlib.Path,
) -> None:
    """``--continue`` finishes the tail even when the resolution stages nothing.

    Resolving every conflicting hunk in favor of the target leaves an empty
    staged diff once the seed is stripped, which is *not* the clean merge's
    "no changes" outcome -- the node had changes and the operator adjudicated
    them away. Exiting there as a clean merge does would leave the target
    mid-squash (``SQUASH_MSG`` intact, so a bare ``git commit`` prefills the
    squash message) and skip the merge-base advance, replaying the identical
    conflict on the next merge forever. The continue clears the squash state
    and advances the merge-base instead, skipping only the commit.
    """
    repo = _init_tree(tmp_path / 'ourssrepo')
    # settle the wiki so the merge's index refresh stages nothing of its own:
    # the resolution's empty staged diff is what this test turns on
    settle = subprocess.run(
        ['wiki', 'update', '--path', f'{repo}/wiki'],
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert settle.returncode == 0, settle.stderr
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'settle wiki')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    merge_sh = _scripts_dir() / 'merge.sh'

    # a first clean merge lands the node's inherited scaffolding on the target
    # (and leaves its worktree clean), so the conflicting file below is the
    # node's whole contribution from there on
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    settled = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert settled.returncode == 0, (settled.stdout, settled.stderr)

    # resolving that one file to the target's content leaves nothing to commit
    (repo / 'tracked.txt').write_text('parent line\n', encoding='utf-8')
    _git(repo, 'add', 'tracked.txt')
    _git(repo, 'commit', '-m', 'parent edits tracked')
    (worktree / 'tracked.txt').write_text('child line\n', encoding='utf-8')
    _git(worktree, 'add', 'tracked.txt')
    _git(worktree, 'commit', '-m', 'child edits tracked')
    conflicted = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert conflicted.returncode != 0, conflicted.stdout

    # the operator redoes the squash and keeps the target's own content
    subprocess.run(
        ['git', 'merge', '--squash', 'main.task'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
    )
    _git(repo, 'checkout', '--ours', '--', 'tracked.txt')
    _git(repo, 'add', 'tracked.txt')

    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}', '--continue'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the resolution stands: no merge commit, the target's content intact
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'Nothing to commit' in result.stdout
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() != 'merge main.task'
    assert (repo / 'tracked.txt').read_text(encoding='utf-8') == 'parent line\n'
    # the squash state is cleared, so the target no longer looks mid-squash
    # and a bare git commit prefills nothing from the abandoned squash
    assert not (repo / '.git' / 'SQUASH_MSG').exists()
    assert not (repo / '.git' / 'MERGE_MSG').exists()
    assert not (repo / '.git' / 'AUTO_MERGE').exists()
    # and the merge-base advanced, so the resolved conflict is not replayed
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    ancestor = subprocess.run(
        ['git', 'merge-base', '--is-ancestor', main_head, 'HEAD'],
        cwd=f'{worktree}',
        capture_output=True,
        text=True,
    )
    assert ancestor.returncode == 0, ancestor.stderr
    again = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert again.returncode == 0, (again.stdout, again.stderr)
    # the resolution reached the node too: it holds the target's content
    assert (worktree / 'tracked.txt').read_text(encoding='utf-8') == 'parent line\n'


@pytest.mark.parametrize(
    argnames=('resolution', 'keep_added'),
    argvalues=[
        # a third version of the conflicted line
        ('resolved line\n', True),
        # the fork-point content restored
        ('original\n', True),
        # the added file dropped from the squash
        ('resolved line\n', False),
    ],
    ids=['third-version', 'base-content', 'dropped-add'],
)
def test_merge_continue_lands_the_resolution_on_the_node(
    tmp_path: pathlib.Path,
    resolution: str,
    keep_added: bool,
) -> None:
    """A ``--continue`` resolution reaches the node, whichever way it went.

    A squash records no per-hunk ancestry, so a resolution the operator makes
    in the target -- a third version of a conflicting line, a restore of the
    fork-point content, an added file dropped from the squash -- would be
    silently undone by the node's next merge if the node kept its own version.
    The merge-base advance records the target's post-squash tree on the node,
    so afterwards the node holds exactly what the target holds for every path
    it offered. The restore to the fork-point content and the dropped
    addition are the cases a real merge of the target into the node would
    miss: neither conflicts there, since the node is the only side that
    changed.
    """
    repo = _init_tree(tmp_path / 'resolvedrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    merge_sh = _scripts_dir() / 'merge.sh'

    # both sides edit the tracked file so the merge conflicts, and the node
    # adds a file of its own
    (repo / 'tracked.txt').write_text('parent line\n', encoding='utf-8')
    _git(repo, 'add', 'tracked.txt')
    _git(repo, 'commit', '-m', 'parent edits tracked')
    (worktree / 'tracked.txt').write_text('child line\n', encoding='utf-8')
    (worktree / 'added.txt').write_text('node added\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'node edits tracked, adds a file')
    conflicted = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert conflicted.returncode != 0, conflicted.stdout

    # the operator redoes the squash by hand and adjudicates: the conflicting
    # line one way or another, and the added file kept or dropped
    subprocess.run(
        ['git', 'merge', '--squash', 'main.task'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
    )
    (repo / 'tracked.txt').write_text(resolution, encoding='utf-8')
    _git(repo, 'add', 'tracked.txt')
    if not keep_added:
        _git(repo, 'rm', '--cached', '--quiet', 'added.txt')
        (repo / 'added.txt').unlink()

    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}', '--continue'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the resolution stands on the target...
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'skipped advancing' not in result.stderr, result.stderr
    assert (repo / 'tracked.txt').read_text(encoding='utf-8') == resolution
    assert (repo / 'added.txt').is_file() is keep_added
    # ...and the node holds the same content (or absence) for every path it
    # offered, so a re-merge has nothing to undo
    for name in ('tracked.txt', 'added.txt'):
        target_copy = (
            (repo / name).read_text(encoding='utf-8')
            if (repo / name).is_file()
            else None
        )
        node_copy = (
            (worktree / name).read_text(encoding='utf-8')
            if (worktree / name).is_file()
            else None
        )
        assert node_copy == target_copy, (name, result.stderr)


def test_merge_continue_restores_a_foreign_seed_edit_the_hand_squash_carries(
    tmp_path: pathlib.Path,
) -> None:
    """``--continue`` restores a foreign ``.fractal/`` edit the hand squash carries.

    An operator's hand squash stages everything the node offered, a clean
    edit to a seed the target tracks included -- a child's edit to its
    parent's contract beside the conflict the operator resolved. The
    continue runs the merge's own tail, so the restore returns that path to
    the target's content and names it in the restored warning exactly as a
    clean merge would; the resolution lands, and the parent's contract stays
    its own.
    """
    repo = _init_tree(tmp_path / 'continuerestorerepo')
    init = _run(repo, 'node', 'init', 'p', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    parent = repo / '.worktrees' / 'main.p'
    _git(parent, 'add', '-A')
    _git(parent, 'commit', '-m', 'settle p estate')
    node_dir = parent / '.fractal' / 'main.p'
    spawn = _run(
        repo, 'node', 'init', 'c', '--agent', 'claude', '--local', _NODE=str(node_dir)
    )
    assert spawn.returncode == 0, spawn.stderr
    child = repo / '.worktrees' / 'main.p.c'
    _git(child, 'add', '-A')
    _git(child, 'commit', '-m', 'settle c estate')
    # parent and child edit the same line so the merge conflicts, and the
    # child edits the parent's contract on the side
    (parent / 'tracked.txt').write_text('parent line\n', encoding='utf-8')
    _git(parent, 'commit', '-a', '-m', 'p edits tracked')
    contract = parent / '.fractal' / 'main.p' / 'NODE.md'
    parent_contract = contract.read_text(encoding='utf-8')
    (child / 'tracked.txt').write_text('child line\n', encoding='utf-8')
    (child / '.fractal' / 'main.p' / 'NODE.md').write_text(
        parent_contract + '\nChild line.\n', encoding='utf-8'
    )
    _git(child, 'commit', '-a', '-m', 'c edits tracked and the contract')
    merge_sh = _scripts_dir() / 'merge.sh'
    conflicted = subprocess.run(
        ['bash', f'{merge_sh}', f'{child}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert conflicted.returncode != 0, conflicted.stdout
    assert '--continue' in conflicted.stderr
    # the operator redoes the squash by hand, resolves, and stages -- the
    # hand squash carries the contract edit along
    redo = subprocess.run(
        ['git', 'merge', '--squash', 'main.p.c'],
        cwd=f'{parent}',
        capture_output=True,
        text=True,
    )
    assert redo.returncode != 0, redo.stdout
    (parent / 'tracked.txt').write_text('resolved line\n', encoding='utf-8')
    _git(parent, 'add', 'tracked.txt')
    staged = _git(parent, 'diff', '--cached', '--name-only').stdout
    assert '.fractal/main.p/NODE.md' in staged, staged

    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{child}', '--continue'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the resolution landed with the contract back at the parent's content,
    # the restore named in the warning, and nothing under .fractal/ committed
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "restored to main.p's content" in result.stderr, result.stderr
    assert '.fractal/main.p/NODE.md' in result.stderr, result.stderr
    assert _git(parent, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.p.c'
    assert (parent / 'tracked.txt').read_text(encoding='utf-8') == 'resolved line\n'
    assert contract.read_text(encoding='utf-8') == parent_contract
    committed = _git(parent, 'show', '--stat', '--format=', 'HEAD').stdout
    assert '.fractal/' not in committed, committed
    assert _git(parent, 'status', '--porcelain').stdout == ''


def test_merge_continue_refuses_unstaged_target_edits(
    tmp_path: pathlib.Path,
) -> None:
    """``--continue`` refuses while an unstaged edit to a tracked path remains.

    A hand-resolved squash is fully staged by contract. An unstaged edit to a
    tracked ``.fractal/`` path -- an operator's own tweak to a profile made
    mid-resolution -- would be rewritten by the restore that returns every
    ``.fractal/`` path to HEAD, so the continue names the path and leaves the
    edit and the staged squash in place; once staged, the continue lands.
    """
    repo = _init_tree(tmp_path / 'unstagedrepo')
    profile = repo / '.fractal' / 'profiles' / 'p' / 'NODE.md'
    profile.parent.mkdir(parents=True)
    profile.write_text('v1\n', encoding='utf-8')
    _git(repo, 'add', '.fractal/profiles')
    _git(repo, 'commit', '-m', 'add profile')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    # both sides edit the same line so the merge conflicts
    (repo / 'tracked.txt').write_text('parent line\n', encoding='utf-8')
    _git(repo, 'add', 'tracked.txt')
    _git(repo, 'commit', '-m', 'parent edits tracked')
    (worktree / 'tracked.txt').write_text('child line\n', encoding='utf-8')
    _git(worktree, 'add', 'tracked.txt')
    _git(worktree, 'commit', '-m', 'child edits tracked')
    merge_sh = _scripts_dir() / 'merge.sh'
    conflicted = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert conflicted.returncode != 0, conflicted.stdout

    # the operator redoes the squash, resolves and stages the conflict, and
    # tweaks the tracked profile without staging it
    subprocess.run(
        ['git', 'merge', '--squash', 'main.task'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
    )
    (repo / 'tracked.txt').write_text('resolved line\n', encoding='utf-8')
    _git(repo, 'add', 'tracked.txt')
    profile.write_text('v1\nOPERATOR EDIT\n', encoding='utf-8')
    refused = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}', '--continue'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # refused naming the path, with the edit and the staged squash intact
    assert refused.returncode != 0, (refused.stdout, refused.stderr)
    assert 'unstaged changes remain' in refused.stderr, refused.stderr
    assert '.fractal/profiles/p/NODE.md' in refused.stderr, refused.stderr
    # the remedy keeps the edit recoverable -- a copy, then git add or
    # checkout -- per path -- and never sends it to a stash the restore
    # would not see
    assert 'save any copy you need' in refused.stderr, refused.stderr
    assert 'checkout -- <path>' in refused.stderr, refused.stderr
    assert "restores every .fractal/ path to main's HEAD" in refused.stderr
    assert 'stash' not in refused.stderr, refused.stderr
    assert profile.read_text(encoding='utf-8').endswith('OPERATOR EDIT\n')
    assert (repo / '.git' / 'SQUASH_MSG').exists()

    # staged, the same squash lands
    _git(repo, 'add', '.fractal/profiles/p/NODE.md')
    landed = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}', '--continue'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert landed.returncode == 0, (landed.stdout, landed.stderr)
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    assert (repo / 'tracked.txt').read_text(encoding='utf-8') == 'resolved line\n'


def test_merge_continue_refuses_commits_newer_than_the_squash(
    tmp_path: pathlib.Path,
) -> None:
    """``--continue`` refuses a hand squash the node has since outgrown.

    ``SQUASH_MSG`` lists every commit the hand squash took, so a commit the
    node made after it -- an iteration, a nested merge -- is work the squash
    never staged, which the merge-base advance would then record on the node
    as adjudicated away. The continue refuses naming the redo (reset the
    target, squash again), leaving the staged resolution and ``SQUASH_MSG``
    in place; redone over the whole branch, the same continue lands the
    later commit with the resolution.
    """
    repo = _init_tree(tmp_path / 'outgrownrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # both sides edit the same line so the merge conflicts
    (repo / 'tracked.txt').write_text('parent line\n', encoding='utf-8')
    _git(repo, 'add', 'tracked.txt')
    _git(repo, 'commit', '-m', 'parent edits tracked')
    (worktree / 'tracked.txt').write_text('child line\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child edits tracked')
    merge_sh = _scripts_dir() / 'merge.sh'
    conflicted = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert conflicted.returncode != 0, conflicted.stdout

    # the operator redoes the squash and resolves; the node commits again
    # before the continue runs
    subprocess.run(
        ['git', 'merge', '--squash', 'main.task'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
    )
    (repo / 'tracked.txt').write_text('resolved line\n', encoding='utf-8')
    _git(repo, 'add', 'tracked.txt')
    (worktree / 'later.txt').write_text('later work\n', encoding='utf-8')
    _git(worktree, 'add', 'later.txt')
    _git(worktree, 'commit', '-m', 'later work')
    refused = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}', '--continue'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # refused naming the redo, with the staged resolution and squash intact
    assert refused.returncode != 0, (refused.stdout, refused.stderr)
    assert 'commits newer than the squash in progress' in refused.stderr
    assert 'reset --hard HEAD' in refused.stderr, refused.stderr
    assert 'merge --squash main.task' in refused.stderr, refused.stderr
    assert (repo / '.git' / 'SQUASH_MSG').exists()
    assert _git(repo, 'show', ':tracked.txt').stdout == 'resolved line\n'
    assert not (repo / 'later.txt').exists()

    # redone over the whole branch, the continue lands the later commit too
    _git(repo, 'reset', '--hard', 'HEAD')
    redo = subprocess.run(
        ['git', 'merge', '--squash', 'main.task'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
    )
    assert redo.returncode != 0, redo.stdout
    (repo / 'tracked.txt').write_text('resolved line\n', encoding='utf-8')
    _git(repo, 'add', 'tracked.txt')
    landed = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}', '--continue'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert landed.returncode == 0, (landed.stdout, landed.stderr)
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    assert (repo / 'tracked.txt').read_text(encoding='utf-8') == 'resolved line\n'
    assert (repo / 'later.txt').read_text(encoding='utf-8') == 'later work\n'
    assert _git(repo, 'status', '--porcelain').stdout == ''
    assert not (repo / '.git' / 'SQUASH_MSG').exists()


def test_merge_continue_refuses_unresolved_conflicts(tmp_path: pathlib.Path) -> None:
    """``--continue`` refuses while the hand squash still holds a conflict.

    The continue's tail restores ``.fractal/`` paths, checks the footprint,
    and commits whatever is staged; an unmerged index entry there would
    either commit conflict markers or die inside the tail with the squash
    half-processed. The continue refuses up front, naming the ``git add``
    that resolves it, and leaves the conflict and the squash state exactly
    as the operator left them.
    """
    repo = _init_tree(tmp_path / 'unresolvedrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # both sides edit the same line so the hand squash conflicts
    (repo / 'tracked.txt').write_text('parent line\n', encoding='utf-8')
    _git(repo, 'add', 'tracked.txt')
    _git(repo, 'commit', '-m', 'parent edits tracked')
    (worktree / 'tracked.txt').write_text('child line\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child edits tracked')
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    # the operator squashes by hand and continues without resolving
    redo = subprocess.run(
        ['git', 'merge', '--squash', 'main.task'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
    )
    assert redo.returncode != 0, redo.stdout
    merge_sh = _scripts_dir() / 'merge.sh'
    refused = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}', '--continue'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # refused with the remedy, the conflict and the squash state untouched
    assert refused.returncode != 0, (refused.stdout, refused.stderr)
    assert (
        "Error: unresolved conflicts remain in main's worktree; resolve and stage"
        ' them (git add), then re-run with --continue'
    ) in refused.stderr, refused.stderr
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == main_head
    assert _git(repo, 'ls-files', '-u').stdout != ''
    assert (repo / '.git' / 'SQUASH_MSG').exists()


def test_merge_continue_refuses_a_siblings_squash(tmp_path: pathlib.Path) -> None:
    """``--continue`` refuses a staged squash that comes from another node.

    The continue trusts the target's staged state as the named node's
    hand-resolved squash: it strips that node's seed and advances that
    node's merge-base. A sibling's squash left staged in the target would
    be committed as the named node's merge, with the sibling's seed landed
    and the wrong branch advanced. The squash's provenance is read from
    ``SQUASH_MSG``, so a node with nothing to offer -- whose branch has no
    commits past the target, which the newer-commits check alone would
    wave through -- is still refused, and nothing is committed.
    """
    repo = _init_tree(tmp_path / 'foreignsquashrepo')
    for name in ('a', 'b'):
        init = _run(repo, 'node', 'init', name, '--agent', 'claude', '--local')
        assert init.returncode == 0, init.stderr
    idle = repo / '.worktrees' / 'main.a'
    sibling = repo / '.worktrees' / 'main.b'
    (sibling / 'b.txt').write_text('b work\n', encoding='utf-8')
    _git(sibling, 'add', '-A')
    _git(sibling, 'commit', '-m', 'b work')
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    # the operator squashes the sibling by hand, then continues the other node
    _git(repo, 'merge', '--squash', 'main.b')
    refused = _run(repo, 'node', 'merge', '--continue', f'--path={idle}')

    # refused naming the node the squash is not from, nothing committed, and
    # the sibling's squash left staged for its own merge
    assert refused.returncode != 0, (refused.stdout, refused.stderr)
    assert (
        "the squash in progress in main's worktree does not come from main.a;"
        ' commit or abort it before merging this node'
    ) in refused.stderr, refused.stderr
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == main_head
    assert _git(repo, 'ls-files', '--cached', 'b.txt').stdout.strip() == 'b.txt'
    assert (repo / '.git' / 'SQUASH_MSG').exists()


# ------ merge.sh: the target's .fractal/ after the squash


def test_merge_leaves_the_targets_fractal_dir_as_it_is(
    tmp_path: pathlib.Path,
) -> None:
    """A child's edits under the target's ``.fractal/`` never reach the target.

    A child's commit sweeps all of ``.fractal/``, so the target's own estate
    (its scripts, plans, memory) and any foreign seed the child carries ride
    the squash. The merge returns every ``.fractal`` directory on the target
    to its HEAD before committing: an added foreign seed is absent, a modified
    estate file keeps the target's content, a deleted one stays tracked, and
    the target's memory is untouched -- with a warning per fate naming what
    was dropped (restored to the target's content, or removed as a path it
    never tracked), so a deliberate change can still be landed by hand. A
    descendant's seed the target folded in with a real merge is its own to
    track, never reported as a leak.
    """
    repo = _init_tree(tmp_path / 'estaterepo')
    init = _run(repo, 'node', 'init', 'parent', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    parent = repo / '.worktrees' / 'main.parent'
    # the parent tracks its own estate, with both wikis settled so the merge's
    # index refresh regenerates nothing of its own
    for wiki_dir in (parent / 'wiki', parent / '.fractal' / 'main.parent' / 'memory'):
        settle = subprocess.run(
            ['wiki', 'update', f'--path={wiki_dir}'],
            capture_output=True,
            text=True,
            env=_cli_env(),
        )
        assert settle.returncode == 0, settle.stderr
    _git(parent, 'add', '-A')
    _git(parent, 'commit', '-m', 'settle parent estate')
    node_dir = parent / '.fractal' / 'main.parent'
    # a child the parent folded in with a real merge (as its PREPARE step
    # does), so the parent tracks a descendant's seed of its own
    folded = _run(
        parent, 'node', 'init', 'c2', '--agent', 'claude', _NODE=str(node_dir)
    )
    assert folded.returncode == 0, folded.stderr
    descendant = repo / '.worktrees' / 'main.parent.c2'
    _git(descendant, 'add', '-A')
    _git(descendant, 'commit', '-m', 'descendant seed')
    _git(parent, 'merge', '--no-ff', '--no-edit', 'main.parent.c2')
    spawn = _run(
        parent, 'node', 'init', 'child', '--agent', 'claude', _NODE=str(node_dir)
    )
    assert spawn.returncode == 0, spawn.stderr
    child = repo / '.worktrees' / 'main.parent.child'
    # the child writes into the parent's estate and a foreign seed...
    estate = child / '.fractal' / 'main.parent'
    foreign = child / '.fractal' / 'main.other' / 'NODE.md'
    foreign.parent.mkdir()
    foreign.write_text('# foreign contract\n', encoding='utf-8')
    script = estate / 'scripts' / 'test.sh'
    script.write_text(
        script.read_text(encoding='utf-8') + 'echo edited\n', encoding='utf-8'
    )
    _git(child, 'rm', '--quiet', '.fractal/main.parent/plans/.gitkeep')
    (estate / 'memory' / 'note.md').write_text(
        '# note\n\nchild memory.\n', encoding='utf-8'
    )
    # ...beside real work
    (child / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(child, 'add', '-A')
    _git(child, 'commit', '-m', 'child edits the estate')
    script_before = _git(
        parent, 'show', 'HEAD:.fractal/main.parent/scripts/test.sh'
    ).stdout
    memory_before = _git(
        parent, 'rev-parse', 'HEAD:.fractal/main.parent/memory'
    ).stdout.strip()
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{child}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    # the real work landed and nothing under .fractal/ changed: the foreign
    # seed is absent, the script keeps the parent's content, the deleted file
    # is still tracked, and the memory tree is the same
    assert (parent / 'f.txt').read_text(encoding='utf-8') == 'child work\n'
    assert not (parent / '.fractal' / 'main.other').exists()
    assert (parent / '.fractal' / 'main.parent' / 'scripts' / 'test.sh').read_text(
        encoding='utf-8'
    ) == script_before
    tracked = _git(parent, 'ls-files', '.fractal').stdout
    assert '.fractal/main.other/NODE.md' not in tracked
    assert '.fractal/main.parent/plans/.gitkeep' in tracked
    assert '.fractal/main.parent/memory/note.md' not in tracked
    assert '.fractal/main.parent.c2/NODE.md' in tracked
    memory_after = _git(
        parent, 'rev-parse', 'HEAD:.fractal/main.parent/memory'
    ).stdout.strip()
    assert memory_after == memory_before
    assert _git(parent, 'status', '--porcelain').stdout == ''
    # the warnings name every dropped path by fate: the paths the parent
    # tracks went back to its content, the paths it lacks were removed -- and
    # the descendant seed the parent tracks is its own, never a leak
    warnings = result.stderr.splitlines()
    restored = next(line for line in warnings if 'the merge restored to' in line)
    removed = next(line for line in warnings if 'the merge removed, since' in line)
    assert restored.startswith(
        "Warning: main.parent.child's squash changed paths under .fractal/"
    ), restored
    assert "main.parent's content" in restored, restored
    for path in (
        '.fractal/main.parent/scripts/test.sh',
        '.fractal/main.parent/plans/.gitkeep',
    ):
        assert path in restored, (path, restored)
        assert path not in removed, (path, removed)
    assert removed.startswith(
        "Warning: main.parent.child's squash added paths under .fractal/"
    ), removed
    assert "main.parent.child's branch history keeps its copy" in removed, removed
    for path in (
        '.fractal/main.other/NODE.md',
        '.fractal/main.parent/memory/note.md',
    ):
        assert path in removed, (path, removed)
        assert path not in restored, (path, restored)
    assert 'leaked by an earlier merge' not in result.stderr, result.stderr


@pytest.mark.parametrize(
    argnames=('scope', 'live', 'added', 'named'),
    argvalues=[
        # the root's live memory, self-ignored there
        (
            [],
            '.fractal/main/memory/notes.md',
            '.fractal/main/memory/notes.md',
            '.fractal/main/memory/notes.md',
        ),
        # the root's own config, from a node scoped to .fractal itself
        (
            ['--scope', '.fractal'],
            '.fractal/main/config.json',
            '.fractal/main/config.json',
            '.fractal/main/config.json',
        ),
        # a live file where the squash creates a directory
        (
            [],
            '.fractal/main/scratch',
            '.fractal/main/scratch/x.md',
            '.fractal/main/scratch',
        ),
        # a private ignored file outside .fractal/
        (['--scope', 'docs'], 'local.env', 'local.env', 'local.env'),
    ],
    ids=[
        'root-memory',
        'root-config-in-scope',
        'file-at-a-prefix',
        'ignored-outside-fractal',
    ],
)
def test_merge_refuses_over_any_untracked_file_the_squash_would_overwrite(
    tmp_path: pathlib.Path,
    scope: list[str],
    live: str,
    added: str,
    named: str,
) -> None:
    """Every path the squash would write over an untracked file refuses the merge.

    Git treats an untracked ignored file as expendable, so a squash adding
    the same path silently overwrites it -- and the tail then commits or
    deletes it. The guard covers every path the node added or changed since
    the merge-base, wherever it sits: the root's live memory (its own seed
    is self-ignored on the root, so the memory is invisible to git there yet
    committable from a child, whose commit sweeps all of ``.fractal/``), the
    root's live ``config.json`` even from a node scoped to ``.fractal``
    itself (no scope carve-out -- the root never tracks its own seed, so no
    restore could bring it back), a file sitting where the squash creates a
    directory (every parent prefix is probed), and a private ``local.env``
    outside ``.fractal/`` that a scoped node force-added -- which the
    footprint refusal's reset would otherwise delete from the root's disk
    after the squash had overwritten it. Each refuses before the squash,
    naming the path in the way, and leaves the live file, HEAD, and index as
    they were -- and the root still answers.
    """
    repo = _init_tree(tmp_path / 'overwriterepo')
    if live == 'local.env':
        (repo / '.gitignore').write_text('local.env\n', encoding='utf-8')
        _git(repo, 'add', '.gitignore')
        _git(repo, 'commit', '-m', 'ignore local.env')
    init = _run(repo, 'node', 'init', 'task', *scope, '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    # the root's live file -- its own config, a scratch file in its seed, a
    # private env file -- untracked and ignored where it sits
    live_path = repo / live
    if live != '.fractal/main/config.json':
        live_path.parent.mkdir(parents=True, exist_ok=True)
        live_path.write_text('root live\n', encoding='utf-8')
    live_before = live_path.read_bytes()
    assert _git(repo, 'status', '--porcelain').stdout == ''
    # the node commits the same path past any ignore, beside in-scope work
    (worktree / added).parent.mkdir(parents=True, exist_ok=True)
    (worktree / added).write_text('child\n', encoding='utf-8')
    (worktree / 'docs').mkdir()
    (worktree / 'docs' / 'a.md').write_text('# a\n', encoding='utf-8')
    _git(worktree, 'add', '-f', added, 'docs')
    _git(worktree, 'commit', '-m', 'child writes over the root')
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # refused naming the path in the way; the live file, HEAD, and index are
    # untouched, no squash state is left behind, and the root still answers
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert (
        f"Error: merging main.task into main would overwrite untracked files in main's"
        f' worktree: {named}; move them aside or drop them from main.task before'
        ' merging'
    ) in result.stderr, result.stderr
    assert live_path.read_bytes() == live_before
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == main_head
    assert _git(repo, 'status', '--porcelain').stdout == ''
    assert not (repo / '.git' / 'SQUASH_MSG').exists()
    assert not (repo / 'docs').exists()
    listed = _run(repo, 'node', 'list')
    assert listed.returncode == 0, listed.stderr


def test_merge_refuses_over_a_seed_file_the_root_untracked_but_kept(
    tmp_path: pathlib.Path,
) -> None:
    """A path the root dropped from its index with ``--cached`` is still a collision.

    ``fractal untrack``'s remedy is ``git rm -r --cached``: the root's seed
    leaves the index but stays on disk as the live seed, self-ignored again.
    A node forked while the seed was tracked and editing a file in it offers
    a modification the squash would write over that live copy -- HEAD no
    longer tracks the path, so nothing restores it -- while a path untracked
    with a plain ``git rm`` has no disk copy and is no collision. The guard
    judges added and modified paths alike and refuses, and the live file
    survives.
    """
    repo = _init_tree(tmp_path / 'untrackedrepo')
    # the root tracks its seed for a while, notes included
    track = _run(repo, 'track')
    assert track.returncode == 0, track.stderr
    notes = repo / '.fractal' / 'main' / 'memory' / 'notes.md'
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text('root notes v1\n', encoding='utf-8')
    _git(repo, 'add', '.fractal/main/memory/notes.md')
    _git(repo, 'commit', '-m', 'track the root notes')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    # the node edits the root's notes...
    (worktree / '.fractal' / 'main' / 'memory' / 'notes.md').write_text(
        'child edit\n', encoding='utf-8'
    )
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child edits the root notes')
    # ...while the root untracks its seed with the printed remedy, which
    # keeps the live copy on disk
    untrack = _run(repo, 'untrack')
    assert untrack.returncode == 0, untrack.stderr
    remedy = next(
        line for line in untrack.stdout.splitlines() if 'git rm -r --cached' in line
    ).split('with: ', 1)[1]
    removed = subprocess.run(
        ['bash', '-c', remedy], cwd=f'{repo}', capture_output=True, text=True
    )
    assert removed.returncode == 0, (removed.stdout, removed.stderr)
    _git(repo, 'commit', '-m', 'untrack the root seed')
    assert notes.read_text(encoding='utf-8') == 'root notes v1\n'
    assert _git(repo, 'status', '--porcelain').stdout == ''
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # refused naming the live path; the file, HEAD, and index are untouched
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert 'would overwrite untracked files in main' in result.stderr, result.stderr
    assert '.fractal/main/memory/notes.md' in result.stderr, result.stderr
    assert notes.read_text(encoding='utf-8') == 'root notes v1\n'
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == main_head
    assert _git(repo, 'status', '--porcelain').stdout == ''
    assert not (repo / '.git' / 'SQUASH_MSG').exists()


@pytest.mark.parametrize(
    argnames=('project', 'seed_prefix', 'work'),
    argvalues=[
        ([], '.fractal', 'feature.txt'),
        (['--path', 'app'], 'app/.fractal', 'app/feature.txt'),
    ],
    ids=['repo-root', 'sub-project'],
)
def test_merge_refuses_over_an_ignored_copy_of_the_nodes_own_seed(
    tmp_path: pathlib.Path,
    project: list[str],
    seed_prefix: str,
    work: str,
) -> None:
    """An ignored copy of the node's own seed on the target's disk refuses the merge.

    The squash stages the node's seed like any other addition, and the
    restore then deletes it as a path the target never tracked -- so a copy
    of that seed sitting ignored on the target's disk (an operator's private
    notes at the node's own path) would be overwritten by the squash and
    removed by the restore, with no refusal and no warning. The guard carves
    out no path for being the node's own seed: it refuses before the squash
    naming the file, wherever the seed nests -- ``.fractal/`` at the repo
    root or ``<project>/.fractal/`` for a sub-project node -- and leaves the
    copy, the target's HEAD, and its index as they were.
    """
    repo = _init_tree(tmp_path / 'ownseedcopyrepo')
    if project:
        # a committed sub-project wiki -- the base-ref precondition for the init
        app_wiki = repo / 'app' / 'wiki' / '_index.md'
        app_wiki.parent.mkdir(parents=True)
        app_wiki.write_text('---\nname: app\n---\n# app\n\n***\n', encoding='utf-8')
        _git(repo, 'add', 'app')
        _git(repo, 'commit', '-m', 'add app wiki')
    init = _run(
        repo, 'node', 'init', 'feature', *project, '--agent', 'claude', '--local'
    )
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.feature'
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    # the operator's private copy at the node's own seed path on the target,
    # ignored through the shared info/exclude -- after the node's seed is
    # committed, since the rule reaches the node's worktree too
    copy = repo / seed_prefix / 'main.feature' / 'NODE.md'
    copy.parent.mkdir(parents=True)
    copy.write_text("operator's local notes\n", encoding='utf-8')
    with (repo / '.git' / 'info' / 'exclude').open('a', encoding='utf-8') as exclude:
        exclude.write(f'{seed_prefix}/main.feature/\n')
    assert _git(repo, 'status', '--porcelain').stdout == ''
    (worktree / work).write_text('feature work\n', encoding='utf-8')
    _git(worktree, 'add', work)
    _git(worktree, 'commit', '-m', 'feature work')
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # refused naming the copy; the copy, HEAD, and index are untouched and no
    # squash state was left behind
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert (
        'Error: merging main.feature into main would overwrite untracked files in'
        f" main's worktree: {seed_prefix}/main.feature/NODE.md; move them aside or"
        ' drop them from main.feature before merging'
    ) in result.stderr, result.stderr
    assert copy.read_text(encoding='utf-8') == "operator's local notes\n"
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == main_head
    assert _git(repo, 'status', '--porcelain').stdout == ''
    assert not (repo / '.git' / 'SQUASH_MSG').exists()
    assert not (repo / work).exists()


def test_merge_lands_a_meta_nodes_edit_to_the_targets_seed(
    tmp_path: pathlib.Path,
) -> None:
    """A ``--meta`` node's edit to the target's own seed dir still lands.

    A meta node's scope is the target's ``.fractal/<branch>/`` -- its whole
    work product is the target's contract and machinery -- so that one upward
    flow under ``.fractal/`` is work, not machinery riding along: the restore
    that returns the rest of the target's ``.fractal/`` to HEAD leaves the
    meta node's scope root alone, and the edit lands without a warning, while
    a profile the meta node adds beside it is removed like any other
    ``.fractal/`` addition. Being work, a conflict inside the scope root is
    the operator's -- the merge refuses it rather than resolving it to the
    target's content as it does for machinery.
    """
    repo = _init_tree(tmp_path / 'metarepo')
    init = _run(repo, 'node', 'init', 'parent', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    parent = repo / '.worktrees' / 'main.parent'
    # the target tracks its own estate, so the meta node forks with it
    _git(parent, 'add', '-A')
    _git(parent, 'commit', '-m', 'settle parent estate')
    meta = _run(
        repo, 'node', 'init', 'fix', '--meta', 'main.parent', '--agent', 'claude'
    )
    assert meta.returncode == 0, meta.stderr
    fix = repo / '.worktrees' / 'main.fix'
    contract = fix / '.fractal' / 'main.parent' / 'NODE.md'
    contract.write_text(
        contract.read_text(encoding='utf-8') + '\nTuned by the meta node.\n',
        encoding='utf-8',
    )
    # ...and adds a profile beside it, outside its scope root
    profile = fix / '.fractal' / 'profiles' / 'q' / 'NODE.md'
    profile.parent.mkdir(parents=True)
    profile.write_text('# q\n', encoding='utf-8')
    _git(fix, 'add', '-A')
    _git(fix, 'commit', '-m', 'tune the target contract')
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{fix}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the edit is on the target and the meta node's own seed is not; nothing
    # was restored, and only the profile outside the scope root was removed
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'changed paths under .fractal/' not in result.stderr, result.stderr
    assert 'added paths under .fractal/ that the merge removed' in result.stderr
    assert '.fractal/profiles/q/NODE.md' in result.stderr, result.stderr
    assert 'leaked by an earlier merge' not in result.stderr, result.stderr
    landed = (parent / '.fractal' / 'main.parent' / 'NODE.md').read_text(
        encoding='utf-8'
    )
    assert landed.endswith('Tuned by the meta node.\n')
    assert not (parent / '.fractal' / 'main.fix').exists()
    assert _git(parent, 'ls-files', '.fractal/profiles').stdout == ''
    assert _git(parent, 'status', '--porcelain').stdout == ''

    # a later edit on both sides of the scope root is the operator's conflict
    # -- work, not machinery the merge resolves to the target's content -- so
    # the merge refuses and restores the target
    (parent / '.fractal' / 'main.parent' / 'NODE.md').write_text(
        landed + 'Tuned by the parent.\n', encoding='utf-8'
    )
    _git(parent, 'add', '.fractal/main.parent/NODE.md')
    _git(parent, 'commit', '-m', 'parent tunes its contract')
    contract.write_text(landed + 'Tuned again by the meta node.\n', encoding='utf-8')
    _git(fix, 'add', '-A')
    _git(fix, 'commit', '-m', 'tune the target contract again')
    parent_head = _git(parent, 'rev-parse', 'HEAD').stdout.strip()
    conflicted = subprocess.run(
        ['bash', f'{merge_sh}', f'{fix}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert conflicted.returncode != 0, (conflicted.stdout, conflicted.stderr)
    assert 'produced conflicts' in conflicted.stderr, conflicted.stderr
    assert _git(parent, 'rev-parse', 'HEAD').stdout.strip() == parent_head
    assert _git(parent, 'status', '--porcelain').stdout == ''


def test_merge_lands_a_fractal_scoped_nodes_profile_edit(
    tmp_path: pathlib.Path,
) -> None:
    """A node scoped to ``.fractal`` itself lands its profile edits.

    A scope root of ``.fractal`` makes the whole machinery dir the node's
    work product -- a node commissioned to maintain the tree's profiles --
    so the restore that returns ``.fractal/`` to the target's HEAD carves
    the whole dir out: the edit lands with no warning, and the advance
    carries it back to the node's worktree as the target's own content.
    """
    repo = _init_tree(tmp_path / 'profilescoperepo')
    profile = repo / '.fractal' / 'profiles' / 'p' / 'NODE.md'
    profile.parent.mkdir(parents=True)
    profile.write_text('v1\n', encoding='utf-8')
    _git(repo, 'add', '.fractal/profiles')
    _git(repo, 'commit', '-m', 'add profile')
    init = _run(
        repo,
        'node',
        'init',
        'task',
        '--scope',
        '.fractal',
        '--agent',
        'claude',
        '--local',
    )
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    # the node edits the profile through the commit law, which admits it
    node_copy = worktree / '.fractal' / 'profiles' / 'p' / 'NODE.md'
    node_copy.write_text('v2\n', encoding='utf-8')
    commit = _run(worktree, 'commit', 'set the profile to v2')
    assert commit.returncode == 0, commit.stderr
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the edit is on the target with nothing restored, and the node's worktree
    # keeps it after the advance
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'changed paths under .fractal/' not in result.stderr, result.stderr
    assert 'skipped advancing' not in result.stderr, result.stderr
    assert profile.read_text(encoding='utf-8') == 'v2\n'
    assert node_copy.read_text(encoding='utf-8') == 'v2\n'
    assert _git(repo, 'status', '--porcelain').stdout == ''


def test_merge_lands_a_multi_root_scope_with_a_profile_root(
    tmp_path: pathlib.Path,
) -> None:
    """A node scoped to a work dir and the profiles dir lands both through the merge.

    Scope roots are read one per line, so a node commissioned for ``docs``
    and ``.fractal/profiles`` carves the profiles dir alone out of the
    restore while every other ``.fractal/`` path stays the target's: the
    profile edit lands with no restore warning beside the docs work, and
    neither trips the footprint check.
    """
    repo = _init_tree(tmp_path / 'multiscoperepo')
    profile = repo / '.fractal' / 'profiles' / 'p' / 'NODE.md'
    profile.parent.mkdir(parents=True)
    profile.write_text('v1\n', encoding='utf-8')
    _git(repo, 'add', '.fractal/profiles')
    _git(repo, 'commit', '-m', 'add profile')
    init = _run(
        repo,
        'node',
        'init',
        'task',
        '--scope',
        'docs',
        '--scope',
        '.fractal/profiles',
        '--agent',
        'claude',
        '--local',
    )
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    # the node edits the profile and writes its docs through the commit law,
    # which admits both roots
    node_copy = worktree / '.fractal' / 'profiles' / 'p' / 'NODE.md'
    node_copy.write_text('v2\n', encoding='utf-8')
    (worktree / 'docs').mkdir()
    (worktree / 'docs' / 'a.md').write_text('# a\n', encoding='utf-8')
    commit = _run(worktree, 'commit', 'edit the profile and the docs')
    assert commit.returncode == 0, commit.stderr
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # both roots landed with nothing restored, removed, or refused
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'changed paths under .fractal/' not in result.stderr, result.stderr
    assert 'added paths under .fractal/' not in result.stderr, result.stderr
    assert 'outside its scope' not in result.stderr, result.stderr
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    assert profile.read_text(encoding='utf-8') == 'v2\n'
    assert (repo / 'docs' / 'a.md').read_text(encoding='utf-8') == '# a\n'
    assert _git(repo, 'status', '--porcelain').stdout == ''


@pytest.mark.parametrize(
    argnames=('seed', 'target_edit', 'node_copy'),
    argvalues=[
        ('main.task', None, 'child v2\n'),
        ('main.other', 'target v2\n', 'target v2\n'),
    ],
    ids=['purged', 'edited-on-both-sides'],
)
def test_merge_resolves_a_conflict_on_the_nodes_own_seed(
    tmp_path: pathlib.Path,
    seed: str,
    target_edit: Optional[str],
    node_copy: str,
) -> None:
    """A squash conflicting only under ``.fractal/`` outside the scope resolves itself.

    Such a conflict has a known answer: the restore makes the target's HEAD
    authoritative for every ``.fractal/`` path outside the node's scope roots,
    and the node's own seed is always stripped. A copy of the node's seed
    that leaked onto the target and was purged there leaves the merge-base
    carrying it, so the node's live edits hit modify/delete conflicts on
    paths the strip would remove anyway; a foreign seed the target tracks
    and both sides edit conflicts on a path the restore would return to the
    target's content. Either way the merge resolves the entries as the tail
    would and lands: the node's own seed is stripped and its live copy
    survives the advance, while the foreign path keeps the target's content,
    stays tracked, and reaches the node as such.
    """
    repo = _init_tree(tmp_path / 'seedconflictrepo')
    # a seed copy on the target before the fork
    copy = repo / '.fractal' / seed / 'memory' / 'x.md'
    copy.parent.mkdir(parents=True)
    copy.write_text('v1\n', encoding='utf-8')
    _git(repo, 'add', f'.fractal/{seed}')
    _git(repo, 'commit', '-m', 'seed copy')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the target purges its copy or edits it, while the node edits its own
    if target_edit is None:
        _git(repo, 'rm', '-r', '--quiet', f'.fractal/{seed}')
        _git(repo, 'commit', '-m', 'purge the seed copy')
    else:
        copy.write_text(target_edit, encoding='utf-8')
        _git(repo, 'commit', '-a', '-m', 'target edits the seed copy')
    (worktree / '.fractal' / seed / 'memory' / 'x.md').write_text(
        'child v2\n',
        encoding='utf-8',
    )
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the merge landed clean with the work on the target
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'resolved 1 conflicting path(s) under .fractal/' in result.stderr, (
        result.stderr
    )
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    assert (repo / 'f.txt').read_text(encoding='utf-8') == 'child work\n'
    assert _git(repo, 'status', '--porcelain').stdout == ''
    # the target holds its own answer for the path -- gone, or its own content
    # still tracked -- and the node holds its live seed or the target's content
    tracked = _git(repo, 'ls-files', f'.fractal/{seed}').stdout
    if target_edit is None:
        assert not (repo / '.fractal' / seed).exists()
        assert tracked == ''
    else:
        assert copy.read_text(encoding='utf-8') == target_edit
        assert f'.fractal/{seed}/memory/x.md' in tracked
    live = (worktree / '.fractal' / seed / 'memory' / 'x.md').read_text(
        encoding='utf-8'
    )
    assert live == node_copy


def test_merge_removes_an_own_seed_leaked_after_the_fork(
    tmp_path: pathlib.Path,
) -> None:
    """A copy of the node's seed leaked onto the root after the fork is removed.

    A hand copy of a node's live seed committed on the user node's branch
    after the node forked is a leak the root warns about, and it makes the
    node's next squash conflict add/add on every seed file the node has
    since edited. The conflict is only under ``.fractal/`` outside any scope
    root, so the merge resolves it to the root's content, strips the copy
    with the node's own seed, and lands; the root tracks no copy afterwards,
    and the advance grafts the node's live seed back untouched.
    """
    repo = _init_tree(tmp_path / 'lateleakrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    # the operator copies the node's tracked seed onto the root and commits it
    listing = _git(worktree, 'ls-files', '-z', '.fractal/main.task').stdout
    for relpath in filter(None, listing.split('\0')):
        (repo / relpath).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(worktree / relpath, repo / relpath)
    _git(repo, 'add', '-f', '.fractal/main.task')
    _git(repo, 'commit', '-m', 'leak the live seed')
    # the node edits its contract and works on
    contract = worktree / '.fractal' / 'main.task' / 'NODE.md'
    contract.write_text(
        contract.read_text(encoding='utf-8') + '\nEdited after the leak.\n',
        encoding='utf-8',
    )
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child edits its contract and works')
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the leak was named as the node's own, the add/add conflict resolved to
    # the root's content, and the merge landed the work with the copy gone
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (
        'Warning: main tracks seeds of main.task or its descendants, leaked by an'
        ' earlier merge: .fractal/main.task; this merge removes them'
    ) in result.stderr, result.stderr
    assert 'resolved 1 conflicting path(s) under .fractal/' in result.stderr, (
        result.stderr
    )
    assert '.fractal/main.task/NODE.md' in result.stderr, result.stderr
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    assert (repo / 'f.txt').read_text(encoding='utf-8') == 'child work\n'
    assert _git(repo, 'ls-files', '.fractal').stdout == ''
    assert not (repo / '.fractal' / 'main.task').exists()
    assert _git(repo, 'status', '--porcelain').stdout == ''
    # the advance grafted the live seed back: the node's contract is intact
    assert 'skipped advancing' not in result.stderr, result.stderr
    subject = _git(worktree, 'log', '-1', '--format=%s').stdout.strip()
    assert subject == 'merge main (post-squash)'
    live = contract.read_text(encoding='utf-8')
    assert live.endswith('Edited after the leak.\n')


def test_merge_refuses_a_mixed_conflict(tmp_path: pathlib.Path) -> None:
    """A conflict on the node's seed beside a real one stays the operator's.

    The self-resolution covers a squash whose every unmerged path is under
    ``.fractal/`` outside the node's scope roots; one real conflict beside the
    seed's makes the whole squash the operator's, so the merge restores the
    target and reports the conflict without resolving anything. The
    operator's hand squash then drops the stale seed path and resolves the
    work, and ``--continue`` lands with the seed stripped.
    """
    repo = _init_tree(tmp_path / 'mixedrepo')
    # a leaked copy of the node's memory on the target before the fork, purged
    # after it, so the node's live edit conflicts modify/delete
    leaked = repo / '.fractal' / 'main.task' / 'memory' / 'x.md'
    leaked.parent.mkdir(parents=True)
    leaked.write_text('leaked v1\n', encoding='utf-8')
    _git(repo, 'add', '.fractal/main.task')
    _git(repo, 'commit', '-m', 'leaked seed copy')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    _git(repo, 'rm', '-r', '--quiet', '.fractal/main.task')
    _git(repo, 'commit', '-m', 'purge leaked seed')
    # both sides edit the tracked file too
    (repo / 'tracked.txt').write_text('parent line\n', encoding='utf-8')
    _git(repo, 'add', 'tracked.txt')
    _git(repo, 'commit', '-m', 'parent edits tracked')
    (worktree / '.fractal' / 'main.task' / 'memory' / 'x.md').write_text(
        'live v2\n',
        encoding='utf-8',
    )
    (worktree / 'tracked.txt').write_text('child line\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child edits its seed and tracked')
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # refused as a whole: nothing resolved, the target restored
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert 'produced conflicts' in result.stderr, result.stderr
    assert 'resolved' not in result.stderr, result.stderr
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == main_head
    assert _git(repo, 'status', '--porcelain').stdout == ''
    assert not (repo / '.fractal' / 'main.task').exists()

    # the operator squashes by hand, drops the stale seed path, resolves the
    # work, and the continue lands with the seed stripped
    redo = subprocess.run(
        ['git', 'merge', '--squash', 'main.task'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
    )
    assert redo.returncode != 0, redo.stdout
    _git(repo, 'rm', '-f', '--quiet', '.fractal/main.task/memory/x.md')
    (repo / 'tracked.txt').write_text('resolved line\n', encoding='utf-8')
    _git(repo, 'add', 'tracked.txt')
    landed = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}', '--continue'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert landed.returncode == 0, (landed.stdout, landed.stderr)
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    assert (repo / 'tracked.txt').read_text(encoding='utf-8') == 'resolved line\n'
    assert _git(repo, 'ls-files', '.fractal/main.task').stdout == ''
    assert not (repo / '.fractal' / 'main.task').exists()
    assert _git(repo, 'status', '--porcelain').stdout == ''


@pytest.mark.parametrize(
    argnames='target',
    argvalues=['main', 'main.parent'],
    ids=['user-target', 'node-target'],
)
def test_merge_warns_about_leaked_seed_dirs_on_the_target(
    tmp_path: pathlib.Path,
    target: str,
) -> None:
    """A foreign node's seed the user node tracks draws a warning with a runnable remedy.

    The root owns no seed (its own dir is self-ignored) and every node seed
    is stripped on the way up, so a dotted seed the user node's branch tracks
    is a leak from a hand merge -- one that collides with that node's live
    seed on its every later merge of the root. Only a node's own squash
    removes its own copy, so the merge names the rest with the ``git rm -r``
    and commit remedy (not ``--cached``: a copy left on disk would collide
    with the node's next squash) and still lands, leaving them for the
    operator. The printed line runs as it is, and once it has the node's next
    merge lands without a collision refusal. A node target is never judged:
    its branch legitimately carries other nodes' seeds -- its ancestors' by
    fork, its descendants' by PREPARE merges, a sibling's by the advance --
    so the same foreign seed there draws no warning at all.
    """
    repo = _init_tree(tmp_path / 'leakedrepo')
    if target == 'main':
        target_dir = repo
    else:
        init = _run(repo, 'node', 'init', 'parent', '--agent', 'claude', '--local')
        assert init.returncode == 0, init.stderr
        target_dir = repo / '.worktrees' / 'main.parent'
    leaked = target_dir / '.fractal' / 'main.other' / 'NODE.md'
    leaked.parent.mkdir(parents=True)
    leaked.write_text('# leaked contract\n', encoding='utf-8')
    _git(target_dir, 'add', '-A')
    _git(target_dir, 'commit', '-m', 'leaked seed copy')
    # a child spawned from the target's own checkout nests under it
    init = _run(target_dir, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    branch = f'{target}.task'
    worktree = repo / '.worktrees' / branch
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the merge landed; the leak is the operator's to remove, so it stays tracked
    assert result.returncode == 0, (result.stdout, result.stderr)
    subject = _git(target_dir, 'log', '-1', '--format=%s').stdout.strip()
    assert subject == f'merge {branch}'
    tracked = _git(target_dir, 'ls-files', '.fractal').stdout
    assert '.fractal/main.other/NODE.md' in tracked
    if target != 'main':
        # a node target's tracked seeds are its own business: no warning
        assert 'leaked by an earlier merge' not in result.stderr, result.stderr
        return
    # the user target warned, naming the leaked dir and the hand remedy
    assert 'tracks node seed directories leaked by an earlier merge' in result.stderr
    assert '.fractal/main.other' in result.stderr, result.stderr
    remedy = next(
        line
        for line in result.stderr.splitlines()
        if line.startswith('Remove them with: ')
    )
    assert ' rm -r -- ' in remedy, remedy
    assert '--cached' not in remedy, remedy
    assert '&& git -C' in remedy, remedy
    assert "commit -m 'drop leaked node seeds'" in remedy, remedy
    # the remedy runs as printed and drops the leak from the index and disk
    removed = subprocess.run(
        ['bash', '-c', remedy.removeprefix('Remove them with: ')],
        cwd=f'{tmp_path}',
        capture_output=True,
        text=True,
    )
    assert removed.returncode == 0, (removed.stdout, removed.stderr)
    assert _git(target_dir, 'ls-files', '.fractal').stdout == ''
    assert not (target_dir / '.fractal' / 'main.other').exists()
    assert _git(target_dir, 'status', '--porcelain').stdout == ''
    # the node's branch still carries the seed from its fork, and its next
    # merge lands: neither a collision nor a leak
    (worktree / 'g.txt').write_text('more child work\n', encoding='utf-8')
    _git(worktree, 'add', 'g.txt')
    _git(worktree, 'commit', '-m', 'more child work')
    again = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert again.returncode == 0, (again.stdout, again.stderr)
    assert 'would overwrite untracked files' not in again.stderr, again.stderr
    assert 'leaked by an earlier merge' not in again.stderr, again.stderr
    assert (target_dir / 'g.txt').read_text(encoding='utf-8') == 'more child work\n'
    assert _git(target_dir, 'ls-files', '.fractal').stdout == ''


def test_merge_leaves_a_same_named_seed_under_another_prefix_for_the_remedy(
    tmp_path: pathlib.Path,
) -> None:
    """A leaked copy of the node's seed under another project prefix is the operator's.

    The strip removes exactly the node's seed at its own prefix and its
    descendants' at any depth, so a same-named copy under another
    ``.fractal/`` -- the node once lived at a sub-project path, a hand merge
    leaked its seed there, and the node was deleted and re-created at the
    repo root -- survives every merge. The leak check names it under the
    ``git rm -r`` remedy rather than as one this merge removes, so the
    warning never claims a removal that does not happen; the merge lands,
    the copy stays tracked until the operator acts, and the node's live seed
    at its new prefix is untouched.
    """
    repo = _init_tree(tmp_path / 'reprefixrepo')
    # a committed sub-project wiki -- the base-ref precondition for the init
    sub_wiki = repo / 'sub' / 'wiki' / '_index.md'
    sub_wiki.parent.mkdir(parents=True)
    sub_wiki.write_text('---\nname: sub\n---\n# sub\n\n***\n', encoding='utf-8')
    _git(repo, 'add', 'sub')
    _git(repo, 'commit', '-m', 'add sub wiki')
    # the node first lives under the sub-project; a hand merge leaks its seed
    # onto the root, and the node is deleted
    init = _run(
        repo, 'node', 'init', 'task', '--path', 'sub', '--agent', 'claude', '--local'
    )
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    _git(repo, 'merge', '--no-ff', '--no-edit', 'main.task')
    leaked = _git(repo, 'ls-files', 'sub/.fractal').stdout
    assert 'sub/.fractal/main.task/NODE.md' in leaked, leaked
    deleted = _run(repo, 'node', 'delete', f'--path={worktree}', '--force')
    assert deleted.returncode == 0, deleted.stderr
    # re-created at the repo root, the node forks with the leak in its tree
    # and works on
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', 'f.txt')
    _git(worktree, 'commit', '-m', 'child work')
    live = worktree / '.fractal' / 'main.task' / 'NODE.md'
    live_before = live.read_bytes()
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the merge landed; the copy under the old prefix is named for the hand
    # remedy, never claimed as removed, and stays tracked on the root
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'Squash-merged' in result.stdout
    assert (
        'Warning: main tracks node seed directories leaked by an earlier merge:'
        ' sub/.fractal/main.task'
    ) in result.stderr, result.stderr
    remedy = next(
        line
        for line in result.stderr.splitlines()
        if line.startswith('Remove them with: ')
    )
    assert ' rm -r -- sub/.fractal/main.task ' in remedy, remedy
    assert 'this merge removes them' not in result.stderr, result.stderr
    assert (repo / 'f.txt').read_text(encoding='utf-8') == 'child work\n'
    tracked = _git(repo, 'ls-files', 'sub/.fractal').stdout
    assert 'sub/.fractal/main.task/NODE.md' in tracked, tracked
    assert _git(repo, 'ls-files', '.fractal').stdout == ''
    assert _git(repo, 'status', '--porcelain').stdout == ''
    # the advance grafted the live seed back untouched
    assert 'skipped advancing' not in result.stderr, result.stderr
    subject = _git(worktree, 'log', '-1', '--format=%s').stdout.strip()
    assert subject == 'merge main (post-squash)'
    assert live.read_bytes() == live_before


def test_merge_warns_about_its_own_seed_leaked_onto_another_trees_root(
    tmp_path: pathlib.Path,
) -> None:
    """A ``--base`` merge into another tree's root judges the node's own seed by name.

    The user node's tracked seeds are picked out by the target's own name --
    its tree's nodes are ``<root>.<...>`` -- which a node merging across
    trees does not carry: its branch is named for its own root. The merging
    node's own seed and its descendants' are admitted by their own names as
    well, so a hand-leaked copy of that seed on the other tree's root draws
    the removal warning and the strip takes it out, instead of passing
    unjudged to collide with the node's live seed on every later merge.
    """
    repo = _init_tree(tmp_path / 'crosstreerepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', 'f.txt')
    _git(worktree, 'commit', '-m', 'child work')
    # a second tree rooted beside the first, and the node rebased onto its
    # root: the merge target is another tree's user node
    _git(repo, 'checkout', '-b', 'second')
    assert _run(repo, 'init').returncode == 0
    rebased = _run(repo, 'config', '_set', 'base=second', f'--path={worktree}')
    assert rebased.returncode == 0, rebased.stderr
    _git(worktree, 'commit', '-a', '-m', 'rebase onto second')
    # a hand copy of the node's seed committed on that root
    _git(repo, 'checkout', 'main.task', '--', '.fractal/main.task')
    _git(repo, 'commit', '-m', 'leaked seed copy')
    tracked = _git(repo, 'ls-files', '.fractal').stdout
    assert '.fractal/main.task/NODE.md' in tracked, tracked
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the leak was named as this merge's own removal, and it is gone
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'Squash-merged main.task into second' in result.stdout, result.stdout
    assert 'second tracks seeds of main.task or its descendants' in result.stderr, (
        result.stderr
    )
    assert '.fractal/main.task; this merge removes them' in result.stderr, result.stderr
    assert 'tracks node seed directories' not in result.stderr, result.stderr
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    assert (repo / 'f.txt').read_text(encoding='utf-8') == 'child work\n'
    assert _git(repo, 'ls-files', '.fractal').stdout == ''
    assert not (repo / '.fractal' / 'main.task').exists()
    assert _git(repo, 'status', '--porcelain').stdout == ''
    # the advance converged the node onto the other tree's root
    assert 'skipped advancing' not in result.stderr, result.stderr
    subject = _git(worktree, 'log', '-1', '--format=%s').stdout.strip()
    assert subject == 'merge second (post-squash)', (subject, result.stderr)
    assert _git(worktree, 'status', '--porcelain').stdout == ''


def test_merge_collision_guard_skips_a_seed_the_target_dropped_since_the_fork(
    tmp_path: pathlib.Path,
) -> None:
    """A ``.fractal/`` path the target untracked after the fork is no collision.

    The pre-squash guard refuses a ``.fractal/`` path the squash would add
    over an untracked copy on the target's disk. It diffs the node's branch
    from the merge-base, as the squash does, and only for paths HEAD lacks:
    a leaked seed the target tracked at the fork and later dropped from its
    index by hand -- the copy still on disk, untracked -- is not the node's
    addition, and the squash deletes it as the target did. The merge lands
    and leaves the operator's copy alone.
    """
    repo = _init_tree(tmp_path / 'droppedrepo')
    leaked = repo / '.fractal' / 'main.other' / 'NODE.md'
    leaked.parent.mkdir(parents=True)
    leaked.write_text('# leaked contract\n', encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'leaked seed copy')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    # the operator drops the leak from the index only, keeping the copy
    _git(repo, 'rm', '-r', '--cached', '--quiet', '.fractal/main.other')
    _git(repo, 'commit', '-m', 'drop the leaked seed from the index')
    assert leaked.is_file()
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the merge landed with no refusal and no warning; the copy is untouched
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'would overwrite untracked files' not in result.stderr, result.stderr
    assert 'leaked by an earlier merge' not in result.stderr, result.stderr
    assert (repo / 'f.txt').read_text(encoding='utf-8') == 'child work\n'
    assert leaked.read_text(encoding='utf-8') == '# leaked contract\n'
    assert _git(repo, 'ls-files', '.fractal').stdout == ''


def test_merge_collision_guard_skips_a_prefix_the_target_tracks(
    tmp_path: pathlib.Path,
) -> None:
    """A file the node turned into a directory is the squash's own type change.

    The pre-squash guard probes every parent prefix of a path the node added,
    so a file sitting where the squash creates a directory is caught. A
    prefix the target's HEAD tracks is not in the way: the node replaced that
    file with a directory, a type change the squash performs itself, and
    HEAD's copy is no untracked file to protect. The probe stops there, and
    the merge lands the directory in the file's place.
    """
    repo = _init_tree(tmp_path / 'typechangerepo')
    (repo / 'out').write_text('out-file\n', encoding='utf-8')
    _git(repo, 'add', 'out')
    _git(repo, 'commit', '-m', 'track out as a file')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    # the node replaces the file with a directory
    _git(worktree, 'rm', '--quiet', 'out')
    (worktree / 'out').mkdir()
    (worktree / 'out' / 'x').write_text('x\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'out: file to directory')
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the merge landed the type change: no refusal, the directory in place
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'Squash-merged' in result.stdout
    assert 'would overwrite untracked files' not in result.stderr, result.stderr
    assert (repo / 'out').is_dir()
    assert (repo / 'out' / 'x').read_text(encoding='utf-8') == 'x\n'
    assert _git(repo, 'ls-files', 'out').stdout.split() == ['out/x']
    assert _git(repo, 'status', '--porcelain').stdout == ''


def test_merge_into_a_node_target_never_judges_its_ancestors_seed(
    tmp_path: pathlib.Path,
) -> None:
    """A node target's tracked ancestor seed is its own, never a leak.

    A node forks from its parent's branch, which tracks the parent's seed, so
    every node below the root carries its ancestors' seeds by construction.
    Judging a node target would call each of those a leak on every merge into
    it, with a remedy that removes the parent's own machinery from the
    child's branch. Only the user node is judged: a grandchild's merge into
    the middle of a three-deep chain warns of nothing, lands, and leaves the
    ancestor seed tracked.
    """
    repo = _init_tree(tmp_path / 'chainrepo')
    init = _run(repo, 'node', 'init', 'p', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    parent = repo / '.worktrees' / 'main.p'
    _git(parent, 'add', '-A')
    _git(parent, 'commit', '-m', 'settle p estate')
    # each spawn nests under the caller (the loop's spawn sets _NODE to the
    # caller's seed dir), so the chain forks seed by seed
    node_dir = parent / '.fractal' / 'main.p'
    spawn = _run(
        repo, 'node', 'init', 'c', '--agent', 'claude', '--local', _NODE=str(node_dir)
    )
    assert spawn.returncode == 0, spawn.stderr
    child = repo / '.worktrees' / 'main.p.c'
    _git(child, 'add', '-A')
    _git(child, 'commit', '-m', 'settle c estate')
    node_dir = child / '.fractal' / 'main.p.c'
    spawn = _run(
        repo, 'node', 'init', 'd', '--agent', 'claude', '--local', _NODE=str(node_dir)
    )
    assert spawn.returncode == 0, spawn.stderr
    grandchild = repo / '.worktrees' / 'main.p.c.d'
    (grandchild / 'd.txt').write_text('grandchild work\n', encoding='utf-8')
    _git(grandchild, 'add', '-A')
    _git(grandchild, 'commit', '-m', 'grandchild work')
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{grandchild}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # no leak warning; the work landed and the ancestor seed is still tracked
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'leaked' not in result.stderr, result.stderr
    assert 'Squash-merged main.p.c.d into main.p.c' in result.stdout
    assert (child / 'd.txt').read_text(encoding='utf-8') == 'grandchild work\n'
    tracked = _git(child, 'ls-files', '.fractal').stdout
    assert '.fractal/main.p/NODE.md' in tracked, tracked
    assert '.fractal/main.p.c.d/' not in tracked, tracked
    assert _git(child, 'status', '--porcelain').stdout == ''


def test_merge_into_a_node_target_keeps_its_tracked_descendant_seed(
    tmp_path: pathlib.Path,
) -> None:
    """A child's seed its parent folded in for real survives the child's squash.

    A parent that merges a child with ``--no-ff`` (as its PREPARE step does)
    tracks the child's seed on its branch. The child's later squash into the
    parent never adds that seed -- the restore drops every ``.fractal/``
    addition -- and never removes the copy either: the strip runs only on
    the user node, where a tracked node seed can only be a leak. Deleting
    the parent's copy would reach the child's live seed on its next merge of
    the parent -- a mid-iteration child (its advance skipped for dirty work)
    merging the parent by hand would lose its own machinery to the parent's
    deletion. The copy stays, and the child's merge keeps its live seed.
    """
    repo = _init_tree(tmp_path / 'preparerepo')
    init = _run(repo, 'node', 'init', 'parent', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    parent = repo / '.worktrees' / 'main.parent'
    _git(parent, 'add', '-A')
    _git(parent, 'commit', '-m', 'settle parent estate')
    node_dir = parent / '.fractal' / 'main.parent'
    spawn = _run(
        repo, 'node', 'init', 'c', '--agent', 'claude', '--local', _NODE=str(node_dir)
    )
    assert spawn.returncode == 0, spawn.stderr
    child = repo / '.worktrees' / 'main.parent.c'
    _git(child, 'add', '-A')
    _git(child, 'commit', '-m', 'settle child estate')
    # the parent folds the child in for real, tracking its seed
    _git(parent, 'merge', '--no-ff', '--no-edit', 'main.parent.c')
    seed_tree = _git(parent, 'rev-parse', 'HEAD:.fractal/main.parent.c').stdout.strip()
    # the child works on and merges up mid-iteration: committed work beside
    # an uncommitted file, so its advance is skipped
    (child / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(child, 'add', 'f.txt')
    _git(child, 'commit', '-m', 'child work')
    (child / 'wip.txt').write_text('mid-iteration\n', encoding='utf-8')
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{child}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'skipped advancing' in result.stderr, result.stderr

    # the work landed and the parent's copy of the child's seed is untouched
    assert (parent / 'f.txt').read_text(encoding='utf-8') == 'child work\n'
    tracked = _git(parent, 'rev-parse', 'HEAD:.fractal/main.parent.c').stdout.strip()
    assert tracked == seed_tree
    assert _git(parent, 'status', '--porcelain').stdout == ''
    # the child's own merge of the parent keeps its live seed
    _git(child, 'merge', '--no-edit', 'main.parent')
    live = child / '.fractal' / 'main.parent.c'
    assert (live / 'NODE.md').is_file()
    assert (live / 'config.json').is_file()
    tracked = _git(child, 'ls-files', '.fractal/main.parent.c').stdout
    assert '.fractal/main.parent.c/config.json' in tracked
    assert (child / 'wip.txt').read_text(encoding='utf-8') == 'mid-iteration\n'


def test_merge_into_a_node_target_resolves_a_conflict_on_its_own_seed(
    tmp_path: pathlib.Path,
) -> None:
    """A child's edit to its parent's own seed resolves to the parent's and returns.

    A child forks with its parent's seed tracked, so an edit both make to
    the parent's contract conflicts in the squash. The conflict lies only
    under ``.fractal/`` outside the child's scope, so the merge resolves it
    to the target's own content -- the restore's answer -- and lands the
    work; a node target is never stripped, so the parent keeps its seed
    tracked as it was, and the advance carries the parent's content back to
    the child, whose copy of the contract is the parent's once the merge is
    done.
    """
    repo = _init_tree(tmp_path / 'nodeseedconflictrepo')
    init = _run(repo, 'node', 'init', 'p', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    parent = repo / '.worktrees' / 'main.p'
    _git(parent, 'add', '-A')
    _git(parent, 'commit', '-m', 'settle p estate')
    node_dir = parent / '.fractal' / 'main.p'
    spawn = _run(
        repo, 'node', 'init', 'c', '--agent', 'claude', '--local', _NODE=str(node_dir)
    )
    assert spawn.returncode == 0, spawn.stderr
    child = repo / '.worktrees' / 'main.p.c'
    _git(child, 'add', '-A')
    _git(child, 'commit', '-m', 'settle c estate')
    # both edit the parent's contract: the parent its live seed, the child
    # the copy it forked with, beside real work
    contract = parent / '.fractal' / 'main.p' / 'NODE.md'
    forked = contract.read_text(encoding='utf-8')
    contract.write_text(forked + '\nParent line.\n', encoding='utf-8')
    _git(parent, 'commit', '-a', '-m', 'p edits its contract')
    (child / '.fractal' / 'main.p' / 'NODE.md').write_text(
        forked + '\nChild line.\n', encoding='utf-8'
    )
    (child / 'c.txt').write_text('child work\n', encoding='utf-8')
    _git(child, 'add', '-A')
    _git(child, 'commit', '-m', 'c edits the contract and works')
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{child}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the conflict resolved to the parent's content and the work landed, the
    # parent's seed tracked and untouched by the commit
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'resolved 1 conflicting path(s) under .fractal/' in result.stderr, (
        result.stderr
    )
    assert '.fractal/main.p/NODE.md' in result.stderr, result.stderr
    assert _git(parent, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.p.c'
    assert (parent / 'c.txt').read_text(encoding='utf-8') == 'child work\n'
    parent_contract = contract.read_text(encoding='utf-8')
    assert parent_contract == forked + '\nParent line.\n'
    committed = _git(parent, 'show', '--stat', '--format=', 'HEAD').stdout
    assert '.fractal/' not in committed, committed
    tracked = _git(parent, 'ls-files', '.fractal').stdout
    assert '.fractal/main.p/NODE.md' in tracked, tracked
    assert _git(parent, 'status', '--porcelain').stdout == ''
    # the advance carried the parent's content back to the child
    assert 'skipped advancing' not in result.stderr, result.stderr
    subject = _git(child, 'log', '-1', '--format=%s').stdout.strip()
    assert subject == 'merge main.p (post-squash)', (subject, result.stderr)
    child_contract = (child / '.fractal' / 'main.p' / 'NODE.md').read_text(
        encoding='utf-8'
    )
    assert child_contract == parent_contract
    assert _git(child, 'status', '--porcelain').stdout == ''


def test_merge_remedies_quote_a_path_with_a_space(tmp_path: pathlib.Path) -> None:
    """The leaked-seed remedy the merge prints quotes its path for the paste back.

    A remedy is copy-paste material, and an unquoted path with a space splits
    into two words in the shell it is pasted into. The leaked-seed line
    carries a ``printf %q`` quoted path, so it runs as printed through
    ``bash -c`` and removes the leak.
    """
    repo = _init_tree(tmp_path / 'with space' / 'repo')
    leaked = repo / '.fractal' / 'main.other' / 'NODE.md'
    leaked.parent.mkdir(parents=True)
    leaked.write_text('# leaked contract\n', encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'leaked seed copy')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    # the leaked-seed remedy escapes the space and runs as printed
    remedy = next(
        line
        for line in result.stderr.splitlines()
        if line.startswith('Remove them with: ')
    )
    assert 'with\\ space/repo' in remedy, remedy
    removed = subprocess.run(
        ['bash', '-c', remedy.removeprefix('Remove them with: ')],
        cwd=f'{tmp_path}',
        capture_output=True,
        text=True,
    )
    assert removed.returncode == 0, (removed.stdout, removed.stderr)
    assert _git(repo, 'ls-files', '.fractal').stdout == ''
    assert not (repo / '.fractal' / 'main.other').exists()


def test_merge_warnings_print_a_non_ascii_path_readably(
    tmp_path: pathlib.Path,
) -> None:
    """The restore's warnings name a non-ASCII path as it is, not C-quoted.

    With git's default ``core.quotePath`` a ``--name-only`` listing octal-
    escapes a non-ASCII name inside double quotes; the warnings read their
    paths NUL-delimited, so the operator sees the file name as written, not
    its octal escapes.
    """
    repo = _init_tree(tmp_path / 'quotedrepo')
    # pin git's default path quoting so an operator-level quotePath=false can
    # never mask the case
    _git(repo, 'config', 'core.quotePath', 'true')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    # the child writes into a foreign seed under a non-ASCII name, beside work
    foreign = worktree / '.fractal' / 'main.other' / 'naïve.md'
    foreign.parent.mkdir()
    foreign.write_text('# foreign\n', encoding='utf-8')
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child writes a foreign seed')
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the removed warning names the path readably
    assert result.returncode == 0, (result.stdout, result.stderr)
    removed = next(
        line
        for line in result.stderr.splitlines()
        if 'the merge removed, since' in line
    )
    assert '.fractal/main.other/naïve.md' in removed, removed
    assert '\\303' not in result.stderr, result.stderr
    assert not (repo / '.fractal' / 'main.other').exists()
    assert (repo / 'f.txt').read_text(encoding='utf-8') == 'child work\n'


# ------ merge.sh: the squash footprint


@pytest.mark.parametrize(
    argnames=('scope', 'flags', 'stray', 'lands'),
    argvalues=[
        # a stray outside the scope refuses the squash
        (['--scope', 'docs'], [], 'outside.txt', False),
        # --ignore-scope lands it anyway
        (['--scope', 'docs'], ['--ignore-scope'], 'outside.txt', True),
        # in-scope work alone lands
        (['--scope', 'docs'], [], None, True),
        # a foreign .gitattributes is a stray like any other
        (['--scope', 'docs'], [], '.gitattributes', False),
        # an unscoped node has no footprint to refuse
        ([], [], 'outside.txt', True),
    ],
    ids=[
        'scoped-refused',
        'scoped-overridden',
        'scoped-landing',
        'scoped-foreign-attributes',
        'unscoped',
    ],
)
def test_merge_refuses_a_squash_outside_the_nodes_scope(
    tmp_path: pathlib.Path,
    scope: list[str],
    flags: list[str],
    stray: Optional[str],
    lands: bool,
) -> None:
    """A squash changing paths outside the node's scope is refused unless overridden.

    Commit-time scope is bypassable -- ``fractal commit --ignore-scope``, the
    ``--force`` backstop, a raw ``git commit`` -- so the squash is the one
    point that sees the node's whole offering. A path outside every scope
    root (the node's project wiki excepted) refuses the merge, naming the
    paths and both remedies -- widening the scope, with the ``fractal
    commit`` that records it, or ``--ignore-scope`` -- and a fresh merge
    restores the target. The worktree-root ``.gitattributes`` is admitted
    only as init's own edit -- the target's content plus exactly the two
    lines the wiki tool appends, which a first squash carries to a target
    that lacks them; a foreign attribute line beside them is out of scope
    like any other path, even when the target has no ``.gitattributes`` for
    it to be told apart from. ``--ignore-scope`` lands the offering as it
    is, and a repo-root node with no scope is unrestricted.
    """
    repo = _init_tree(tmp_path / 'scoperepo')
    init = _run(repo, 'node', 'init', 'task', *scope, '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    (worktree / 'docs').mkdir()
    (worktree / 'docs' / 'a.md').write_text('# a\n', encoding='utf-8')
    if stray == 'outside.txt':
        (worktree / stray).write_text('outside the scope\n', encoding='utf-8')
    elif stray == '.gitattributes':
        # a foreign line beside init's own two, on a target that tracks no
        # .gitattributes at all
        with (worktree / stray).open('a', encoding='utf-8') as attributes:
            attributes.write('*.bin binary\n')
    # raw git: `fractal commit` would refuse the out-of-scope path itself; a
    # first squash carries init's own .gitattributes edit beside the seed
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work in and out of scope')
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}', *flags],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    if lands:
        # the in-scope work and init's .gitattributes edit are tracked, and
        # the stray path landed as offered
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert (
            _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
        )
        tracked = _git(repo, 'ls-files', '.gitattributes', 'docs/a.md').stdout.split()
        assert tracked == ['.gitattributes', 'docs/a.md']
        if stray is not None:
            assert (repo / stray).read_text(encoding='utf-8') == 'outside the scope\n'
    else:
        # refused, naming the stray path alone -- not the in-scope one, nor
        # init's own .gitattributes edit -- and both remedies
        assert result.returncode != 0, (result.stdout, result.stderr)
        assert 'outside its scope' in result.stderr, result.stderr
        for path in ('docs/a.md', 'outside.txt', '.gitattributes'):
            assert (path in result.stderr) is (path == stray), (path, result.stderr)
        assert 'config set scope=' in result.stderr, result.stderr
        assert 'fractal commit' in result.stderr, result.stderr
        assert '--ignore-scope' in result.stderr, result.stderr
        # the target is restored: HEAD unmoved, nothing staged or left on disk
        assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == main_head
        assert _git(repo, 'status', '--porcelain').stdout == ''
        assert not (repo / 'outside.txt').exists()
        assert not (repo / 'docs').exists()


@pytest.mark.parametrize(
    argnames='attributes',
    argvalues=['* text=auto\n', '* text=auto', '\n* text=auto\n', '* text=auto  \n'],
    ids=[
        'trailing-newline',
        'no-trailing-newline',
        'leading-blank-line',
        'trailing-spaces',
    ],
)
def test_merge_admits_init_attributes_over_a_targets_own_lines(
    tmp_path: pathlib.Path,
    attributes: str,
) -> None:
    """Init's ``.gitattributes`` edit passes the footprint over a target's own lines.

    A target that already tracks a ``.gitattributes`` -- with or without a
    trailing newline, opening with a blank line, or carrying trailing
    whitespace -- receives exactly the two lines the wiki tool appends
    through a scoped node's first squash. The footprint check admits the
    edit as init's own when the staged content is HEAD's followed by only
    those lines -- HEAD's bytes as they are, never a stripped read that
    would drop the blank line or the whitespace and miss the prefix -- so
    the scoped landing succeeds and the target's file is its original plus
    the two lines, once.
    """
    repo = _init_tree(tmp_path / 'attrsrepo')
    (repo / '.gitattributes').write_text(attributes, encoding='utf-8')
    _git(repo, 'add', '.gitattributes')
    _git(repo, 'commit', '-m', 'own attributes')
    init = _run(
        repo, 'node', 'init', 'task', '--scope', 'docs', '--agent', 'claude', '--local'
    )
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    (worktree / 'docs').mkdir()
    (worktree / 'docs' / 'a.md').write_text('# a\n', encoding='utf-8')
    # raw git: a first squash carries init's own .gitattributes edit beside
    # the seed and the in-scope work
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work with init scaffolding')
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the scoped landing succeeded, and the target's .gitattributes is its
    # original followed by init's two lines, once
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    landed = _git(repo, 'show', 'HEAD:.gitattributes').stdout
    assert landed.startswith(attributes.rstrip('\n')), landed
    lines = [line for line in landed.splitlines() if line]
    assert lines == [
        attributes.strip('\n'),
        '# Wiki index merge driver',
        '**/_index.md merge=wiki',
    ]
    assert (repo / 'docs' / 'a.md').read_text(encoding='utf-8') == '# a\n'
    assert _git(repo, 'status', '--porcelain').stdout == ''


def test_merge_strips_a_leaked_cross_project_descendant_seed_without_a_scope_refusal(
    tmp_path: pathlib.Path,
) -> None:
    """A leaked descendant seed the strip removes never trips the footprint check.

    The strip removes every copy of the node's own and descendant seeds the
    target tracks -- a sub-project descendant's under ``<project>/.fractal/``
    too -- so the staged squash carries their deletion. Those paths are the
    merge's own doing, not the node's offering: the footprint check judges
    the squash minus every ``.fractal/`` path, so a scoped node lands its
    work while the leak leaves the target with the warning.
    """
    repo = _init_tree(tmp_path / 'crossprojectrepo')
    init = _run(
        repo, 'node', 'init', 'a', '--scope', 'src', '--agent', 'claude', '--local'
    )
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.a'
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    # a descendant's seed under a sub-project, leaked onto the target
    leaked = repo / 'app' / '.fractal' / 'main.a.b' / 'NODE.md'
    leaked.parent.mkdir(parents=True)
    leaked.write_text('# leaked descendant contract\n', encoding='utf-8')
    _git(repo, 'add', '-f', 'app/.fractal/main.a.b')
    _git(repo, 'commit', '-m', 'leaked descendant seed')
    # the node's in-scope work
    (worktree / 'src').mkdir()
    (worktree / 'src' / 'x.py').write_text('x = 1\n', encoding='utf-8')
    _git(worktree, 'add', 'src')
    _git(worktree, 'commit', '-m', 'child work')
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the merge landed the work and removed the leak, with the warning and no
    # scope refusal
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'tracks seeds of main.a or its descendants' in result.stderr, result.stderr
    assert 'app/.fractal/main.a.b; this merge removes them' in result.stderr, (
        result.stderr
    )
    assert 'outside its scope' not in result.stderr, result.stderr
    assert (repo / 'src' / 'x.py').read_text(encoding='utf-8') == 'x = 1\n'
    assert _git(repo, 'ls-files', 'app/.fractal').stdout == ''
    assert _git(repo, 'status', '--porcelain').stdout == ''


def test_merge_continue_refuses_a_squash_outside_the_nodes_scope(
    tmp_path: pathlib.Path,
) -> None:
    """``--continue`` runs the footprint check too, leaving the staged squash.

    A hand-redone squash is the operator's own state, so a footprint refusal
    on the continue arm names the paths and leaves the staged squash in place
    -- as every other continue-arm failure does -- for the operator to prune
    by hand or land with ``--ignore-scope``.
    """
    repo = _init_tree(tmp_path / 'scopecontinuerepo')
    init = _run(
        repo, 'node', 'init', 'task', '--scope', 'docs', '--agent', 'claude', '--local'
    )
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    (worktree / 'docs').mkdir()
    (worktree / 'docs' / 'a.md').write_text('# a\n', encoding='utf-8')
    (worktree / 'outside.txt').write_text('outside the scope\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work in and out of scope')
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    # the operator squashes by hand and hands the tail to the continue
    _git(repo, 'merge', '--squash', 'main.task')
    merge_sh = _scripts_dir() / 'merge.sh'
    refused = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}', '--continue'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # refused with the path named, the staged squash left in place
    assert refused.returncode != 0, (refused.stdout, refused.stderr)
    assert 'outside its scope' in refused.stderr, refused.stderr
    assert 'outside.txt' in refused.stderr, refused.stderr
    assert 'staged squash is left in place' in refused.stderr, refused.stderr
    # both remedies name the continue arm: land as it is, or widen the
    # scope and redo the squash (a node commit after the hand squash
    # refuses --continue)
    assert '--continue --ignore-scope' in refused.stderr, refused.stderr
    assert 'redo the squash' in refused.stderr, refused.stderr
    assert 'merge --squash main.task' in refused.stderr, refused.stderr
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == main_head
    staged = subprocess.run(
        ['git', 'diff', '--cached', '--quiet'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
    )
    assert staged.returncode != 0, 'the staged squash was discarded'

    # the override lands the same staged squash
    landed = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}', '--continue', '--ignore-scope'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert landed.returncode == 0, (landed.stdout, landed.stderr)
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    assert (repo / 'docs' / 'a.md').is_file()
    assert (repo / 'outside.txt').read_text(encoding='utf-8') == 'outside the scope\n'
    assert _git(repo, 'status', '--porcelain').stdout == ''


# ------ merge.sh: sub-project nodes


@pytest.mark.parametrize(
    argnames=('scope', 'inside', 'strays'),
    argvalues=[
        # work outside the project is a stray
        ([], ['app/feature.txt'], ['outside.txt']),
        # the root's wiki is outside a sub-project
        ([], ['app/wiki/note.md'], ['wiki/note.md']),
        # a scope is judged inside the project
        (['--scope', 'docs'], ['app/docs/a.md'], ['app/other.txt', 'root.txt']),
    ],
    ids=['project-bound', 'root-wiki-refused', 'scoped-in-project'],
)
def test_merge_bounds_a_sub_project_node_to_its_project(
    tmp_path: pathlib.Path,
    scope: list[str],
    inside: list[str],
    strays: list[str],
) -> None:
    """A sub-project node's footprint is judged inside its project dir.

    ``fractal commit`` bounds such a node to ``<project>/`` -- its wiki and
    seed live there, so the repo-root wiki is as foreign as any other path
    outside the project -- and nests its scope roots under the project, so
    a root of ``docs`` admits ``<project>/docs/`` alone, not the rest of the
    project nor the repo root. The footprint check judges the squash by the
    same boundaries: a path outside them refuses the merge naming exactly
    those paths, while the in-scope work and init's own worktree-root
    ``.gitattributes`` edit pass, and the node's seed under
    ``<project>/.fractal/`` is stripped rather than landed.
    """
    repo = _init_tree(tmp_path / 'subprojectrepo')
    # a committed sub-project wiki -- the base-ref precondition for the init
    app_wiki = repo / 'app' / 'wiki' / '_index.md'
    app_wiki.parent.mkdir(parents=True)
    app_wiki.write_text('---\nname: app\n---\n# app\n\n***\n', encoding='utf-8')
    _git(repo, 'add', 'app')
    _git(repo, 'commit', '-m', 'add app wiki')
    init = _run(
        repo,
        'node',
        'init',
        'feature',
        '--path',
        'app',
        *scope,
        '--agent',
        'claude',
        '--local',
    )
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.feature'
    for path in (*inside, *strays):
        (worktree / path).parent.mkdir(parents=True, exist_ok=True)
        (worktree / path).write_text(f'{path}\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'work in and out of the project')
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    merge_sh = _scripts_dir() / 'merge.sh'
    refused = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # refused naming exactly the paths outside the boundaries -- not the
    # in-scope ones, nor init's .gitattributes edit; the target restored
    assert refused.returncode != 0, (refused.stdout, refused.stderr)
    assert 'outside its scope: ' in refused.stderr, refused.stderr
    named = refused.stderr.split('outside its scope: ', 1)[1].split(';', 1)[0]
    assert named.split(', ') == sorted(strays), refused.stderr
    assert '.gitattributes' not in refused.stderr, refused.stderr
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == main_head
    assert _git(repo, 'status', '--porcelain').stdout == ''

    # dropped from the branch, the squash lands the in-scope work and init's
    # edit, never the seed
    _git(worktree, 'rm', '--quiet', *strays)
    _git(worktree, 'commit', '-m', 'drop the stray files')
    seed_tree = _git(worktree, 'rev-parse', 'HEAD:app/.fractal/main.feature').stdout
    landed = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert landed.returncode == 0, (landed.stdout, landed.stderr)
    tracked = _git(repo, 'ls-files', '.gitattributes', *inside).stdout.split()
    assert tracked == ['.gitattributes', *inside]
    for path in strays:
        assert not (repo / path).exists(), path
    assert _git(repo, 'ls-files', 'app/.fractal').stdout == ''
    assert not (repo / 'app' / '.fractal').exists()
    # the advance moved the node onto the target's tree with its seed under
    # <project>/.fractal/ grafted back unchanged
    assert 'skipped advancing' not in landed.stderr, landed.stderr
    subject = _git(worktree, 'log', '-1', '--format=%s').stdout.strip()
    assert subject == 'merge main (post-squash)'
    grafted = _git(worktree, 'rev-parse', 'HEAD:app/.fractal/main.feature').stdout
    assert grafted == seed_tree
    assert _git(worktree, 'status', '--porcelain').stdout == ''


def test_merge_strips_a_nested_descendant_seed_from_a_no_ff_parent(
    tmp_path: pathlib.Path,
) -> None:
    """A sub-project grandchild's seed a parent merged for real never lands.

    A parent that folds a child in with a real merge (as its PREPARE step
    does) tracks that child's seed on its branch -- under the child's
    ``<project>/.fractal/`` when the child is a sub-project node of a
    repo-root parent. The parent's squash then offers both its own seed and
    the nested one: the strip covers descendants at any depth and prefix, so
    the target gains only the work, while the advance grafts both seed trees
    back onto the parent byte for byte.
    """
    repo = _init_tree(tmp_path / 'nestedrepo')
    # a committed sub-project wiki -- the base-ref precondition for the init
    app_wiki = repo / 'app' / 'wiki' / '_index.md'
    app_wiki.parent.mkdir(parents=True)
    app_wiki.write_text('---\nname: app\n---\n# app\n\n***\n', encoding='utf-8')
    _git(repo, 'add', 'app')
    _git(repo, 'commit', '-m', 'add app wiki')
    init = _run(repo, 'node', 'init', 'a', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    parent = repo / '.worktrees' / 'main.a'
    _git(parent, 'add', '-A')
    _git(parent, 'commit', '-m', 'settle node scaffolding')
    # the parent spawns a child into the sub-project (the loop's spawn sets
    # _NODE to the caller's seed dir; --path selects the child's project)
    node_dir = parent / '.fractal' / 'main.a'
    spawn = _run(
        repo,
        'node',
        'init',
        'b',
        '--path',
        'app',
        '--agent',
        'claude',
        '--local',
        _NODE=str(node_dir),
    )
    assert spawn.returncode == 0, spawn.stderr
    child = repo / '.worktrees' / 'main.a.b'
    (child / 'app' / 'gc.txt').write_text('grandchild work\n', encoding='utf-8')
    _git(child, 'add', '-A')
    _git(child, 'commit', '-m', 'grandchild work')
    # the parent folds the child in with a real merge, tracking its seed
    _git(parent, 'merge', '--no-ff', '--no-edit', 'main.a.b')
    seed_trees = {
        path: _git(parent, 'rev-parse', f'HEAD:{path}').stdout.strip()
        for path in ('.fractal/main.a', 'app/.fractal/main.a.b')
    }
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{parent}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'skipped advancing' not in result.stderr, result.stderr

    # the target gained the work and no seed at any depth...
    assert (repo / 'app' / 'gc.txt').read_text(encoding='utf-8') == 'grandchild work\n'
    tracked = _git(repo, 'ls-files').stdout.splitlines()
    assert not any('.fractal/' in path for path in tracked), tracked
    assert not (repo / 'app' / '.fractal').exists()
    # ...and the parent's branch carries both seed trees unchanged
    for path, tree in seed_trees.items():
        assert _git(parent, 'rev-parse', f'HEAD:{path}').stdout.strip() == tree


# ------ merge.sh: failing after the squash


def test_merge_reports_the_target_restored_past_a_ref_lock(
    tmp_path: pathlib.Path,
) -> None:
    """A commit refused by a ref lock leaves the target restored, and says so.

    ``git reset --hard HEAD`` writes the index and worktree before it moves
    the ref, so with ``refs/heads/<target>.lock`` held by another process the
    reset exits non-zero after restoring everything that matters -- the
    target is clean and out of the squash. The failure path judges
    "restored" by that state, not by the reset's exit status, so the
    operator is told the truth instead of sent to run a reset that would
    fail the same way.
    """
    repo = _init_tree(tmp_path / 'reflockrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    # another git process holds the target's ref for the whole merge, so the
    # squash stages but its commit cannot move the branch
    lock_path = pathlib.Path(
        _git(repo, 'rev-parse', '--git-path', 'refs/heads/main.lock').stdout.strip()
    )
    if not lock_path.is_absolute():
        lock_path = repo / lock_path
    lock_path.touch()
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    lock_path.unlink()

    # the merge failed at the commit and reports the target restored -- which
    # it is: HEAD unmoved, nothing staged or on disk, no squash state
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert 'failed to commit the squash-merge of main.task' in result.stderr, (
        result.stderr
    )
    assert 'the parent worktree has been restored' in result.stderr, result.stderr
    assert 'could NOT be restored' not in result.stderr, result.stderr
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == main_head
    assert _git(repo, 'status', '--porcelain').stdout == ''
    assert not (repo / '.git' / 'SQUASH_MSG').exists()
    assert not (repo / 'f.txt').exists()


def test_merge_resets_a_squash_git_aborted_after_staging(
    tmp_path: pathlib.Path,
) -> None:
    """A squash git abandons after writing the index is reset, and reported as such.

    ``git merge --squash`` writes the index and worktree before its squash
    message, so a marker it cannot write -- a stale ``SQUASH_MSG`` that is a
    directory -- kills it with the whole squash staged on the target and no
    conflict to show for it. The target was clean before the squash, so what
    is staged is the squash's own: the merge resets it, removes the stale
    marker, and reports the failure as one after staging -- a failure before
    staging would leave the squash, seed included, for the target's next
    commit to absorb. The next merge lands.
    """
    repo = _init_tree(tmp_path / 'stalemarkerrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    # a stale squash marker git cannot write: a directory in its place
    marker = repo / '.git' / 'SQUASH_MSG'
    marker.mkdir()
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the merge failed once the squash was staged and reports the target
    # restored -- which it is: HEAD unmoved, nothing staged or on disk, the
    # stale marker gone
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert (
        'failed after staging; the parent worktree has been restored'
    ) in result.stderr, result.stderr
    assert 'before staging' not in result.stderr, result.stderr
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == main_head
    assert _git(repo, 'status', '--porcelain').stdout == ''
    assert not marker.exists()
    assert not (repo / 'f.txt').exists()

    # the next merge lands
    again = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert again.returncode == 0, (again.stdout, again.stderr)
    assert 'Squash-merged' in again.stdout
    assert (repo / 'f.txt').read_text(encoding='utf-8') == 'child work\n'
    assert _git(repo, 'status', '--porcelain').stdout == ''


# ------ merge.sh: an interrupt once the target is settled


@pytest.mark.parametrize(
    argnames='arm',
    argvalues=[[], ['--continue']],
    ids=['fresh', 'continue'],
)
def test_merge_interrupt_during_the_commit_hook_finishes_the_merge(
    tmp_path: pathlib.Path,
    arm: list[str],
) -> None:
    """A SIGINT while ``git commit`` runs its hook finishes the landed merge.

    ``git commit`` moves the target's ref before its post-commit hook runs,
    so an interrupt during the hook reaches the script while the commit is
    still its foreground child: the pre-commit trap of either arm is armed,
    yet the squash has landed. Both traps compare the target's HEAD with the
    one recorded before the squash -- moved means landed -- and finish the
    merge like any other: advance the child, close the event completed,
    report the landed squash, exit 0. Neither reports a "restored" target
    (the reset --hard HEAD would restore nothing) nor a "staged squash left
    in place" that the commit already consumed.

    The signal goes to the process group: bash acts on a SIGINT it receives
    while waiting on a child only when that child dies of it, so the hook
    and its sleep must take the signal too.
    """
    repo = _init_tree(tmp_path / 'hookrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    # the continue arm picks up an operator's hand squash
    if arm:
        _git(repo, 'merge', '--squash', 'main.task')
    # the hook marks the ref update and holds the commit open past it at a
    # gate; a marker after the gate shows whether the signal cut it short
    ready = tmp_path / 'gate_ready'
    release = tmp_path / 'gate_release'
    slept = tmp_path / 'slept'
    hook = repo / '.git' / 'hooks' / 'post-commit'
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(
        '#!/usr/bin/env bash\n'
        f'touch "{ready}"\n'
        f'while [[ ! -e "{release}" ]]; do sleep 0.05; done\n'
        f'touch "{slept}"\n',
        encoding='utf-8',
    )
    hook.chmod(0o755)
    merge_sh = _scripts_dir() / 'merge.sh'
    proc = subprocess.Popen(
        ['bash', f'{merge_sh}', f'{worktree}', *arm],
        cwd=f'{repo}',
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_cli_env(),
        start_new_session=True,
    )
    try:
        parked = _await_gate(ready, repo, deadline=time.monotonic() + 60)
        assert parked, 'the commit never reached its hook'
        os.killpg(proc.pid, signal.SIGINT)
        # released right behind the signal: a hook the signal missed runs on
        # to its marker instead of stalling the wait
        release.touch()
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        release.touch()
        _reap_group(proc)

    # the signal cut the hook short, and the merge finished anyway: the squash
    # commit on the target, the child advanced and clean, the event closed
    # completed, and no failure wording
    assert not slept.exists(), 'the signal never reached the commit'
    assert proc.returncode == 0, (stdout, stderr)
    assert 'Squash-merged main.task into main' in stdout, (stdout, stderr)
    assert 'interrupted' not in stderr, stderr
    assert 'restored' not in stderr, stderr
    assert 'left in place' not in stderr, stderr
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    assert (repo / 'f.txt').read_text(encoding='utf-8') == 'child work\n'
    assert _git(repo, 'status', '--porcelain').stdout == ''
    assert not (repo / '.git' / 'SQUASH_MSG').exists()
    subject = _git(worktree, 'log', '-1', '--format=%s').stdout.strip()
    assert subject == 'merge main (post-squash)', (subject, stderr)
    assert _git(worktree, 'status', '--porcelain').stdout == ''
    activity = _run(repo, 'node', 'activity', 'main', '--json')
    assert activity.returncode == 0, activity.stderr
    rows = json.loads(activity.stdout)
    merges = [row['status'] for row in rows if row['event'] == 'merge']
    assert merges == ['completed'], rows


def test_merge_interrupt_after_a_skipped_advance_warns_once(
    tmp_path: pathlib.Path,
) -> None:
    """A SIGINT in the event close after a skipped advance adds no second warning.

    Once the clobber guard skips the advance the child is untouched and the
    merge is settled, so an interrupt while the event closes has nothing to
    finish or roll back: the trap resets the child only while an advance is
    underway -- never onto the commit the guard refused, which would write
    the target's copy over the private file -- and warns of an interrupted
    advance only then, so the skip's own warning stands alone. The event
    still closes completed and the landed squash is reported with exit 0.

    The event close is held open at a gate with a ``fractal`` shim that parks
    before running the real ``event _end``, and the signal goes to the
    process group so the shim dies of it and bash acts on it.
    """
    repo = _init_tree(tmp_path / 'skipinterruptrepo')
    (repo / '.gitignore').write_text('local.env\n', encoding='utf-8')
    _git(repo, 'add', '.gitignore')
    _git(repo, 'commit', '-m', 'ignore local.env')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the child holds a private ignored copy beside committed work
    (worktree / 'local.env').write_text('PRIVATE SECRET\n', encoding='utf-8')
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    child_head = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    # the target force-tracks its own copy of the ignored path after the fork
    (repo / 'local.env').write_text('TARGET COPY\n', encoding='utf-8')
    _git(repo, 'add', '-f', 'local.env')
    _git(repo, 'commit', '-m', 'track a local.env')
    bindir = _fractal_shim_holding(tmp_path, on='event _end')
    ready = bindir / 'gate_ready'
    release = bindir / 'gate_release'
    env = _cli_env()
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    merge_sh = _scripts_dir() / 'merge.sh'
    proc = subprocess.Popen(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        parked = _await_gate(ready, repo, deadline=time.monotonic() + 60)
        assert parked, 'the event close never started'
        os.killpg(proc.pid, signal.SIGINT)
        # released right behind the signal: a shim the signal missed runs on
        # instead of stalling the wait
        release.touch()
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        release.touch()
        _reap_group(proc)

    # the signal cut the first event close short and the trap closed it again;
    # the landed squash is reported, the skip's one warning names the path in
    # the way, and the private copy and the child's HEAD are untouched
    calls = (bindir / 'calls').read_text(encoding='utf-8').splitlines()
    assert sum('event _end' in call for call in calls) == 2, calls
    assert proc.returncode == 0, (stdout, stderr)
    assert 'Squash-merged main.task into main' in stdout, (stdout, stderr)
    assert stderr.count('skipped advancing') == 1, stderr
    assert 'now tracks: local.env;' in stderr, stderr
    assert 'interrupted' not in stderr, stderr
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    assert (worktree / 'local.env').read_text(encoding='utf-8') == 'PRIVATE SECRET\n'
    assert _git(worktree, 'rev-parse', 'HEAD').stdout.strip() == child_head
    assert _git(worktree, 'status', '--porcelain').stdout == ''
    activity = _run(repo, 'node', 'activity', 'main', '--json')
    assert activity.returncode == 0, activity.stderr
    rows = json.loads(activity.stdout)
    merges = [row['status'] for row in rows if row['event'] == 'merge']
    assert merges == ['completed'], rows


def test_merge_interrupt_in_a_no_op_merges_event_close_reports_the_no_op(
    tmp_path: pathlib.Path,
) -> None:
    """A SIGINT while a no-op merge closes its event reports the no-op alone.

    A re-merge that offers nothing runs no advance, so its interrupt trap
    has no child update to finish or roll back and no reason to warn: it
    closes the event completed and prints the arm's own summary, exactly
    the "Nothing to merge" line a quiet run prints, with exit 0.

    The event close is held open at a gate with a ``fractal`` shim that parks
    before running the real ``event _end``, and the signal goes to the
    process group so the shim dies of it and bash acts on it.
    """
    repo = _init_tree(tmp_path / 'noopinterruptrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # a first merge lands the scaffolding, so the re-merge offers nothing
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    merge_sh = _scripts_dir() / 'merge.sh'
    first = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert first.returncode == 0, (first.stdout, first.stderr)
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    child_head = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    bindir = _fractal_shim_holding(tmp_path, on='event _end')
    ready = bindir / 'gate_ready'
    release = bindir / 'gate_release'
    env = _cli_env()
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    proc = subprocess.Popen(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        parked = _await_gate(ready, repo, deadline=time.monotonic() + 60)
        assert parked, 'the event close never started'
        os.killpg(proc.pid, signal.SIGINT)
        # released right behind the signal: a shim the signal missed runs on
        # instead of stalling the wait
        release.touch()
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        release.touch()
        _reap_group(proc)

    # the signal cut the first event close short and the trap closed it again;
    # the no-op is reported as the single stdout line, with no advance
    # warning; neither side moved, and both merge events closed completed
    calls = (bindir / 'calls').read_text(encoding='utf-8').splitlines()
    assert sum('event _end' in call for call in calls) == 2, calls
    assert proc.returncode == 0, (stdout, stderr)
    assert stdout == 'Nothing to merge: main.task has no changes for main\n', stderr
    assert 'skipped advancing' not in stderr, stderr
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == main_head
    assert _git(worktree, 'rev-parse', 'HEAD').stdout.strip() == child_head
    assert _git(repo, 'status', '--porcelain').stdout == ''
    assert not (repo / '.git' / 'SQUASH_MSG').exists()
    activity = _run(repo, 'node', 'activity', 'main', '--json')
    assert activity.returncode == 0, activity.stderr
    rows = json.loads(activity.stdout)
    merges = [row['status'] for row in rows if row['event'] == 'merge']
    assert merges == ['completed', 'completed'], rows


# ------ merge.sh: an interrupt while the merge event opens


def test_merge_interrupt_during_the_event_start_fails_the_event(
    tmp_path: pathlib.Path,
) -> None:
    """A SIGINT while the merge event opens closes the row failed, never active.

    ``merge.sh`` arms its interrupt trap before the ``fractal event _start``
    that opens the merge row: bash runs the trap only once the substitution
    has returned the id, so an interrupt landing while the start still runs
    -- the row committed, the call not yet exited -- closes that very row as
    failed. Nothing has touched the target yet, so it stays clean at the
    same HEAD, and the script exits non-zero.

    The start is held open at a gate with a ``fractal`` shim that runs the
    real ``event _start`` and parks before exiting, and the signal goes to
    the process group -- the Ctrl-C shape -- so the shim dies of it and bash
    acts on it.
    """
    repo = _init_tree(tmp_path / 'eventstartrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    main_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    bindir = _fractal_shim_lingering(tmp_path, on='event _start')
    ready = bindir / 'gate_ready'
    release = bindir / 'gate_release'
    env = _cli_env()
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    merge_sh = _scripts_dir() / 'merge.sh'
    proc = subprocess.Popen(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        parked = _await_gate(ready, repo, deadline=time.monotonic() + 60)
        assert parked, 'the event start never returned'
        os.killpg(proc.pid, signal.SIGINT)
        # released right behind the signal: a shim the signal missed runs on
        # instead of stalling the wait
        release.touch()
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        release.touch()
        _reap_group(proc)

    # the script aborted, the target is untouched, and the one merge row the
    # start opened is closed failed
    assert proc.returncode != 0, (stdout, stderr)
    assert 'Squash-merged' not in stdout, stdout
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == main_head
    assert _git(repo, 'status', '--porcelain').stdout == ''
    assert not (repo / '.git' / 'SQUASH_MSG').exists()
    assert not (repo / 'f.txt').exists()
    activity = _run(repo, 'node', 'activity', 'main', '--json')
    assert activity.returncode == 0, activity.stderr
    rows = json.loads(activity.stdout)
    merges = [row['status'] for row in rows if row['event'] == 'merge']
    assert merges == ['failed'], rows


# ------ fractal node merge: the verb around the script


def test_merge_serializes_concurrent_sibling_merges(tmp_path: pathlib.Path) -> None:
    """Two sibling merges racing into one target both land, one after the other.

    ``git merge --squash`` locks the target's index only for its final write,
    so two sibling merges both pass their preflight and interleave: the
    loser's files land untracked in the target, the winner's index write
    drops the loser's staged entries, and the loser's ``reset --hard`` cannot
    undo untracked files. ``fractal node merge`` holds one lock per repo
    around the script, so a merge started while another is inside its squash
    parks before its script runs, and both land -- one commit each, the
    second's on top, the target clean, no squash state behind.

    The first squash is held open at a gate with a ``git`` shim that parks
    before running the real ``merge --squash``; the second squash only marks
    its start, so a merge that reaches it while the first is parked shows.
    """
    repo = _init_tree(tmp_path / 'racerepo')
    names = ('a', 'b')
    for name in names:
        init = _run(repo, 'node', 'init', name, '--agent', 'claude', '--local')
        assert init.returncode == 0, init.stderr
        worktree = repo / '.worktrees' / f'main.{name}'
        (worktree / f'{name}.txt').write_text(f'{name} work\n', encoding='utf-8')
        _git(worktree, 'add', '-A')
        _git(worktree, 'commit', '-m', f'{name} work')
    first, second = (repo / '.worktrees' / f'main.{name}' for name in names)
    ready_a = tmp_path / 'gate_ready_a'
    release_a = tmp_path / 'gate_release_a'
    ready_b = tmp_path / 'gate_ready_b'
    bindir = _git_shim(
        tmp_path,
        'if [[ " $* " == *" merge --squash main.a "* ]]; then\n'
        f'    touch "{ready_a}"\n'
        f'    while [[ ! -e "{release_a}" ]]; do sleep 0.05; done\n'
        'elif [[ " $* " == *" merge --squash main.b "* ]]; then\n'
        f'    touch "{ready_b}"\n'
        'fi\n',
    )
    env = _cli_env()
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    procs: list[subprocess.Popen] = []
    try:
        # the first merge runs through the CLI, where the lock is, and parks
        # inside its squash with the lock held
        procs.append(
            subprocess.Popen(
                [_fractal_bin(), 'node', 'merge', f'--path={first}'],
                cwd=f'{repo}',
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,
            )
        )
        parked = _await_gate(ready_a, repo, deadline=time.monotonic() + 60)
        assert parked, 'the first squash never started'
        # the second starts against the held lock
        procs.append(
            subprocess.Popen(
                [_fractal_bin(), 'node', 'merge', f'--path={second}'],
                cwd=f'{repo}',
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,
            )
        )
        # an observation margin before the negative check: long enough for an
        # unlocked second merge to boot the CLI, run its preflight, and reach
        # its own squash
        time.sleep(5)
        assert procs[1].poll() is None, procs[1].communicate()
        assert not ready_b.exists(), 'the second squash ran while the first was parked'
        release_a.touch()
        outputs = [proc.communicate(timeout=180) for proc in procs]
    finally:
        release_a.touch()
        for proc in procs:
            _reap_group(proc)

    # both landed in turn: one merge commit each with the second's on top, the
    # work on the target, and no residue in the index, the working tree, or
    # git's squash state
    for proc, (stdout, stderr) in zip(procs, outputs):
        assert proc.returncode == 0, (stdout, stderr)
    subjects = _git(repo, 'log', '--format=%s').stdout.splitlines()
    assert subjects[:2] == ['merge main.b', 'merge main.a'], subjects
    for name in names:
        landed = (repo / f'{name}.txt').read_text(encoding='utf-8')
        assert landed == f'{name} work\n'
    assert _git(repo, 'status', '--porcelain').stdout == ''
    assert not (repo / '.git' / 'SQUASH_MSG').exists()


def test_merge_interrupt_never_leaves_a_half_merge(tmp_path: pathlib.Path) -> None:
    """A SIGINT to ``fractal node merge`` reaches the script, never kills it.

    A pid-targeted SIGINT (``timeout -s INT``, ``kill -INT``, a supervisor)
    reaches only the CLI process. Killing the script in reply would orphan
    the squash git already started, which then stages itself into the target
    with nothing left to restore it or close the merge event. The CLI
    forwards the signal and waits for the script, whose own INT handling
    decides -- it finishes the merge or restores the target through its trap
    -- so either way the target ends clean, with no squash state and no
    merge event left active.

    The squash is held open at a gate with a ``git`` shim that parks before
    running the real ``merge --squash``, so the signal lands inside that
    window.
    """
    repo = _init_tree(tmp_path / 'interruptrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    # the shim marks the squash's start and parks it at a gate, then execs
    # the real git it fronts
    ready = tmp_path / 'gate_ready'
    release = tmp_path / 'gate_release'
    bindir = _git_shim(
        tmp_path,
        'if [[ " $* " == *" merge "* && " $* " == *" --squash "* ]]; then\n'
        f'    touch "{ready}"\n'
        f'    while [[ ! -e "{release}" ]]; do sleep 0.05; done\n'
        'fi\n',
    )
    env = _cli_env()
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    proc = subprocess.Popen(
        [_fractal_bin(), 'node', 'merge', f'--path={worktree}'],
        cwd=f'{repo}',
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        parked = _await_gate(ready, repo, deadline=time.monotonic() + 60)
        assert parked, 'the squash never started'
        proc.send_signal(signal.SIGINT)
        # the signal is the CLI's alone, so the parked squash is released
        # behind it and runs on to where the script's own trap acts
        release.touch()
        stdout, stderr = proc.communicate(timeout=20)
    finally:
        release.touch()
        _reap_group(proc)

    # the CLI aborted, and the target is clean: nothing staged, no squash
    # state, and the merge event closed one way or the other
    assert proc.returncode != 0, (stdout, stderr)
    assert _git(repo, 'status', '--porcelain').stdout == ''
    assert not (repo / '.git' / 'SQUASH_MSG').exists()
    activity = _run(repo, 'node', 'activity', 'main', '--json')
    assert activity.returncode == 0, activity.stderr
    rows = json.loads(activity.stdout)
    merges = [row for row in rows if row['event'] == 'merge']
    assert merges, activity.stdout
    assert all(row['status'] != 'active' for row in merges), merges


def test_merge_interrupt_after_the_squash_finishes_the_merge(
    tmp_path: pathlib.Path,
) -> None:
    """A SIGINT after the squash commit finishes the merge instead of failing it.

    Once the squash commit is on the target the merge is complete whatever
    the advance manages, so an interrupt in the advance -- inside the reset
    that moves the child's worktree -- must neither report a failed merge nor
    leave the child half checked out or the event open. Bash runs the trap
    only once the held reset returns, so the trap repeats a reset that has
    already landed, warns of nothing, closes the event completed, reports
    the landed squash, and exits 0 with the child converged; the CLI relays
    that outcome rather than a bare interrupt.

    The advance's reset is held open at a gate with a ``git`` shim that parks
    before running the real ``reset --hard <sha>``, so the signal lands
    inside it.
    """
    repo = _init_tree(tmp_path / 'lateinterruptrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    # the shim marks the advance's reset and parks it at a gate, then execs
    # the real git it fronts (only a reset --hard onto a full sha is the
    # advance's)
    ready = tmp_path / 'gate_ready'
    release = tmp_path / 'gate_release'
    bindir = _git_shim(
        tmp_path,
        'if [[ " $* " == *" reset "* && " $* " == *" --hard "* '
        '&& "$*" =~ [0-9a-f]{40} ]]; then\n'
        f'    touch "{ready}"\n'
        f'    while [[ ! -e "{release}" ]]; do sleep 0.05; done\n'
        'fi\n',
    )
    env = _cli_env()
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    proc = subprocess.Popen(
        [_fractal_bin(), 'node', 'merge', f'--path={worktree}'],
        cwd=f'{repo}',
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        parked = _await_gate(ready, repo, deadline=time.monotonic() + 60)
        assert parked, 'the advance never started'
        proc.send_signal(signal.SIGINT)
        # the signal is the CLI's alone, so the parked reset is released
        # behind it and runs on to where the script's own trap acts
        release.touch()
        stdout, stderr = proc.communicate(timeout=30)
    finally:
        release.touch()
        _reap_group(proc)

    # the CLI reports the landed squash with exit 0; the target has the
    # commit, the child converged clean onto the merged tree with its work in
    # place -- the held reset completed and the trap's repeat of it was a
    # no-op, so no skip was warned -- and the merge event is closed
    assert proc.returncode == 0, (stdout, stderr)
    assert 'Squash-merged main.task into main' in stdout, (stdout, stderr)
    assert 'skipped advancing' not in stderr, stderr
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    assert (repo / 'f.txt').read_text(encoding='utf-8') == 'child work\n'
    assert _git(worktree, 'status', '--porcelain').stdout == ''
    subject = _git(worktree, 'log', '-1', '--format=%s').stdout.strip()
    assert subject == 'merge main (post-squash)', (subject, stderr)
    assert (worktree / 'f.txt').read_text(encoding='utf-8') == 'child work\n'
    activity = _run(repo, 'node', 'activity', 'main', '--json')
    assert activity.returncode == 0, activity.stderr
    rows = json.loads(activity.stdout)
    merges = [row for row in rows if row['event'] == 'merge']
    assert merges, activity.stdout
    assert all(row['status'] != 'active' for row in merges), merges


@pytest.mark.parametrize(
    argnames='direct',
    argvalues=[False, True],
    ids=['cli', 'script'],
)
def test_merge_footprint_refusal_quotes_a_path_with_a_space(
    tmp_path: pathlib.Path,
    direct: bool,
) -> None:
    """The footprint refusal names the escaped worktree path through either entry.

    A remedy is copy-paste material, and an unquoted path with a space splits
    into two words in the shell it is pasted into: under a repo path with a
    space the refusal's ``--path=`` carries a ``printf %q`` quoted path, and
    the unquoted path never appears. The CLI relays the script's stderr
    verbatim -- a repr'd message would double each backslash, and the pasted
    line would then name a path that does not exist -- so the operator sees
    one backslash per space there too.
    """
    repo = _init_tree(tmp_path / 'with space' / 'repo')
    init = _run(
        repo,
        'node',
        'init',
        'scoped',
        '--scope',
        'docs',
        '--agent',
        'claude',
        '--local',
    )
    assert init.returncode == 0, init.stderr
    scoped = repo / '.worktrees' / 'main.scoped'
    (scoped / 'docs').mkdir()
    (scoped / 'docs' / 'a.md').write_text('# a\n', encoding='utf-8')
    (scoped / 'outside.txt').write_text('outside the scope\n', encoding='utf-8')
    _git(scoped, 'add', '-A')
    _git(scoped, 'commit', '-m', 'work in and out of scope')
    if direct:
        merge_sh = _scripts_dir() / 'merge.sh'
        result = subprocess.run(
            ['bash', f'{merge_sh}', f'{scoped}'],
            cwd=f'{repo}',
            capture_output=True,
            text=True,
            env=_cli_env(),
        )
    else:
        result = _run(repo, 'node', 'merge', f'--path={scoped}')

    # refused naming the escaped worktree path, never the unquoted one
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert 'outside its scope' in result.stderr, result.stderr
    quoted = f'{scoped}'.replace(' ', '\\ ')
    assert f'--path={quoted}' in result.stderr, result.stderr
    assert f'--path={scoped}' not in result.stderr, result.stderr
    # the CLI relays the script's quoting as written, one backslash per space
    if not direct:
        assert 'with\\ space' in result.stderr, result.stderr
        assert 'with\\\\ space' not in result.stderr, result.stderr
        assert '\\\\' not in result.stderr, result.stderr


@pytest.mark.parametrize(
    argnames='direct',
    argvalues=[False, True],
    ids=['cli', 'script'],
)
def test_merge_into_a_root_checked_out_in_a_linked_worktree(
    tmp_path: pathlib.Path,
    direct: bool,
) -> None:
    """A root checked out in a linked worktree is still judged the user node.

    The user node's seed is self-ignored, so a linked checkout of the root
    branch (the repo root parked on a side branch, ``main`` added at
    ``../main-wt``) carries no node config for ``merge.sh`` to probe.
    ``fractal node merge`` settles the target's user-ness from the repo's
    record and passes ``--user-target``, so a copy of the node's own seed
    leaked onto the root is still named and stripped there; the merge event
    cannot land on a target no node resolves to, and the warning says so. A
    direct script call with no flag falls back to the probe, and a probe that
    cannot read the config is said -- the target is treated as a node, whose
    tracked seeds are its own business -- never read as false silently.
    """
    repo = _init_tree(tmp_path / 'linkedrootrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'settle node scaffolding')
    # main tracks a leaked copy of the node's own seed
    _git(repo, 'checkout', 'main.task', '--', '.fractal/main.task')
    _git(repo, 'commit', '-m', 'leak the live seed')
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    # the repo root parks on a side branch; main is checked out linked
    _git(repo, 'checkout', '-b', 'side', 'main')
    linked = tmp_path / 'main-wt'
    _git(repo, 'worktree', 'add', f'{linked}', 'main')
    if direct:
        merge_sh = _scripts_dir() / 'merge.sh'
        result = subprocess.run(
            ['bash', f'{merge_sh}', f'{worktree}'],
            cwd=f'{repo}',
            capture_output=True,
            text=True,
            env=_cli_env(),
        )
        # the failed probe is said, and the target is merged as a node: the
        # squash lands and the leaked copy stays tracked
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert (
            "Warning: could not read main's node config; treating it as a node target"
        ) in result.stderr, result.stderr
        assert 'leaked by an earlier merge' not in result.stderr, result.stderr
        assert 'Squash-merged main.task into main' in result.stdout, result.stdout
        tracked = _git(linked, 'ls-files', '.fractal').stdout
        assert '.fractal/main.task/config.json' in tracked
        return
    result = _run(repo, 'node', 'merge', f'--path={worktree}')

    # the CLI's verdict stands in for the probe: the leak is named and
    # stripped, the squash lands on the linked checkout, and the unrecorded
    # event is said
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'Squash-merged main.task into main' in result.stdout, result.stdout
    assert (
        'Warning: main tracks seeds of main.task or its descendants, leaked by an'
        ' earlier merge: .fractal/main.task; this merge removes them'
    ) in result.stderr, result.stderr
    assert (
        'Warning: merge event for main.task -> main was not recorded'
    ) in result.stderr, result.stderr
    assert _git(linked, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    assert (linked / 'f.txt').read_text(encoding='utf-8') == 'child work\n'
    assert _git(linked, 'ls-files', '.fractal').stdout == ''
    assert not (linked / '.fractal' / 'main.task').exists()
    assert _git(linked, 'status', '--porcelain').stdout == ''


# ------ fractal destroy: the merge lock


def test_destroy_removes_the_merge_lock_with_the_worktrees_dir(
    tmp_path: pathlib.Path,
) -> None:
    """A tree teardown takes the merge lock file down with the ``.worktrees/`` plumbing.

    ``fractal node merge`` holds its repo-wide lock as
    ``.worktrees/.merge.lock``. The last tree's destroy removes
    ``.worktrees/`` only once nothing is left in it, so a lock file left
    behind would keep the directory alive -- and surface as untracked junk
    once the exclude block goes with the tree.
    """
    repo = _init_tree(tmp_path / 'destroyrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    merge = _run(repo, 'node', 'merge', f'--path={worktree}')
    assert merge.returncode == 0, merge.stderr
    assert (repo / '.worktrees' / '.merge.lock').is_file()
    destroy = _run(repo, 'destroy', 'main', '--force')

    # the whole plumbing is gone and the tree is clean
    assert destroy.returncode == 0, (destroy.stdout, destroy.stderr)
    assert not (repo / '.worktrees').exists()
    assert not (repo / '.fractal').exists()
    assert (repo / 'f.txt').read_text(encoding='utf-8') == 'child work\n'
    assert _git(repo, 'status', '--porcelain').stdout == ''


# ------ delete.sh: unmerged-commit warning


def test_delete_warns_on_unmerged_commits(tmp_path: pathlib.Path) -> None:
    """Deleting a node with commits unmerged into its parent warns about the loss.

    ``delete.sh`` force-deletes the branch (``branch -D``) even with commits the
    parent never absorbed. The destructive teardown is by design, but it must
    surface the unmerged work on the automation path (not only the interactive
    prompt), so an operator deleting a node mid-flight knows what is discarded.
    """
    repo = _init_tree(tmp_path / 'deleterepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # a real commit on the child branch that the parent (main) does not have
    (worktree / 'feature.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child feature work')

    delete_sh = _scripts_dir() / 'delete.sh'
    result = subprocess.run(
        ['bash', f'{delete_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the delete proceeds (destructive by design) but warns about the unmerged work
    assert result.returncode == 0, result.stderr
    assert 'not merged into main' in result.stderr, (result.stdout, result.stderr)
    assert not worktree.exists()


def test_delete_does_not_warn_after_squash_merge(tmp_path: pathlib.Path) -> None:
    """A squash-merged node deletes without a false unmerged-work warning.

    ``merge.sh`` squashes (no ancestry) and strips the node's ``.fractal/`` seed,
    so a commit-count check flags a just-merged branch as unmerged. The warning
    must instead see that the branch's work already lives in the target and stay
    silent on the normal merge-then-delete path.
    """
    repo = _init_tree(tmp_path / 'squashrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # real work on the child, then squash it into main via merge.sh (strips .fractal)
    (worktree / 'feature.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child feature work')
    merge_sh = _scripts_dir() / 'merge.sh'
    merge = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert merge.returncode == 0, merge.stderr

    delete_sh = _scripts_dir() / 'delete.sh'
    result = subprocess.run(
        ['bash', f'{delete_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the work is preserved in main (squashed), so no false unmerged warning
    assert result.returncode == 0, result.stderr
    assert 'not merged' not in result.stderr, (result.stdout, result.stderr)
    assert not worktree.exists()


def test_delete_does_not_warn_after_squash_merge_then_target_advances(
    tmp_path: pathlib.Path,
) -> None:
    """A squash-merged node deletes silently even after the target moves on.

    After a child squash-merges (no ancestry), a sibling/parent keeps iterating,
    so the target advances in *other* paths. A symmetric ``diff TARGET BRANCH``
    then false-fires -- it sees the target's later, unrelated commits as work the
    branch lacks -- and cries wolf on the normal multi-child workflow. Scoping the
    check to the paths the branch itself changed keeps the warning silent: the
    target already matches the branch on ``feature.txt``, and its advance in a
    different path is not the branch's unmerged work.
    """
    repo = _init_tree(tmp_path / 'advancerepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # real work on the child, then squash it into main via merge.sh (strips .fractal)
    (worktree / 'feature.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child feature work')
    merge_sh = _scripts_dir() / 'merge.sh'
    merge = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert merge.returncode == 0, merge.stderr
    # the target moves on in an unrelated path (a later iteration / sibling merge)
    (repo / 'other.txt').write_text('later target work\n', encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'target advances elsewhere')

    delete_sh = _scripts_dir() / 'delete.sh'
    result = subprocess.run(
        ['bash', f'{delete_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the child's work is in main on its own paths, so the target's later advance
    # in another path must not resurrect a false unmerged warning
    assert result.returncode == 0, result.stderr
    assert 'not merged' not in result.stderr, (result.stdout, result.stderr)
    assert not worktree.exists()


def test_delete_warns_on_unmerged_non_ascii_work(tmp_path: pathlib.Path) -> None:
    """Unmerged work under a non-ASCII file name still draws the warning.

    With git's default ``core.quotePath``, ``--name-only`` C-quotes a
    non-ASCII name (octal escapes, surrounding double quotes included), and
    feeding that literal back as a pathspec matches nothing -- the
    preserved-content check would go quiet and swallow the warning. The check
    reads the changed paths NUL-delimited (emitted verbatim), so a child that
    squash-merged once and then kept iterating on ``café.md`` -- its *only*
    unmerged diff, with the machinery edits that init commits on the branch
    already absorbed by the merge -- warns like any other unmerged work.
    """
    repo = _init_tree(tmp_path / 'quotedrepo')
    # pin git's default path quoting so an operator-level quotePath=false can
    # never mask the case
    _git(repo, 'config', 'core.quotePath', 'true')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the first iteration lands via merge.sh, absorbing init's machinery
    # commits so the follow-up work is the branch's only unmerged diff
    (worktree / 'café.md').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child feature work')
    merge_sh = _scripts_dir() / 'merge.sh'
    merge = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert merge.returncode == 0, merge.stderr
    # the child keeps iterating on the page; the parent never absorbs it
    (worktree / 'café.md').write_text('child work\nmore child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child iterates on the page')

    delete_sh = _scripts_dir() / 'delete.sh'
    result = subprocess.run(
        ['bash', f'{delete_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the unmerged iteration is real work -- the quoting of its file name
    # must not swallow the warning
    assert result.returncode == 0, result.stderr
    assert 'not merged into main' in result.stderr, (result.stdout, result.stderr)
    assert not worktree.exists()


def test_delete_warns_on_unmerged_wiki_page_work(tmp_path: pathlib.Path) -> None:
    """Unmerged wiki *pages* still warn despite the generated-state excludes.

    The unmerged-work check excludes the wiki's generated indexes and
    ``.wiki/`` state (merge-up regenerates them on the target), but a wiki
    page is the branch's real work product. A child whose unmerged commits add
    a page -- refreshed index and cache riding along -- must still draw the
    warning: the excludes silence tool-owned bytes, never content.
    """
    repo = _init_tree(tmp_path / 'wikiwarnrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the child writes a wiki page and refreshes the generated index/cache
    (worktree / 'wiki' / 'topic.md').write_text(
        '---\nname: topic\ndesc: A topic page.\n---\n\n# topic\n\n***\n',
        encoding='utf-8',
    )
    wiki_dir = worktree / 'wiki'
    subprocess.run(
        ['wiki', 'update', f'--path={wiki_dir}'],
        capture_output=True,
        check=True,
    )
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child wiki page')

    delete_sh = _scripts_dir() / 'delete.sh'
    result = subprocess.run(
        ['bash', f'{delete_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the page is unmerged work -- the generated-state excludes must not
    # swallow the warning
    assert result.returncode == 0, result.stderr
    assert 'not merged into main' in result.stderr, (result.stdout, result.stderr)
    assert not worktree.exists()


def test_delete_does_not_warn_on_merge_regenerated_wiki_state(
    tmp_path: pathlib.Path,
) -> None:
    """Wiki state the tool regenerates never resurrects the warning.

    A mid-iteration merge (dirty child worktree) skips ``merge.sh``'s
    merge-base advance, so the branch keeps diffing from the fork point --
    including its committed ``_index.md`` and ``.wiki/`` cache. Once the
    target's wiki moves on, its regenerated bytes differ from the branch's
    copies, but those paths are tool-owned state, not the branch's work --
    the unmerged check excludes them and stays silent.
    """
    repo = _init_tree(tmp_path / 'wikiregenrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the child writes a wiki page and commits the refreshed index/cache --
    # the branch's own copies of the generated state
    (worktree / 'wiki' / 'topic.md').write_text(
        '---\nname: topic\ndesc: A topic page.\n---\n\n# topic\n\n***\n',
        encoding='utf-8',
    )
    wiki_dir = worktree / 'wiki'
    subprocess.run(
        ['wiki', 'update', f'--path={wiki_dir}'],
        capture_output=True,
        check=True,
    )
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child wiki page')
    # a dirty worktree makes the merge skip the merge-base advance (the
    # mid-iteration path), so the delete still diffs from the fork point
    (worktree / 'scratch.tmp').write_text('wip\n', encoding='utf-8')
    merge_sh = _scripts_dir() / 'merge.sh'
    merge = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )
    assert merge.returncode == 0, merge.stderr
    # the target's wiki moves on (a sibling page lands and refreshes the
    # index), so its regenerated `_index.md`/`.wiki` bytes now differ from
    # the branch's committed copies
    (repo / 'wiki' / 'other.md').write_text(
        '---\nname: other\ndesc: A sibling page.\n---\n\n# other\n\n***\n',
        encoding='utf-8',
    )
    wiki_dir = repo / 'wiki'
    subprocess.run(
        ['wiki', 'update', f'--path={wiki_dir}'],
        capture_output=True,
        check=True,
    )
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'sibling wiki page')

    delete_sh = _scripts_dir() / 'delete.sh'
    result = subprocess.run(
        ['bash', f'{delete_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the branch's page is in main; its index/.wiki bytes differ only because
    # the tool regenerated them on the target -- no false unmerged warning
    assert result.returncode == 0, result.stderr
    assert 'not merged' not in result.stderr, (result.stdout, result.stderr)
    assert not worktree.exists()


# ------ helpers


def _scripts_dir() -> pathlib.Path:
    """Bundled ``_scripts/`` directory (resolved lazily, not at import)."""
    return pathlib.Path(fractal.__file__).resolve().parent / '_scripts'


def _init_tree(root: pathlib.Path) -> pathlib.Path:
    """Build a git repo with a committed wiki and a ``fractal`` user node."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'script@test.local')
    _git(root, 'config', 'user.name', 'script')
    (root / 'tracked.txt').write_text('original\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    assert _run(root, 'init').returncode == 0
    return root


def _require_case_folding(tmp: pathlib.Path) -> None:
    """Skip the test when the filesystem under ``tmp`` keeps case distinct.

    A case-only alias is a disk hit only where the filesystem folds case, so
    the probe writes ``a`` and looks for ``A``.
    """
    (tmp / 'a').write_text('a', encoding='utf-8')
    if not (tmp / 'A').exists():
        pytest.skip('requires a case-insensitive filesystem')


def _git_shim(tmp: pathlib.Path, body: str) -> pathlib.Path:
    """A bindir holding a pass-through ``git`` that runs ``body`` first.

    ``body`` is a bash block over the call's ``$*`` -- one that fails, marks,
    or parks a chosen subcommand -- after which the shim execs the real git
    for the call. Returns the bindir to prepend to ``PATH``.
    """
    real_git = shutil.which('git')
    assert real_git is not None
    bindir = tmp / 'git_shim'
    bindir.mkdir()
    shim = bindir / 'git'
    shim.write_text(
        f'#!/usr/bin/env bash\n{body}exec "{real_git}" "$@"\n',
        encoding='utf-8',
    )
    shim.chmod(0o755)
    return bindir


def _fractal_shim_dirtying(
    tmp: pathlib.Path,
    target_file: pathlib.Path,
    *,
    on: str,
) -> pathlib.Path:
    """A bindir holding a pass-through ``fractal`` that dirties a file on a call.

    The shim execs the real console script for every call, but when the joined
    arguments contain ``on`` it first appends to ``target_file`` -- a stand-in
    for a user editing the worktree mid-operation. Returns the bindir to prepend
    to ``PATH``.
    """
    bindir = tmp / 'fractal_shim'
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / 'fractal'
    shim.write_text(
        '#!/usr/bin/env bash\n'
        f'if [[ "$*" == *"{on}"* ]]; then\n'
        f'    echo "WINDOW EDIT" >> "{target_file}"\n'
        'fi\n'
        f'exec "{_fractal_bin()}" "$@"\n',
        encoding='utf-8',
    )
    shim.chmod(0o755)
    return bindir


def _fractal_shim_holding(tmp: pathlib.Path, *, on: str) -> pathlib.Path:
    """A bindir holding a pass-through ``fractal`` that holds one call at a gate.

    The shim execs the real console script for every call, but the first time
    the joined arguments contain ``on`` it touches ``gate_ready`` beside the
    shim and parks until ``gate_release`` appears there -- a window for a
    signal to land while the script waits on that call; a later matching call
    (a trap's retry) passes straight through. Every call's arguments are
    appended to ``calls`` beside the shim, so a test can tell a call the
    signal cut short from the retry that followed. Returns the bindir to
    prepend to ``PATH``.
    """
    bindir = tmp / 'fractal_shim'
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / 'fractal'
    calls = bindir / 'calls'
    ready = bindir / 'gate_ready'
    release = bindir / 'gate_release'
    shim.write_text(
        '#!/usr/bin/env bash\n'
        f'printf \'%s\\n\' "$*" >> "{calls}"\n'
        f'if [[ "$*" == *"{on}"* && ! -e "{ready}" ]]; then\n'
        f'    touch "{ready}"\n'
        f'    while [[ ! -e "{release}" ]]; do sleep 0.05; done\n'
        'fi\n'
        f'exec "{_fractal_bin()}" "$@"\n',
        encoding='utf-8',
    )
    shim.chmod(0o755)
    return bindir


def _fractal_shim_lingering(tmp: pathlib.Path, *, on: str) -> pathlib.Path:
    """A bindir holding a pass-through ``fractal`` that lingers after one call.

    The shim runs the real console script for every call, but the first time
    the joined arguments contain ``on`` it touches ``gate_ready`` beside the
    shim once that call has returned -- its output already written -- and
    parks until ``gate_release`` appears there before exiting with the call's
    status: a window for a signal to land while the script waits on a call
    whose work is already done. Returns the bindir to prepend to ``PATH``.
    """
    bindir = tmp / 'fractal_shim'
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / 'fractal'
    ready = bindir / 'gate_ready'
    release = bindir / 'gate_release'
    shim.write_text(
        '#!/usr/bin/env bash\n'
        f'"{_fractal_bin()}" "$@"\n'
        'STATUS=$?\n'
        f'if [[ "$*" == *"{on}"* && ! -e "{ready}" ]]; then\n'
        f'    touch "{ready}"\n'
        f'    while [[ ! -e "{release}" ]]; do sleep 0.05; done\n'
        'fi\n'
        'exit "$STATUS"\n',
        encoding='utf-8',
    )
    shim.chmod(0o755)
    return bindir


def _await_gate(ready: pathlib.Path, repo: pathlib.Path, *, deadline: float) -> bool:
    """Block until a gated call parks (its ``gate_ready`` marker appears).

    Idle-based via ``_await_progress``: activity under the target repo's
    ``.git`` refreshes the allowance. Returns whether the call parked.
    """
    return _await_progress(
        check=ready.exists,
        progress=lambda: _git_activity(repo),
        deadline=deadline,
    )


def _git_activity(repo: pathlib.Path) -> list[tuple[str, int]]:
    """An mtime listing of a live merge's footprint under the target's ``.git``.

    The squash and commit churn the top-level entries (the index,
    ``SQUASH_MSG``, ``COMMIT_EDITMSG``, the refs) and the advance rewrites
    the child worktree's index under ``worktrees/``; an entry gone between
    the listing and its stat is churn too.
    """
    git_dir = repo / '.git'
    listing = []
    for path in (*git_dir.glob('*'), *git_dir.glob('worktrees/*/index')):
        try:
            listing.append((f'{path.relative_to(git_dir)}', path.stat().st_mtime_ns))
        except FileNotFoundError:
            continue
    return listing
