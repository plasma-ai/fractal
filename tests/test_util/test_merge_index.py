"""Tests for the ``_index.md`` custom merge driver (consumed from ``wiki``).

The driver splits each ``_index.md`` at the first ``***``. Above it, the
regenerated frontmatter keys (``name``/``updated``) normalize to ours while
the authored keys get a real three-way merge, and the link block resolves to
the UNION of both sides' rows -- ours' layout wins and rows present only in
theirs are appended above the closing ``***``, deduplicated, so a merge never
silently drops one side's additions (the next ``wiki update`` re-sorts the
block and prunes whatever rows went stale). The block *below* (hand-written
content) is a real three-way merge via ``git merge-file``. The driver
recombines the halves and exits nonzero when either merge left conflicts.

These tests drive both real ``git merge`` scenarios -- with the driver wired
exactly as the repo configures it -- and direct invocations on crafted
``%A``/``%O``/``%B`` files, to pin the behaviors that are load-bearing for every
wiki and memory merge.
"""

from __future__ import annotations

import dataclasses
import pathlib
import subprocess

import pytest

from tests._helpers import _git

__all__ = [
    'test_clean_merge_unions_above_and_merges_below',
    'test_above_unions_divergent_link_blocks',
    'test_below_conflict_leaves_file_unmerged_with_markers',
    'test_thematic_break_below_is_not_a_separator',
    'test_empty_index_unions_links',
    'test_exit_status_reflects_below_merge',
    'test_many_below_conflicts_never_wrap_to_clean_exit',
    'test_added_by_both_empty_base_merges_below',
    'test_no_separator_merges_theirs_below',
    'test_malformed_separator_degrades_to_take_below',
    'test_stray_separator_in_frontmatter_still_splits_above',
    'test_bom_side_keeps_its_authored_frontmatter',
    'test_trailing_whitespace_frontmatter_delimiter_still_skips',
    'test_union_above_carries_both_links_and_update_prunes_stale_rows',
]


# ------ real git merge behaviors


def test_clean_merge_unions_above_and_merges_below(
    tmp_path: pathlib.Path,
) -> None:
    """A clean merge unions the link rows and three-way-merges below.

    Ours and theirs regenerate different link blocks (above); theirs also appends
    a line below. The result must be byte-identical to ours' above plus theirs'
    unique row appended above the closing ``***`` plus the three-way-merged
    below -- proving the recombination introduces no drift and no side's
    addition is dropped.
    """
    below_base = 'Manual line 1.\nManual line 2.\n'
    below_theirs = 'Manual line 1.\nManual line 2.\nTheirs line 3.\n'
    result = _three_way_merge(
        tmp_path / 'repo',
        base=_index(('[[a|a]]: alpha',), below_base),
        ours=_index(('[[a|a]]: alpha', '[[c|c]]: gamma'), below_base),
        theirs=_index(('[[a|a]]: alpha', '[[b|b]]: beta'), below_theirs),
    )
    expected = _index(('[[a|a]]: alpha', '[[c|c]]: gamma'), below_theirs)
    expected = expected.replace('***\n', '[[b|b]]: beta\n\n***\n')
    assert result.returncode == 0
    assert not result.unmerged
    assert result.text == expected
    # the shared row deduplicates: the union appends only theirs' unique row
    assert result.text.count('[[a|a]]: alpha') == 1


def test_above_unions_divergent_link_blocks(
    tmp_path: pathlib.Path,
) -> None:
    """Divergent link blocks union -- ours' layout first; below still merges.

    The separator sits at a different line number in each version (different link
    counts), yet the below content is extracted per-file and merged correctly:
    ours' unique link survives in place, theirs' unique link is appended after
    it (the union never drops a side's addition), and theirs' below addition
    is picked up.
    """
    result = _three_way_merge(
        tmp_path / 'repo',
        base=_index(('[[a|a]]: alpha',), 'Note A.\n'),
        ours=_index(('[[a|a]]: alpha', '[[c|c]]: gamma'), 'Note A.\n'),
        theirs=_index(('[[a|a]]: alpha', '[[b|b]]: beta'), 'Note A.\nNote B.\n'),
    )
    assert result.returncode == 0
    assert not result.unmerged
    assert '[[c|c]]: gamma' in result.text  # ours' above kept
    assert result.text.count('[[b|b]]: beta') == 1  # theirs' row unioned once
    # ours' layout leads; theirs' unique row rides appended after it
    assert result.text.index('[[c|c]]: gamma') < result.text.index('[[b|b]]: beta')
    assert 'Note B.' in result.text  # theirs' below merged in


def test_below_conflict_leaves_file_unmerged_with_markers(
    tmp_path: pathlib.Path,
) -> None:
    """Conflicting below edits surface as a real, unresolved git conflict.

    Both sides rewrite the same below line. The driver must exit non-zero so git
    leaves the path unmerged with conflict markers -- never staging a marker-laden
    file as cleanly resolved. Ours' above is still kept.
    """
    result = _three_way_merge(
        tmp_path / 'repo',
        base=_index(('[[a|a]]: alpha',), 'Shared note.\n'),
        ours=_index(('[[a|a]]: alpha',), 'Ours rewrote the note.\n'),
        theirs=_index(('[[a|a]]: alpha',), 'Theirs rewrote the note.\n'),
    )
    assert result.returncode != 0
    assert result.unmerged
    assert '<<<<<<<' in result.text
    assert '>>>>>>>' in result.text
    assert '[[a|a]]: alpha' in result.text  # above still from ours


def test_thematic_break_below_is_not_a_separator(
    tmp_path: pathlib.Path,
) -> None:
    """A ``***`` thematic break *below* the separator is merged, not re-split.

    The driver splits only at the first ``***``. Manual content may contain its
    own ``***`` thematic breaks; those must travel through the three-way merge
    untouched (here ours and theirs edit opposite sides of an inner ``***``).
    """
    base_below = 'Intro.\n\n***\n\nOutro.\n'
    ours_below = 'Intro.\nOurs intro add.\n\n***\n\nOutro.\n'
    theirs_below = 'Intro.\n\n***\n\nOutro.\nTheirs outro add.\n'
    result = _three_way_merge(
        tmp_path / 'repo',
        base=_index(('[[a|a]]: alpha',), base_below),
        ours=_index(('[[a|a]]: alpha',), ours_below),
        theirs=_index(('[[a|a]]: alpha',), theirs_below),
    )
    separator_lines = sum(1 for line in result.text.splitlines() if line == '***')
    assert result.returncode == 0
    assert not result.unmerged
    assert 'Ours intro add.' in result.text
    assert 'Theirs outro add.' in result.text
    assert separator_lines == 2  # the real separator + the inner thematic break


def test_empty_index_unions_links(tmp_path: pathlib.Path) -> None:
    """An index with no below content (``***`` as the last line) still unions.

    Empty wiki/memory indexes end with ``***`` and nothing after it. The below
    merge of three empty bodies is clean, and the link union carries both
    sides' rows -- ours first, theirs' unique row appended (``wiki update``
    re-sorts and prunes post-merge).
    """
    result = _three_way_merge(
        tmp_path / 'repo',
        base=_index((), ''),
        ours=_index(('[[a|a]]: alpha',), ''),
        theirs=_index(('[[b|b]]: beta',), ''),
    )
    assert result.returncode == 0
    assert not result.unmerged
    assert '[[a|a]]: alpha' in result.text  # ours' above kept
    assert result.text.count('[[b|b]]: beta') == 1  # theirs' row unioned once


# ------ direct-driver exit-status behaviors


@pytest.mark.parametrize(
    argnames=('ours_below', 'theirs_below', 'expect_clean'),
    argvalues=[
        ('Note A.\n', 'Note A.\nAppended by theirs.\n', True),
        ('Ours rewrote it.\n', 'Theirs rewrote it.\n', False),
    ],
)
def test_exit_status_reflects_below_merge(
    tmp_path: pathlib.Path,
    ours_below: str,
    theirs_below: str,
    expect_clean: bool,
) -> None:
    """The driver exits 0 iff the below three-way merge is conflict-free.

    git keys off the driver's exit status: 0 means cleanly resolved (staged),
    non-zero means unresolved (left for the user). A non-conflicting below merges
    clean; a same-region divergence conflicts.
    """
    base = _index(('[[a|a]]: alpha',), 'Note A.\n')
    ours = _index(('[[a|a]]: alpha',), ours_below)
    theirs = _index(('[[a|a]]: alpha',), theirs_below)
    returncode, text = _run_driver(tmp_path, ours=ours, base=base, theirs=theirs)
    if expect_clean:
        assert returncode == 0
        assert '<<<<<<<' not in text
    else:
        assert returncode != 0
        assert '<<<<<<<' in text


def test_many_below_conflicts_never_wrap_to_clean_exit(
    tmp_path: pathlib.Path,
) -> None:
    """>=256 below-conflicts must still exit non-zero (no mod-256 wrap to 0).

    ``git merge-file`` returns the conflict count; the driver forwards it via
    ``exit``, and a shell exit code wraps mod 256. An unclamped count that is a
    multiple of 256 would surface as exit 0 -- git would then stage a file full of
    conflict markers as cleanly resolved (silent corruption). git clamps the count
    (at 127 here), so this is currently safe; this test pins the invariant so a
    future git that stops clamping is caught.
    """
    n = 260  # exceeds 256, so an unclamped count could wrap to a clean-looking 0
    returncode, text = _run_driver(
        tmp_path,
        ours=_index(('[[a|a]]: alpha',), _conflict_below('ours', n)),
        base=_index(('[[a|a]]: alpha',), _conflict_below('base', n)),
        theirs=_index(('[[a|a]]: alpha',), _conflict_below('theirs', n)),
    )
    assert returncode != 0
    assert '<<<<<<<' in text


# ------ adversarial edge behaviors


@pytest.mark.parametrize(
    argnames=('ours_below', 'theirs_below', 'expect_clean'),
    argvalues=[
        ('Same note.\n', 'Same note.\n', True),
        ('Ours note.\n', 'Theirs note.\n', False),
    ],
)
def test_added_by_both_empty_base_merges_below(
    tmp_path: pathlib.Path,
    ours_below: str,
    theirs_below: str,
    expect_clean: bool,
) -> None:
    """An index added on both sides (empty ``%O``) still merges the below.

    When a file is added independently on both branches, git invokes the driver
    with an empty base. The driver must union the link rows and three-way-merge
    the below against the empty base: identical below additions resolve clean,
    while divergent ones conflict. This is the one odd-looking input that is
    genuinely reachable -- added-by-both is a normal merge -- so it must behave
    correctly.
    """
    returncode, text = _run_driver(
        tmp_path,
        ours=_index(('[[a|a]]: alpha',), ours_below),
        base='',
        theirs=_index(('[[b|b]]: beta',), theirs_below),
    )
    assert '[[a|a]]: alpha' in text  # ours' above kept
    assert text.count('[[b|b]]: beta') == 1  # theirs' row unioned once
    if expect_clean:
        assert returncode == 0
        assert '<<<<<<<' not in text
    else:
        assert returncode != 0
        assert '<<<<<<<' in text


def test_no_separator_merges_theirs_below(tmp_path: pathlib.Path) -> None:
    """A separator-less file is merged whole (take-below), not take-ours-drop.

    With no ``^***$`` line, ``split_at_separator`` puts the whole file in the
    "below" bucket, so it gets a full three-way merge rather than silently taking
    ours and discarding theirs. Divergent additions surface as a conflict rather
    than vanishing -- theirs' content is preserved either way.
    """
    ours = '---\nname: w\n---\n\n# w\n\n[[a|a]]: alpha\n'
    base = '---\nname: w\n---\n\n# w\n\n'
    theirs = '---\nname: w\n---\n\n# w\n\n[[b|b]]: beta\nTheirs-only content.\n'
    returncode, text = _run_driver(tmp_path, ours=ours, base=base, theirs=theirs)
    assert returncode != 0  # divergent additions surface as a conflict
    assert 'Theirs-only content.' in text  # theirs' below survives the take-below merge


@pytest.mark.parametrize(
    argnames='separator',
    argvalues=['****', '***x'],
    ids=['four-stars', 'trailing-char'],
)
def test_malformed_separator_degrades_to_take_below(
    tmp_path: pathlib.Path,
    separator: str,
) -> None:
    r"""A separator the driver's pattern can't match degrades to a full merge.

    The driver locates the separator with ``grep '^\*\*\*[[:space:]]*$'``
    (whitespace-tolerant, matching the parser's rstrip), so only extra non-space
    characters fail to match -- a trailing space or CRLF still matches and splits
    (covered by ``test_trailing_whitespace_frontmatter_delimiter_still_skips``).
    A non-matching separator degrades to the no-separator take-below path and
    theirs' below is merged in rather than silently dropped. ``wiki update`` only
    ever emits a bare ``***``, so this is unreachable in practice -- but the
    degrade stays data-safe.
    """
    above = f'---\nname: w\n---\n\n# w\n\n[[a|a]]: alpha\n\n{separator}\n'
    ours = above + 'Ours note.\n'
    base = f'---\nname: w\n---\n\n# w\n\n{separator}\n'
    theirs = (
        f'---\nname: w\n---\n\n# w\n\n[[b|b]]: beta\n\n{separator}\n'
        'Theirs note.\nTheirs-only line.\n'
    )
    _, text = _run_driver(tmp_path, ours=ours, base=base, theirs=theirs)
    assert 'Theirs-only line.' in text  # theirs' below survives the take-below merge


def test_stray_separator_in_frontmatter_still_splits_above(
    tmp_path: pathlib.Path,
) -> None:
    """A bare ``***`` *inside* the frontmatter must not mis-split the file.

    The split skips the leading ``--- ... ---`` block before searching for
    ``^***$``, so a stray ``***`` in a multi-line ``desc`` is ignored and the
    real link block still merges as the above region -- ours' link in place,
    theirs' unioned in once -- rather than leaking through a mistaken "below"
    merge (which would conflict or duplicate the shared body).
    """

    def stray(link: str, below: str) -> str:
        # a literal-block desc whose body holds a bare *** above the real one
        return (
            '---\nname: w\ndesc: |\n  line1\n***\n  line2\n---\n\n# w\n\n'
            f'{link}\n\n***\n{below}'
        )

    returncode, text = _run_driver(
        tmp_path,
        ours=stray('[[a|a]]: alpha', 'Shared note.\n'),
        base=stray('[[a|a]]: alpha', 'Shared note.\n'),
        theirs=stray('[[b|b]]: beta', 'Shared note.\n'),
    )
    assert returncode == 0  # identical below -> clean merge
    assert '[[a|a]]: alpha' in text  # ours' link kept in place
    assert text.count('[[b|b]]: beta') == 1  # theirs' link unioned, not leaked
    assert text.count('Shared note.') == 1  # the below region merged intact


def test_bom_side_keeps_its_authored_frontmatter(tmp_path: pathlib.Path) -> None:
    """A UTF-8 BOM on ours must not read as frontmatter-less.

    The Python parser tolerates a leading BOM, so a BOM'd side is a legal
    input. Frontmatter detection blind to the BOM would classify the side
    as having no frontmatter and silently replace its authored keys with
    base's -- exit 0, no conflict, the ``desc`` edit gone. The merged
    output normalizes the BOM away (the wiki suite pins no-residue), but
    never at the price of the authored keys.
    """

    def page(desc: str, below: str, *, bom: bool = False) -> str:
        prefix = '﻿' if bom else ''
        return (
            f'{prefix}---\nname: w\ndesc: {desc}\n---\n\n# w\n\n'
            f'[[a|a]]: alpha\n\n***\n{below}'
        )

    returncode, text = _run_driver(
        tmp_path,
        ours=page('Ours authored desc.', 'Shared note.\n', bom=True),
        base=page('Base desc.', 'Shared note.\n'),
        theirs=page('Base desc.', 'Theirs note.\n'),
    )
    assert returncode == 0
    assert 'desc: Ours authored desc.' in text
    assert 'desc: Base desc.' not in text
    assert '﻿' not in text
    assert 'Theirs note.' in text


def test_trailing_whitespace_frontmatter_delimiter_still_skips(
    tmp_path: pathlib.Path,
) -> None:
    """A trailing-space ``---`` close still bounds the frontmatter.

    Delimiter matching mirrors the Python parser's ``strip``/``rstrip`` (wiki's
    ``_extract_frontmatter``), so a ``---`` close written with a trailing space
    -- which Python tolerates -- does not fall through and mis-split at a bare
    ``***`` inside a multi-line frontmatter scalar. The generated above-block is
    still taken from ours, never conflicted.
    """

    def doc(link: str) -> str:
        # trailing-space close delimiter; a bare *** inside a quoted desc scalar
        return f'---\nname: w\ndesc: "a\n***\nb"\n--- \n{link}\n***\nshared\n'

    returncode, text = _run_driver(
        tmp_path,
        ours=doc('[[a|a]]: alpha'),
        base=doc('[[a|a]]: base'),
        theirs=doc('[[a|a]]: beta'),
    )
    assert returncode == 0
    assert '<<<<<<<' not in text  # the generated above-block was not conflicted
    assert '[[a|a]]: alpha' in text  # taken from ours
    assert '[[a|a]]: beta' not in text


# ------ end-to-end union safety (the design contract)


def test_union_above_carries_both_links_and_update_prunes_stale_rows(
    tmp_path: pathlib.Path,
) -> None:
    """The union carries both sides' links at merge time; update settles them.

    The driver unions the link rows, so a link only *theirs* added survives
    the merge of an ``_index.md`` directly -- no restore step is load-bearing
    anymore. The link block stays a *derived view*: the post-merge
    ``wiki update`` re-sorts the unioned rows and prunes whatever rows went
    stale (a deleted page's row no longer rides merges back in -- the
    resurrection class). This drives the real cycle: ours and theirs each add
    a distinct page, the merged index carries both links, and after deleting
    a page its row is pruned by the next update.
    """
    # fresh repo with the index driver wired
    repo = tmp_path / 'repo'
    _init_repo_with_driver(repo)
    wiki_dir = repo / 'wiki'
    index = wiki_dir / '_index.md'
    # base: a wiki with a single page a (seeded with the strict naming policy
    # every wiki init call site carries)
    _wiki(
        wiki_dir,
        'init',
        '--settings={"naming": {"validate": ["ascii", "identifier"]}}',
    )
    (wiki_dir / 'a.md').write_text('# a\n\nAlpha.\n', encoding='utf-8')
    _wiki(wiki_dir, 'update')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'base')
    # theirs: add page b on a side branch (index regenerates with [[b]])
    _git(repo, 'checkout', '-b', 'other')
    (wiki_dir / 'b.md').write_text('# b\n\nBeta.\n', encoding='utf-8')
    _wiki(wiki_dir, 'update')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'theirs')
    # ours: add page c back on main (index regenerates with [[c]])
    _git(repo, 'checkout', 'main')
    (wiki_dir / 'c.md').write_text('# c\n\nGamma.\n', encoding='utf-8')
    _wiki(wiki_dir, 'update')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'ours')
    # merge theirs -- the driver unions wiki/_index.md's link rows
    merge = subprocess.run(
        ['git', '-C', f'{repo}', 'merge', 'other'],
        capture_output=True,
        text=True,
        check=False,
    )
    merged = index.read_text(encoding='utf-8')
    # the union carries both sides' links at merge time, deduplicated
    assert merge.returncode == 0, (merge.stdout, merge.stderr)
    assert merged.count('[[b|b]]') == 1  # theirs' link unioned once
    assert '[[c|c]]' in merged  # ours' link kept
    assert (wiki_dir / 'b.md').is_file()
    # the post-merge wiki update re-sorts the unioned block from ground truth
    _wiki(wiki_dir, 'update')
    settled = index.read_text(encoding='utf-8')
    assert '[[a|a]]' in settled
    assert settled.count('[[b|b]]') == 1
    assert '[[c|c]]' in settled
    # ...and prunes a stale row once its page is deleted, so a hygiene
    # deletion cannot ride a later merge back in as a dangling link
    (wiki_dir / 'c.md').unlink()
    _wiki(wiki_dir, 'update')
    pruned = index.read_text(encoding='utf-8')
    assert '[[c|c]]' not in pruned
    assert pruned.count('[[b|b]]') == 1


# ------ helpers


@dataclasses.dataclass(frozen=True)
class MergeResult:
    """Outcome of a three-way ``_index.md`` merge."""

    returncode: int
    text: str
    unmerged: bool


def _driver() -> pathlib.Path:
    """Path to ``merge_index.sh`` shipped by the installed ``wiki`` package.

    wiki owns this script -- ``wiki config``/``wiki init`` register the stable
    ``wiki _merge`` entry point as the ``merge.wiki`` driver, and ``_merge``
    dispatches ``_index.md`` pathnames to wiki's own copy of the script (wiki
    is a hard dependency that ships ``_assets/**`` as package data). The
    direct invocations exercise exactly that script.
    """
    import wiki

    package = pathlib.Path(wiki.__file__).resolve().parent
    return package / '_assets' / 'git' / 'merge_index.sh'


def _index(links: tuple[str, ...], below: str) -> str:
    """Assemble an ``_index.md``: frontmatter, title, link block, ``***``, below.

    ``links`` are link-list lines (no trailing newline each); ``below`` is the
    hand-written content placed after the ``***`` separator (may be empty). This
    mirrors the shape ``wiki update`` emits: ``---`` frontmatter, an ``# h1``, the
    generated links, the ``***`` separator, then manual content.
    """
    above = '---\nname: w\n---\n\n# w\n\n'
    if links:
        above += '\n'.join(links) + '\n\n'
    above += '***\n'
    return above + below


def _conflict_below(tag: str, n: int, gap: int = 6) -> str:
    """Build a below-section body with ``n`` independently conflicting slots.

    Each slot is one line that differs per side (``slot{i}-{tag}``) followed by
    ``gap`` identical context lines, so ``git merge-file`` keeps the conflicts
    separate instead of coalescing them. ``tag`` is ``base``/``ours``/``theirs``;
    only the slot lines differ across the three, yielding ``n`` real conflicts.
    """
    lines: list[str] = []
    for i in range(n):
        lines.append(f'slot{i}-{tag}')
        lines.extend(f'ctx-{i}-{g}' for g in range(gap))
    return '\n'.join(lines) + '\n'


def _init_repo_with_driver(repo: pathlib.Path) -> None:
    """Init a git repo wiring the wiki merge driver exactly as the repo does.

    The literals mirror wiki's ``configure_git_merge_driver`` registration --
    the ``wiki _merge %O %A %B %L %P`` driver and the ``**/_index.md
    merge=wiki`` attribute -- so a future rename in the wiki package fails
    these merges loudly instead of quietly testing a wiring nothing ships.
    The ``wiki/.wiki/settings.json`` marker declares the wiki root:
    ``wiki _merge`` gates the index merge on it, and an ``_index.md``
    outside every declared wiki takes git's default text merge instead.
    """
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, 'init', '-b', 'main')
    _git(repo, 'config', 'user.email', 'test@test.com')
    _git(repo, 'config', 'user.name', 'Test')
    _git(
        repo,
        'config',
        'merge.wiki.name',
        'wiki merge (auto-resolve generated sections)',
    )
    _git(repo, 'config', 'merge.wiki.driver', 'wiki _merge %O %A %B %L %P')
    (repo / '.gitattributes').write_text(
        '**/_index.md merge=wiki\n',
        encoding='utf-8',
    )
    (repo / 'wiki' / '.wiki').mkdir(parents=True)
    (repo / 'wiki' / '.wiki' / 'settings.json').write_text('{}\n', encoding='utf-8')
    _git(repo, 'add', '.gitattributes', 'wiki/.wiki/settings.json')
    _git(repo, 'commit', '-m', 'configure driver')


def _three_way_merge(
    repo: pathlib.Path,
    *,
    base: str,
    ours: str,
    theirs: str,
    path: str = 'wiki/_index.md',
) -> MergeResult:
    """Drive a real ``git merge`` of an ``_index.md`` through the driver.

    Builds a ``base`` commit on ``main``, a ``theirs`` commit on a side branch,
    and an ``ours`` commit back on ``main``, then merges the side branch. Returns
    the merge's exit code, the resulting file content, and whether git left the
    path unmerged.
    """
    # wire the driver and resolve the _index.md path
    _init_repo_with_driver(repo)
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    # base commit on main
    target.write_text(base, encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'base')
    # theirs on a side branch
    _git(repo, 'checkout', '-b', 'other')
    target.write_text(theirs, encoding='utf-8')
    _git(repo, 'commit', '-am', 'theirs')
    # ours on main
    _git(repo, 'checkout', 'main')
    target.write_text(ours, encoding='utf-8')
    _git(repo, 'commit', '-am', 'ours')
    # merge theirs into ours -- the driver resolves the _index.md
    merge = subprocess.run(
        ['git', '-C', f'{repo}', 'merge', 'other'],
        capture_output=True,
        text=True,
        check=False,
    )
    result = subprocess.run(
        ['git', '-C', f'{repo}', 'status', '--porcelain', '--', path],
        capture_output=True,
        text=True,
        check=True,
    )
    status = result.stdout
    unmerged = bool(status.strip()) and 'U' in status.split()[0]
    return MergeResult(
        returncode=merge.returncode,
        text=target.read_text(encoding='utf-8'),
        unmerged=unmerged,
    )


def _run_driver(
    tmp_path: pathlib.Path,
    *,
    ours: str,
    base: str,
    theirs: str,
) -> tuple[int, str]:
    """Invoke the driver directly on crafted ``%A``/``%O``/``%B`` files.

    Mirrors how ``wiki _merge`` dispatches an ``_index.md`` to
    ``merge_index.sh``: ``%A`` is ours (rewritten in place with the result),
    ``%O`` base, ``%B`` theirs, the conflict-marker size defaulting when
    standalone. Returns the exit code and the rewritten ``%A`` content.
    """
    a = tmp_path / 'A'
    o = tmp_path / 'O'
    b = tmp_path / 'B'
    a.write_text(ours, encoding='utf-8')
    o.write_text(base, encoding='utf-8')
    b.write_text(theirs, encoding='utf-8')
    proc = subprocess.run(
        ['bash', f'{_driver()}', f'{a}', f'{o}', f'{b}'],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, a.read_text(encoding='utf-8')


def _wiki(wiki_dir: pathlib.Path, *args: str) -> None:
    """Run a ``wiki`` CLI command against ``wiki_dir`` (the wiki root).

    ``wiki`` is a hard dependency of the project (it regenerates every
    ``_index.md``), so it is always on PATH where the suite runs.
    """
    subprocess.run(
        ['wiki', *args, '--path', f'{wiki_dir}'],
        capture_output=True,
        text=True,
        check=True,
    )
