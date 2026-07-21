"""Implements ``Files`` class."""

from __future__ import annotations

import datetime as dt
import io
import os
import pathlib
import typing
import zipfile
from typing import Any, Optional

import fractal.util
from fractal.constants import FRACTAL_FOLDER, WORKTREES_FOLDER

if typing.TYPE_CHECKING:
    from .db import Database
    from .node import Node

__all__ = []


class Files:
    """External work-product surface (list/read/write/commit/archive)."""

    def __init__(self: Files, node: Node) -> None:
        """Initialize ``Files``.

        Args:
            node: The owning ``Node`` instance.

        """
        self._node = node

    @property
    def node(self: Files) -> Node:
        """Return the owning node."""
        return self._node

    @property
    def db(self: Files) -> Database:
        """Return the central database."""
        return self._node.db

    @property
    def worktree(self: Files) -> pathlib.Path:
        """Return the node's resolved worktree path."""
        return self._node.worktree

    def list(
        self: Files,
        *,
        path: Optional[str] = None,
        since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """List the node's project files (git-tracked, machinery included).

        The work-product surface: every git-tracked file in the worktree --
        the git-ignored runtime (``.db``/``.status``/logs) never appears in a
        tracked listing, and ``wiki/``/``.fractal/`` entries list like any
        other content (consumers filter or collapse them). With ``since`` the
        set is instead the node's own contribution: files its own commits
        touched (a first-parent walk from the ``since`` anchor -- a tree at
        ``HEAD`` contains everything ever merged in, so without the walk content
        synced from the parent, and through it siblings, would read as this
        node's output) carrying the net line counts of a ``<anchor>...HEAD``
        diff.

        Args:
            path: Restrict to a worktree-relative subtree; all files if
                ``None``.
            since: List changed files instead, from ``base`` (the node's fork
                point), ``commit`` (the previous commit), ``iteration``, or
                ``run``; the full tracked listing if ``None``.

        Returns:
            ``[{path, size}]`` sorted by path (``path`` worktree-relative). A
            changed listing's entries also carry ``change`` (``added``/
            ``modified``/``deleted``) and ``additions``/``deletions`` line
            counts (``None`` for a binary file); a deleted file is kept
            (``size`` ``0``) so its removal can render, and a touched file
            with no net change (self-corrections cancel) drops out.

        """
        # candidate paths: the full tracked set, or this node's changes with
        # line stats; -z everywhere so a non-ASCII path is never C-quoted
        changes: dict[str, dict[str, Any]] = {}
        if since is not None:
            anchor = self._diff_anchor(since)
            if not anchor:
                return []
            # pin HEAD to one sha so the two diff reads cannot straddle a
            # loop commit landing mid-poll
            head = fractal.util.git.run(['rev-parse', 'HEAD'], cwd=self.worktree)
            # line stats per changed path ('-' marks a binary file)
            cmd = ['diff', '--numstat', '--no-renames', '-z', f'{anchor}...{head}']
            raw = fractal.util.git.run_bytes(cmd, cwd=self.worktree) or b''
            out = os.fsdecode(raw)
            for entry in filter(None, out.split('\0')):
                added, _, rest = entry.partition('\t')
                deleted, _, rel = rest.partition('\t')
                if not rel:
                    continue
                changes[rel] = {
                    'additions': int(added) if added.isdigit() else None,
                    'deletions': int(deleted) if deleted.isdigit() else None,
                }
            # change kind per path (T -- a type change -- reads as modified)
            cmd = ['diff', '--name-status', '--no-renames', '-z', f'{anchor}...{head}']
            raw = fractal.util.git.run_bytes(cmd, cwd=self.worktree) or b''
            out = os.fsdecode(raw)
            fields = [field for field in out.split('\0') if field]
            kinds = {'A': 'added', 'M': 'modified', 'D': 'deleted'}
            for status, rel in zip(fields[::2], fields[1::2]):
                changes.setdefault(rel, {'additions': None, 'deletions': None})
                changes[rel]['change'] = kinds.get(status[:1], 'modified')
            # membership: only files this node's own commits touched --
            # --first-parent keeps merged-in side histories out of the range
            # and --no-merges drops the merge commits themselves, while a
            # squash-merged child (an ordinary commit on this line) rightly
            # stays; counts remain the net diff above, so a merged-in file
            # never lists and a touched one shows what the diff view renders
            cmd = [
                'log',
                '--first-parent',
                '--no-merges',
                '--name-only',
                '--no-renames',
                '--format=',
                '-z',
                f'{anchor}..{head}',
            ]
            raw = fractal.util.git.run_bytes(cmd, cwd=self.worktree) or b''
            touched = {rel for rel in os.fsdecode(raw).split('\0') if rel}
            candidates = [rel for rel in changes if rel in touched]
        else:
            cmd = ['ls-files', '-z']
            raw = fractal.util.git.run_bytes(cmd, cwd=self.worktree) or b''
            out = os.fsdecode(raw)
            candidates = [rel for rel in out.split('\0') if rel]
        # drop structurally unsafe and out-of-scope entries, then stat what's
        # on disk; comparisons casefold -- APFS matches case-insensitively
        if path:
            subtree = path.rstrip('/')
            scope = f'{subtree}/'
        else:
            scope = ''
        files = []
        for rel in candidates:
            # skip only what _validate_relpath structurally refuses (so the
            # listing never names an entry read()/path() would reject): a
            # .git component, a leading .worktrees (sibling node worktrees on
            # the user node), and leading pathspec magic -- wiki/ and
            # .fractal/ list like any other tracked content
            parts = rel.casefold().split('/')
            if '.git' in parts or parts[0] == WORKTREES_FOLDER:
                continue
            if rel.startswith(':'):
                continue
            if scope and not rel.startswith(scope):
                continue
            entry: dict[str, Any] = {'path': rel, 'size': 0}
            if since is not None:
                entry.update(changes[rel])
                entry.setdefault('change', 'modified')
            # a deleted entry has nothing on disk -- keep it un-stat'ed so its
            # removal can render; everything else stats once, racing the live
            # worktree (a file vanishing mid-poll is skipped, not an error)
            if entry.get('change') != 'deleted':
                abs_path = self.worktree / rel
                try:
                    if not abs_path.is_file():
                        continue
                    # a symlink serves its target only while the target stays
                    # inside the worktree -- worktree content is agent-authored,
                    # so an escaping link is dropped at the serving boundary
                    if abs_path.is_symlink():
                        if not abs_path.resolve().is_relative_to(self.worktree):
                            continue
                    entry['size'] = abs_path.stat().st_size
                except OSError:
                    continue
            files.append(entry)
        files.sort(key=lambda entry: entry['path'])
        return files

    def read(
        self: Files,
        path: str,
        *,
        max_lines: Optional[int] = None,
        since: Optional[str] = None,
        before: bool = False,
        at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Read a project file's content (validated, capped).

        Only files the project surface exposes are readable: the path must be
        structurally safe (:meth:`_validate_relpath`) and either git-tracked
        or, given ``since``, part of that anchor's changed set -- so a deleted
        file's old content stays readable without exposing anything else.
        ``before`` reads the file as it was at the ``since`` anchor (via
        ``git show``), for the old side of a before/after view; ``at`` reads
        it as one of the node's own commits left it (a sha from
        :meth:`history`), overriding ``since``/``before``. A side that does
        not exist (an added file has no before; a deleted file has no after)
        returns ``exists=False`` with empty content.

        Args:
            path: Worktree-relative file path.
            max_lines: Cap the returned text to this many lines (full if
                ``None``); the cap preserves line terminators, so the included
                portion matches the raw bytes.
            since: Diff scope -- ``base``, ``commit``, ``iteration``, or
                ``run`` (see :meth:`list`).
            before: Read the file at the ``since`` anchor instead of the
                worktree.
            at: Full commit sha to read the file at (:meth:`history` refs);
                ``since``/``before`` are ignored when given.

        Returns:
            ``{path, content, truncated, total_lines, size, binary, exists}``.
            A non-UTF-8 file returns ``binary=True`` with empty ``content``
            (callers download it via :meth:`path` instead).

        Raises:
            ValueError: If ``before`` is set without ``since``, ``at`` is not
                a full sha reachable from the node's HEAD, or ``path`` is not
                a file the tracked set or the anchor's changed set exposes.

        """
        norm = self._validate_relpath(path)
        if at is not None:
            return self._render(norm, self._at_bytes(norm, at), max_lines)
        if before and since is None:
            raise ValueError('Please specify since when reading the before side.')
        # literal pathspec magic: a glob char in a tracked name (e.g. a
        # bracketed route dir) must not widen or empty the match
        spec = f':(literal){norm}'
        # membership: the tracked set, else (with an anchor) the changed set
        # -- O(1) probes, not a full listing, so a poller never pays O(repo)
        cmd = ['ls-files', '--error-unmatch', '--', spec]
        tracked = fractal.util.git.run(cmd, cwd=self.worktree, check=False)
        anchor = self._diff_anchor(since) if since is not None else None
        if not tracked:
            in_changed = False
            if anchor:
                cmd = ['diff', '--name-only', '--no-renames', '-z']
                cmd += [f'{anchor}...HEAD', '--', spec]
                in_changed = bool(
                    fractal.util.git.run(cmd, cwd=self.worktree, check=False)
                )
            if not in_changed:
                raise self._not_readable(path)
        # fetch the requested side's raw bytes (None when the side is absent)
        if before:
            # a tracked file with no resolved anchor has no before side --
            # never interpolate the missing anchor into the ref (a branch
            # literally named 'None' must not answer)
            raw = None
            if anchor:
                # a rev:path lookup is literal already -- no :(literal) here
                raw = fractal.util.git.run_bytes(
                    ['show', f'{anchor}:{norm}'],
                    cwd=self.worktree,
                )
        else:
            abs_path = self.worktree / norm
            raw = None
            # containment at the serving boundary: a tracked symlink escaping
            # the worktree must not be readable through it
            try:
                contained = abs_path.resolve().is_relative_to(self.worktree)
                if abs_path.is_file() and contained:
                    raw = abs_path.read_bytes()
            except OSError:
                raw = None
        return self._render(norm, raw, max_lines)

    def _render(
        self: Files,
        norm: str,
        raw: Optional[bytes],
        max_lines: Optional[int],
    ) -> dict[str, Any]:
        """Shape one side's raw bytes into :meth:`read`'s response dict."""
        if raw is None:
            # the file does not exist on this side (a pure add or delete)
            return {
                'path': norm,
                'content': '',
                'truncated': False,
                'total_lines': 0,
                'size': 0,
                'binary': False,
                'exists': False,
            }
        # binary content has nothing to render -- flag it for download
        size = len(raw)
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            return {
                'path': norm,
                'content': '',
                'truncated': False,
                'total_lines': 0,
                'size': size,
                'binary': True,
                'exists': True,
            }
        # cap on whole lines, keeping terminators so the included portion
        # round-trips byte-identical against the raw file
        lines = text.splitlines(keepends=True)
        total_lines = len(lines)
        truncated = max_lines is not None and total_lines > max_lines
        if truncated:
            text = ''.join(lines[:max_lines])
        return {
            'path': norm,
            'content': text,
            'truncated': truncated,
            'total_lines': total_lines,
            'size': size,
            'binary': False,
            'exists': True,
        }

    def _at_bytes(self: Files, norm: str, at: str) -> Optional[bytes]:
        """Fetch a file's raw bytes at one commit of the node's history.

        ``at`` must be a full commit sha -- never a rev expression, which
        could name arbitrary refs -- reachable from the node's HEAD (the
        shas :meth:`history` returns), so a sibling's unmerged commits
        never answer. Returns ``None`` when the file is absent at that
        commit.

        Raises:
            ValueError: If ``at`` is not a full sha reachable from HEAD.

        """
        sha = at.lower()
        if len(sha) != 40 or not set(sha) <= set('0123456789abcdef'):
            raise ValueError(f'Invalid at: {at!r} (expected a full commit sha).')
        cmd = ['merge-base', '--is-ancestor', sha, 'HEAD']
        reachable = fractal.util.git.run(cmd, cwd=self.worktree, check=False)
        if reachable is None:
            raise ValueError(f'Unknown commit: {at!r} (not reachable from HEAD).')
        # a rev:path lookup is literal already -- no :(literal) here
        return fractal.util.git.run_bytes(
            ['show', f'{sha}:{norm}'],
            cwd=self.worktree,
        )

    def path(self: Files, path: str) -> pathlib.Path:
        """Resolve a project file to its on-disk path (validated).

        The download side of :meth:`read` -- same validation, tracked
        membership, and containment -- returning the absolute path so a
        caller streams the bytes straight from disk (e.g. an HTTP layer
        serving range requests) instead of buffering them through a read.

        Args:
            path: Worktree-relative file path.

        Returns:
            The absolute on-disk path of the file.

        Raises:
            ValueError: If ``path`` is not a readable project file.

        """
        norm = self._validate_relpath(path)
        # literal pathspec magic, as for a read
        cmd = ['ls-files', '--error-unmatch', '--', f':(literal){norm}']
        tracked = fractal.util.git.run(cmd, cwd=self.worktree, check=False)
        abs_path = self.worktree / norm
        # containment at the serving boundary, as for a read: an escaping
        # symlink is not servable
        contained = abs_path.resolve().is_relative_to(self.worktree)
        if not tracked or not abs_path.is_file() or not contained:
            raise self._not_readable(path)
        return abs_path

    def write(self: Files, path: str, data: bytes) -> dict[str, Any]:
        """Write a file into the worktree at ``path`` (validated, uncommitted).

        The upload side of the project surface: raw bytes land at a validated
        worktree-relative path (parents created), joining the tracked listing
        only once committed -- via :meth:`commit`, promptly: on a scoped
        node an uncommitted out-of-scope upload fails the loop's next commit
        scope check, which diffs untracked files too. Uploads validate at the
        write tier (:meth:`_validate_writable`): ``.fractal`` paths refuse,
        the wiki accepts.

        Args:
            path: Worktree-relative destination path.
            data: Raw bytes to write.

        Returns:
            ``{path, size}`` -- the normalized path and bytes written.

        Raises:
            RuntimeError: If the node is paused.
            ValueError: If ``path`` escapes the worktree or names machinery.

        """
        # refuse over frozen work -- paused admits only resume/kill/chat
        if self._node.status() == 'paused':
            raise RuntimeError(
                'Cannot write to a paused node. Resume or kill it first.'
            )
        norm = self._validate_writable(path)
        # atomic write, so a concurrent download never streams a half-written upload
        abs_path = self.worktree / norm
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        fractal.util.filesystem.write_atomic(abs_path, data)
        return {'path': norm, 'size': len(data)}

    def commit(self: Files, paths: list[str], message: str) -> dict[str, Any]:
        """Stage and commit specific worktree paths (no lint, scope, or push).

        A narrow pathspec commit for bringing user files (e.g. uploaded
        inputs) into the tree -- allowed on the user node, unlike the locked
        ``--init`` baseline, and without the loop's full commit machinery.
        Each path is validated like a write, then staged and committed with a
        pathspec, so nothing else the worktree has staged is swept in. No
        ``commit`` event is logged: an upload has no run lineage (the same
        reason the commit script skips the event for ``--init``), and
        auto-resolved lineage during a live run would silently shift the
        ``iteration``/``run`` diff anchors.

        Args:
            paths: Worktree-relative paths to stage and commit.
            message: Short description appended to the commit message.

        Returns:
            ``{committed, sha, paths}`` -- whether a commit was made
            (``False`` with a ``None`` sha when the paths held no change) and
            the normalized paths.

        Raises:
            RuntimeError: If the node is paused.
            ValueError: If ``paths`` is empty, ``message`` is blank, or a path
                escapes the worktree or names machinery.

        """
        # refuse over frozen work -- paused admits only resume/kill/chat
        if self._node.status() == 'paused':
            raise RuntimeError(
                'Cannot commit files on a paused node. Resume or kill it first.'
            )
        if not paths:
            raise ValueError('Please pass at least one path.')
        if not message:
            raise ValueError('Please pass a commit message.')
        norm = [self._validate_writable(entry) for entry in paths]
        # literal pathspec magic: a glob char in an uploaded name must not
        # widen or empty the match
        specs = [f':(literal){entry}' for entry in norm]
        # stage just these paths (pathspec), so other staged work is untouched
        fractal.util.git.run(['add', '--', *specs], cwd=self.worktree)
        # benign no-op when the paths hold nothing new to commit
        cmd = ['diff', '--cached', '--name-only', '-z', '--', *specs]
        if not fractal.util.git.run(cmd, cwd=self.worktree):
            return {'committed': False, 'sha': None, 'paths': norm}
        # commit only these paths (pathspec); --no-verify because bypassing
        # the save path must bypass repo hooks too -- a hook must not rewrite
        # or reject uploaded bytes (the loop's own force path does the same);
        # no push -- the caller owns the branch
        msg = f'{self._node.branch}: files ({message})'
        cmd = ['commit', '--no-verify', '-m', msg, '--', *specs]
        fractal.util.git.run(cmd, cwd=self.worktree)
        sha = fractal.util.git.run(['rev-parse', 'HEAD'], cwd=self.worktree)
        return {'committed': True, 'sha': sha, 'paths': norm}

    def archive(
        self: Files,
        *,
        path: Optional[str] = None,
        since: Optional[str] = None,
    ) -> bytes:
        """Bundle the node's project files into a zip archive.

        Read-only: zips the :meth:`list` set (full or changed); the
        worktree is never modified. Arcnames are the listing's validated
        worktree-relative paths, and a changed listing's deletions (nothing on
        disk) are skipped.

        Args:
            path: Restrict to a worktree-relative subtree; all files if
                ``None``.
            since: Archive the changed set instead (see :meth:`list`).

        Returns:
            The zip archive bytes.

        """
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for entry in self.list(path=path, since=since):
                abs_path = self.worktree / entry['path']
                # skip a changed listing's deletions, and any file vanishing
                # mid-poll under a live loop -- nothing to zip
                try:
                    if abs_path.is_file():
                        archive.write(abs_path, arcname=entry['path'])
                except OSError:
                    continue
        return buffer.getvalue()

    def history(
        self: Files,
        path: str,
        *,
        since: str = 'base',
    ) -> list[dict[str, Any]]:
        """List the node's own commits that touched one file, newest first.

        The per-file trail behind a changed listing: the same first-parent,
        no-merges walk as :meth:`list`'s membership, scoped to one path -- so
        history shows exactly the commits that made the file a member, never
        a merge or merged-in side history. Each entry carries the commit's
        own line counts for the file (unlike the listing's net counts), so
        the trail sums the work over time.

        Args:
            path: Worktree-relative file path.
            since: History scope -- ``base`` (default), ``commit``,
                ``iteration``, or ``run`` (see :meth:`list`).

        Returns:
            ``[{sha, instant, subject, additions, deletions}]`` newest first
            -- ``instant`` is the commit's author time (ISO 8601), the counts
            per-commit numstat (``None`` for binary); empty when the scope
            has no anchor or no own commit touched the file.

        """
        norm = self._validate_relpath(path)
        anchor = self._diff_anchor(since)
        if not anchor:
            return []
        # own commits touching the path, newest-first; the leading 0x01
        # separates records so a subject is never mistaken for a numstat line
        cmd = [
            'log',
            '--first-parent',
            '--no-merges',
            '--no-renames',
            '--numstat',
            '--format=%x01%H%x09%at%x09%s',
            f'{anchor}..HEAD',
            '--',
            f':(literal){norm}',
        ]
        raw = fractal.util.git.run_bytes(cmd, cwd=self.worktree) or b''
        out = os.fsdecode(raw)
        entries = []
        for record in filter(None, out.split('\x01')):
            header, _, stats = record.partition('\n')
            sha, _, rest = header.partition('\t')
            epoch, _, subject = rest.partition('\t')
            if not sha or not epoch.isdigit():
                continue
            # the record's one numstat line ('-' marks a binary file)
            additions = None
            deletions = None
            for line in stats.splitlines():
                added, _, remainder = line.partition('\t')
                deleted, _, rel = remainder.partition('\t')
                if rel:
                    additions = int(added) if added.isdigit() else None
                    deletions = int(deleted) if deleted.isdigit() else None
                    break
            entry = {
                'sha': sha,
                'instant': _instant(int(epoch)),
                'subject': subject,
                'additions': additions,
                'deletions': deletions,
            }
            entries.append(entry)
        return entries

    def _diff_anchor(self: Files, since: str) -> Optional[str]:
        """Resolve the ref a changed-files listing diffs ``<ref>...HEAD`` against.

        ``since`` picks the scope of "this node's changes": ``base`` (the
        whole contribution since the node's fork point), ``commit`` (the
        previous commit), ``iteration``/``run`` (the most recent iteration or
        run that committed). Anchors resolve from the node's own record, not
        branch refs: init records the fork sha and the commit pipeline logs a
        ``commit`` event per save (``metadata`` is the sha, tagged with
        run/iter lineage), so every anchor is a fixed point in the node's own
        history that survives the node being merged into its parent -- a
        parent-branch anchor would collapse to empty once the parent absorbs
        the commits. The ``iteration``/``run`` scopes walk the node's own
        commits (:meth:`_commit_lineage`), so a raw agent commit the pipeline
        never evented still lands on the run that made it. Every query is
        node-scoped (the DB is tree-central: an unscoped read would anchor on
        a sibling's commit) and floored at the newest ``init`` event, so a
        re-init of a deleted branch name never reads a dead incarnation's
        events.

        Args:
            since: Diff scope -- ``base``, ``commit``, ``iteration``, or
                ``run``.

        Returns:
            A git ref, or ``None`` when the scope has no anchor (``commit`` on
            a root commit; ``iteration``/``run`` when nothing attributable
            was committed).

        """
        if since not in ('base', 'commit', 'iteration', 'run'):
            raise ValueError(f'Invalid since: {since!r}')
        if since == 'commit':
            # the previous commit, when HEAD has a parent
            cmd = ['rev-parse', '--verify', '--quiet', 'HEAD~1']
            return fractal.util.git.run(cmd, cwd=self.worktree, check=False) or None
        branch = self._node.branch
        # the current incarnation's floor: history rows persist across
        # delete and reset, so a re-inited branch name must not anchor
        # on a dead namesake's events
        floor = (
            'SELECT COALESCE(MAX(event_id), 0) FROM events'
            " WHERE event = 'init' AND node = ?"
        )
        if since == 'base':
            # the fork sha stamped on the newest init event; a legacy tree
            # (no stamp) anchors just before its first commit event instead
            query = (
                "SELECT metadata FROM events WHERE event = 'init' AND node = ?"
                ' ORDER BY event_id DESC LIMIT 1'
            )
            rows = self.db.read(query=query, params=(branch,))
            if rows and rows[0]['metadata']:
                return rows[0]['metadata']
            query = (
                "SELECT metadata FROM events WHERE event = 'commit' AND node = ?"
                f' AND event_id > ({floor})'
                ' ORDER BY event_id ASC LIMIT 1'
            )
            rows = self.db.read(query=query, params=(branch, branch))
            first = rows[0]['metadata'] if rows and rows[0]['metadata'] else None
            if first:
                return f'{first}^'
            # never committed: the configured base (else the dotted parent),
            # pinned to the merge-base sha so the changed set, the membership
            # probe, and a before-side read all key the same fixed point
            base = self._node.config.get('base') or ''
            if not base and '.' in branch:
                base, *_ = branch.rsplit('.', 1)
            if not base:
                return None
            cmd = ['merge-base', base, 'HEAD']
            return fractal.util.git.run(cmd, cwd=self.worktree, check=False) or None
        # iteration/run: walk the node's own commits for the most recent
        # scope that committed -- events resolve a commit's scope where the
        # pipeline logged one, and a time-window match covers the rest
        column = 'iter_id' if since == 'iteration' else 'run_id'
        return self._commit_lineage(column)

    def _commit_lineage(self: Files, column: str) -> Optional[str]:
        """Anchor the newest committed run/iteration by walking own commits.

        Resolves each of the node's own commits (first-parent, no merges,
        bounded at the incarnation's ``base`` anchor) to a run or iteration:
        through its ``commit`` event when the pipeline logged one, else by
        matching the commit's author time against the scope rows'
        ``started_at``/``ended_at`` windows -- author time survives rebases
        and amends but is fresh on a squash, so adopted work lands on the
        adopting run. The newest resolvable commit names the scope, and the
        anchor sits just before that scope's oldest commit -- a fixed point a
        later merge into the parent cannot move. A commit matching no scope
        (an upload, a between-runs save) is skipped, not misattributed.

        Args:
            column: Scope id column -- ``run_id`` or ``iter_id``.

        Returns:
            A git ref, or ``None`` when no own commit resolves to a scope.

        """
        # bound the walk at the incarnation's fork: everything before it is
        # a parent's (or dead namesake's) history
        base = self._diff_anchor('base')
        if not base:
            return None
        # own commits newest-first, with author epochs
        cmd = ['log', '--first-parent', '--no-merges', '--format=%H %at']
        cmd += [f'{base}..HEAD']
        out = fractal.util.git.run(cmd, cwd=self.worktree, check=False) or ''
        commits = []
        for line in out.splitlines():
            sha, _, epoch = line.partition(' ')
            if sha and epoch.isdigit():
                commits.append((sha, int(epoch)))
        if not commits:
            return None
        branch = self._node.branch
        floor = (
            'SELECT COALESCE(MAX(event_id), 0) FROM events'
            " WHERE event = 'init' AND node = ?"
        )
        # evented commits carry their scope id directly
        query = (
            f'SELECT metadata, {column} FROM events'
            f" WHERE event = 'commit' AND node = ? AND event_id > ({floor})"
        )
        rows = self.db.read(query=query, params=(branch, branch))
        evented = {
            row['metadata']: row[column]
            for row in rows
            if row['metadata'] and row[column] is not None
        }
        # scope windows for the eventless, newest scope first -- author
        # epochs are whole seconds, so starts truncate to match (a commit in
        # a scope's opening second must not fall before it) and a same-second
        # overlap resolves to the newer scope (an open scope is unbounded)
        table = 'iters' if column == 'iter_id' else 'runs'
        query = (
            f'SELECT {column}, started_at, ended_at FROM {table}'
            f' WHERE node = ? ORDER BY {column} DESC'
        )
        windows = []
        for row in self.db.read(query=query, params=(branch,)):
            started = int(_epoch(row['started_at']))
            ended = _epoch(row['ended_at']) if row['ended_at'] else None
            windows.append((row[column], started, ended))
        # resolve newest-first: the first resolvable commit names the scope,
        # and the scope's oldest commit sets the anchor
        scope = None
        oldest = None
        for sha, epoch in commits:
            resolved = evented.get(sha)
            if resolved is None:
                for scope_id, started, ended in windows:
                    if started <= epoch and (ended is None or epoch <= ended):
                        resolved = scope_id
                        break
            if resolved is None:
                continue
            if scope is None:
                scope = resolved
            if resolved == scope:
                oldest = sha
        if oldest is None:
            return None
        return f'{oldest}^'

    def _validate_relpath(self: Files, path: str) -> str:
        """Validate a worktree-relative project path (the structural tier).

        The safety boundary for every caller-supplied file path: the path
        must stay inside the worktree. Rejected are absolute paths and ``..``
        traversal; a leading ``:`` (pathspec magic -- glob characters are
        legal name characters, taken literally by every downstream git call);
        any ``.git`` component (in a linked worktree ``.git`` is a *file*
        whose overwrite hijacks the gitdir); and a leading ``.worktrees`` (on
        the user node the worktree is the repo root, so it would reach into
        sibling nodes). Comparisons casefold -- APFS matches names
        case-insensitively, so ``.GIT`` names the same entry there; rejecting
        a literal ``.GIT`` file on a case-sensitive host is the accepted
        cost. Fractal's own content (``wiki/``, ``.fractal/``) passes: it is
        readable project state, filtered or collapsed by consumers, not a
        boundary; the write tier (:meth:`_validate_writable`) is stricter.

        Args:
            path: Worktree-relative file path.

        Returns:
            The normalized (POSIX) worktree-relative path.

        Raises:
            ValueError: If ``path`` escapes the worktree.

        """
        rel = pathlib.PurePosixPath(path)
        if not path or rel.is_absolute() or not rel.parts or '..' in rel.parts:
            raise ValueError(f'Invalid file path: {path!r}')
        # no leading pathspec magic; glob chars are legal name chars that
        # every downstream pathspec disarms with :(literal)
        if path.startswith(':'):
            raise ValueError(f'Invalid file path: {path!r}')
        # structural machinery components, casefolded
        parts = tuple(part.casefold() for part in rel.parts)
        if '.git' in parts or parts[0] == WORKTREES_FOLDER:
            raise ValueError(f'Cannot touch fractal machinery: {path!r}')
        # containment: a symlinked intermediate directory must not escape
        if not (self.worktree / rel).resolve().is_relative_to(self.worktree):
            raise ValueError(f'Invalid file path: {path!r}')
        return rel.as_posix()

    def _validate_writable(self: Files, path: str) -> str:
        """Validate a worktree-relative path for the upload tier.

        Everything the structural tier rejects, plus any ``.fractal``
        component: a foreign tree's ``.fractal/`` is stale machinery a
        wholesale project upload works better without, and a raw-bytes
        overwrite of a live ``.fractal/<branch>/config.json`` would corrupt a
        running node's caps -- the one path where an upload reaches the
        control plane rather than content. The project wiki stays writable:
        it is project content the user owns, and uploading an existing
        project must carry its wiki.

        Args:
            path: Worktree-relative file path.

        Returns:
            The normalized (POSIX) worktree-relative path.

        Raises:
            ValueError: If ``path`` escapes the worktree or names machinery.

        """
        posix = self._validate_relpath(path)
        parts = tuple(part.casefold() for part in posix.split('/'))
        if FRACTAL_FOLDER in parts:
            raise ValueError(f'Cannot touch fractal machinery: {path!r}')
        return posix

    def _not_readable(self: Files, path: str) -> ValueError:
        """Build the unreadable-project-file error."""
        return ValueError(f'Not a readable project file: {path!r}')


# ------ helper functions


def _epoch(instant: str, /) -> float:
    """Convert a row instant (ISO 8601, millisecond precision) to an epoch.

    Args:
        instant: A ``started_at``/``ended_at`` value.

    Returns:
        Seconds since the epoch.

    """
    parsed = dt.datetime.strptime(instant, '%Y-%m-%dT%H:%M:%S.%fZ')
    return parsed.replace(tzinfo=dt.UTC).timestamp()


def _instant(epoch: int, /) -> str:
    """Convert a commit's author epoch to a row-format instant.

    Args:
        epoch: Seconds since the epoch (git ``%at``, whole seconds).

    Returns:
        ISO 8601 UTC timestamp, millisecond precision.

    """
    moment = dt.datetime.fromtimestamp(epoch, dt.UTC)
    return moment.strftime('%Y-%m-%dT%H:%M:%S.') + '000Z'
