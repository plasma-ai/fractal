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
  byte for byte, and a child index another process holds skips the advance
  with the warning instead of failing the landed merge.
- **``merge.sh`` ``.fractal/`` restore** returns every ``.fractal`` directory
  on the target to its HEAD after the squash, minus the merging node's scope
  roots under it (a ``--meta`` node's edit to the target's seed still lands),
  then strips the node's own seed and descendants -- so a child's write into
  the target's estate or a foreign seed never lands, with a warning naming
  the dropped paths; a conflict only on such paths resolves the same way
  instead of failing the merge, and seed directories of other nodes the
  target already tracks draw a warning with the hand remedy.
- **``merge.sh`` footprint check** refuses a squash that changes paths outside
  the node's scope roots (its project wiki, ``.fractal/``, and the
  worktree-root ``.gitattributes`` excepted), naming the paths and both
  remedies, in both arms -- a fresh merge restores the target, a
  ``--continue`` leaves the staged squash -- unless ``--ignore-scope`` is
  passed; a repo-root node with no scope is unrestricted.
- **``merge.sh`` post-refresh no-op** re-checks the staged squash after the
  target's wiki index refresh: a squash the refresh fully reverts (a re-merge
  offering only regenerated wiki state, e.g. a legacy-tracked ``.wiki/cache``)
  lands on the designed "Nothing to merge" exit instead of dying on the empty
  index -- and with the cache never entering history, disjoint sibling wiki
  work merges without a conflict at all.
- **``merge.sh`` pre-refresh no-op** clears git's squash markers on the fresh
  "Nothing to merge" exit: a re-merge whose squash staged only the stripped
  seed skips the commit that would consume ``SQUASH_MSG``, and left behind it
  prefills a later bare ``git commit`` in the target with the stale squash
  message.
- **``merge.sh --continue``** finishes an operator's hand-resolved squash after
  a conflicted merge with the merge's own tail -- ``.fractal/`` restore and
  seed strip, footprint check, commit, merge-base advance -- and refuses when
  no squash is in progress. A resolution that keeps the target's content for
  everything the node offered stages nothing, and still finishes that tail
  minus the commit, so the target is left neither mid-squash nor primed to
  replay the resolved conflict. Every resolution reaches the node through the
  advance -- a third version, a restore of the fork-point content, an added
  file dropped from the squash -- so a re-merge never undoes the decision.
- **``delete.sh`` unmerged warning** surfaces commits the parent never absorbed
  on the automation path (the interactive prompt warns only the user) -- for
  non-ASCII file names too, which ``core.quotePath`` would otherwise C-quote
  into pathspecs that match nothing -- while excluding the generated wiki
  state that merge-up regenerates on the target.

Each test builds its own fresh repo (the merge/delete edges are destructive) and
shells the scripts directly with the CLI env so ``fractal`` resolves.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import signal
import subprocess

import pytest

import fractal
from tests._helpers import _git

from .conftest import _cli_env, _fractal_bin, _run

__all__ = [
    'test_init_resolves_parent_worktree_under_a_space_path',
    'test_init_allows_a_repo_under_a_worktrees_path',
    'test_init_inherits_parent_skills_on_request',
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
    'test_merge_sibling_wiki_work_lands_without_conflict',
    'test_failed_merge_restore_removes_the_staged_child_additions',
    'test_merge_advances_the_merge_base_past_a_refused_estate_file',
    'test_merge_skips_the_merge_base_advance_for_dirty_tracked_work',
    'test_merge_advance_records_the_target_tree_on_the_child',
    'test_merge_advance_keeps_the_childs_own_seed_intact',
    'test_merge_advance_brings_the_targets_profiles_to_the_child',
    'test_merge_skips_the_advance_when_the_child_index_is_locked',
    'test_merge_continue_finishes_a_hand_resolved_squash',
    'test_merge_continue_finishes_a_target_only_resolution',
    'test_merge_continue_lands_the_resolution_on_the_node',
    'test_merge_leaves_the_targets_fractal_dir_as_it_is',
    'test_merge_lands_a_meta_nodes_edit_to_the_targets_seed',
    'test_merge_resolves_a_conflict_on_the_nodes_own_seed',
    'test_merge_warns_about_leaked_seed_dirs_on_the_target',
    'test_merge_refuses_a_squash_outside_the_nodes_scope',
    'test_merge_continue_refuses_a_squash_outside_the_nodes_scope',
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


def test_merge_re_merge_of_a_merged_node_is_a_no_op(tmp_path: pathlib.Path) -> None:
    """A re-merge offering only regenerated wiki state exits 0 as a no-op.

    A tree whose baseline force-tracked the wiki tool's self-ignored
    ``.wiki/cache/`` churns that cache in every commit (it embeds per-page
    mtimes), so a re-merge of an already-merged node offers nothing but
    cache bytes the target's own index refresh regenerates: the refresh
    reverts the staged squash to ``HEAD``, and a commit attempted anyway
    would die on the empty index -- a false hard failure on the designed
    "Nothing to merge" outcome. The merge re-checks the staged squash after
    the refresh and lands the no-op exit, leaving no squash state behind.
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
    (worktree / 'wiki' / 'topic.md').write_text(
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
    assert not (repo / '.git' / 'SQUASH_MSG').exists()
    assert not (repo / '.git' / 'MERGE_MSG').exists()


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


def test_merge_skips_the_advance_when_the_child_index_is_locked(
    tmp_path: pathlib.Path,
) -> None:
    """A child index another process holds skips the advance, never the merge.

    The advance rewrites the child's branch and worktree after the squash has
    already landed on the target, so a failure there -- an ``index.lock`` held
    by another git process, which the commit content law's cleanliness check
    tolerates -- must neither fail the merge nor leave the child's ref
    half-moved: the merge warns that the advance was skipped, leaves the
    child's HEAD where it was, and still exits 0 with the target's commit in
    place.
    """
    repo = _init_tree(tmp_path / 'lockedrepo')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    (worktree / 'f.txt').write_text('child work\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'child work')
    child_head = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    # another git process holds the child's index for the whole merge
    lock = pathlib.Path(
        _git(worktree, 'rev-parse', '--git-path', 'index.lock').stdout.strip()
    )
    if not lock.is_absolute():
        lock = worktree / lock
    lock.touch()
    merge_sh = _scripts_dir() / 'merge.sh'
    result = subprocess.run(
        ['bash', f'{merge_sh}', f'{worktree}'],
        cwd=f'{repo}',
        capture_output=True,
        text=True,
        env=_cli_env(),
    )

    # the merge landed and warned; the child's ref never moved
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'skipped advancing' in result.stderr, (result.stdout, result.stderr)
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    assert (repo / 'f.txt').read_text(encoding='utf-8') == 'child work\n'
    assert _git(worktree, 'rev-parse', 'HEAD').stdout.strip() == child_head


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
    # stripped from the commit and the working tree both
    assert result.returncode == 0, (result.stdout, result.stderr)
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
        pytest.param('resolved line\n', True, id='third-version'),
        pytest.param('original\n', True, id='base-content'),
        pytest.param('resolved line\n', False, id='dropped-add'),
    ],
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
    the target's memory is untouched -- with a warning naming what was
    dropped, so a deliberate change can still be landed by hand.
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
    memory_after = _git(
        parent, 'rev-parse', 'HEAD:.fractal/main.parent/memory'
    ).stdout.strip()
    assert memory_after == memory_before
    assert _git(parent, 'status', '--porcelain').stdout == ''
    # the warning names every dropped path
    assert 'changed paths under .fractal/' in result.stderr, result.stderr
    for path in (
        '.fractal/main.other/NODE.md',
        '.fractal/main.parent/scripts/test.sh',
        '.fractal/main.parent/plans/.gitkeep',
        '.fractal/main.parent/memory/note.md',
    ):
        assert path in result.stderr, (path, result.stderr)


def test_merge_lands_a_meta_nodes_edit_to_the_targets_seed(
    tmp_path: pathlib.Path,
) -> None:
    """A ``--meta`` node's edit to the target's own seed dir still lands.

    A meta node's scope is the target's ``.fractal/<branch>/`` -- its whole
    work product is the target's contract and machinery -- so that one upward
    flow under ``.fractal/`` is work, not machinery riding along: the restore
    that returns the rest of the target's ``.fractal/`` to HEAD leaves the
    meta node's scope root alone, and the edit lands without a warning.
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

    # the edit is on the target, the meta node's own seed is not, and nothing
    # was dropped
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'changed paths under .fractal/' not in result.stderr, result.stderr
    landed = (parent / '.fractal' / 'main.parent' / 'NODE.md').read_text(
        encoding='utf-8'
    )
    assert landed.endswith('Tuned by the meta node.\n')
    assert not (parent / '.fractal' / 'main.fix').exists()
    assert _git(parent, 'status', '--porcelain').stdout == ''


def test_merge_resolves_a_conflict_on_the_nodes_own_seed(
    tmp_path: pathlib.Path,
) -> None:
    """A squash conflicting only on the node's own seed resolves itself.

    A copy of the node's seed that once leaked onto the target and was purged
    there leaves the merge-base carrying it: the node's live edits to those
    files then hit modify/delete conflicts on paths the merge would strip
    anyway, before any strip could run. When every unmerged path is under
    ``.fractal/`` and outside the node's scope roots, the merge resolves them
    as the restore and strip would and lands; the live seed survives the
    advance.
    """
    repo = _init_tree(tmp_path / 'purgedrepo')
    # a leaked copy of the node's memory on the target before the fork
    leaked = repo / '.fractal' / 'main.task' / 'memory' / 'x.md'
    leaked.parent.mkdir(parents=True)
    leaked.write_text('leaked v1\n', encoding='utf-8')
    _git(repo, 'add', '.fractal/main.task')
    _git(repo, 'commit', '-m', 'leaked seed copy')
    init = _run(repo, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    # the target purges the leak while the node keeps working on its live copy
    _git(repo, 'rm', '-r', '--quiet', '.fractal/main.task')
    _git(repo, 'commit', '-m', 'purge leaked seed')
    (worktree / '.fractal' / 'main.task' / 'memory' / 'x.md').write_text(
        'live v2\n',
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

    # the merge landed clean: the work is on the target, the seed is not, and
    # the node's live copy is intact
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'resolved 1 conflicting path(s) under .fractal/' in result.stderr, (
        result.stderr
    )
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    assert (repo / 'f.txt').read_text(encoding='utf-8') == 'child work\n'
    assert not (repo / '.fractal' / 'main.task').exists()
    assert _git(repo, 'status', '--porcelain').stdout == ''
    live = (worktree / '.fractal' / 'main.task' / 'memory' / 'x.md').read_text(
        encoding='utf-8'
    )
    assert live == 'live v2\n'


def test_merge_warns_about_leaked_seed_dirs_on_the_target(
    tmp_path: pathlib.Path,
) -> None:
    """A foreign node's seed tracked on the target draws a warning, not a fix.

    A seed a hand merge leaked onto the target collides with that node's live
    seed on its every later merge of the target. Only a node's own squash
    removes its own copy, so the merge names the seed directories the target
    tracks that belong to other nodes -- with the ``git rm -r --cached``
    remedy -- and still lands, leaving them for the operator.
    """
    repo = _init_tree(tmp_path / 'leakedrepo')
    leaked = repo / '.fractal' / 'main.other' / 'NODE.md'
    leaked.parent.mkdir(parents=True)
    leaked.write_text('# leaked contract\n', encoding='utf-8')
    _git(repo, 'add', '.fractal/main.other')
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

    # the merge landed and warned, naming the leaked dir and the hand remedy
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'tracks node seed directories leaked by an earlier merge' in result.stderr
    assert '.fractal/main.other' in result.stderr, result.stderr
    assert 'rm -r --cached' in result.stderr, result.stderr
    assert _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
    # the leak is the operator's to remove: the merge leaves it tracked
    assert '.fractal/main.other/NODE.md' in _git(repo, 'ls-files', '.fractal').stdout


# ------ merge.sh: the squash footprint


@pytest.mark.parametrize(
    argnames=('scope', 'flags', 'lands'),
    argvalues=[
        pytest.param(['--scope', 'docs'], [], False, id='scoped-refused'),
        pytest.param(
            ['--scope', 'docs'], ['--ignore-scope'], True, id='scoped-overridden'
        ),
        pytest.param([], [], True, id='unscoped'),
    ],
)
def test_merge_refuses_a_squash_outside_the_nodes_scope(
    tmp_path: pathlib.Path,
    scope: list[str],
    flags: list[str],
    lands: bool,
) -> None:
    """A squash changing paths outside the node's scope is refused unless overridden.

    Commit-time scope is bypassable -- ``fractal commit --ignore-scope``, the
    ``--force`` backstop, a raw ``git commit`` -- so the squash is the one
    point that sees the node's whole offering. A path outside every scope
    root (the node's project wiki, ``.fractal/``, and the worktree-root
    ``.gitattributes`` excepted) refuses the merge, naming the paths and both
    remedies -- widening the scope or ``--ignore-scope`` -- and a fresh merge
    restores the target. ``--ignore-scope`` lands the offering as it is, and
    a repo-root node with no scope is unrestricted.
    """
    repo = _init_tree(tmp_path / 'scoperepo')
    init = _run(repo, 'node', 'init', 'task', *scope, '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    worktree = repo / '.worktrees' / 'main.task'
    (worktree / 'docs').mkdir()
    (worktree / 'docs' / 'a.md').write_text('# a\n', encoding='utf-8')
    (worktree / 'outside.txt').write_text('outside the scope\n', encoding='utf-8')
    # raw git: `fractal commit` would refuse the out-of-scope path itself
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
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert (
            _git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'merge main.task'
        )
        assert (repo / 'docs' / 'a.md').is_file()
        assert (repo / 'outside.txt').read_text(
            encoding='utf-8'
        ) == 'outside the scope\n'
    else:
        # refused, naming the out-of-scope path (not the in-scope one) and
        # both remedies
        assert result.returncode != 0, (result.stdout, result.stderr)
        assert 'outside its scope' in result.stderr, result.stderr
        assert 'outside.txt' in result.stderr, result.stderr
        assert 'docs/a.md' not in result.stderr, result.stderr
        assert 'config set scope=' in result.stderr, result.stderr
        assert '--ignore-scope' in result.stderr, result.stderr
        # the target is restored: HEAD unmoved, nothing staged or left on disk
        assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == main_head
        assert _git(repo, 'status', '--porcelain').stdout == ''
        assert not (repo / 'outside.txt').exists()
        assert not (repo / 'docs').exists()


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
