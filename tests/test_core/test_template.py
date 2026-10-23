"""Test the ``fractal.core.template`` module.

The input contract: a spawn's inputs merge from the ``--values`` sheet and
the ``--set`` overrides, later sources winning, and the sheet reads flat or
in the provenance shape a node's own ``_template.toml`` records. The shared
contract: a repo-rooted include reads one plain tracked file at the
template's commit and refuses every other shape by name, and the record of
the shared files read validates where it is read.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from fractal.core.node import Node
from fractal.core.template import collect_values, read_provenance
from tests._helpers import _commit_template, _git

__all__ = [
    'test_collect_values_reads_flat_and_provenance_shaped_sheets',
    'test_collect_values_refuses_a_values_table_beside_inputs',
    'test_init_refuses_a_shared_include_outside_the_read_rules',
    'test_init_hints_at_an_uncommitted_shared_file_only_by_its_exact_name',
    'test_read_provenance_validates_the_shared_table',
]


@pytest.mark.parametrize(
    argnames=('sheet', 'sets', 'expected'),
    argvalues=[
        # the flat shape: top-level keys are the inputs
        (
            'role = "scout"\nwork_timeout = "2h"\n',
            [],
            {'role': 'scout', 'work_timeout': '2h'},
        ),
        # the provenance shape: the [values] table holds the inputs
        (
            '[values]\nrole = "scout"\nwork_timeout = "2h"\n',
            [],
            {'role': 'scout', 'work_timeout': '2h'},
        ),
        # a node's own record seeds another: its keys beside the table are ignored
        (
            'path = "templates/scout"\ncommit = "0123abcd"\ninclude = ["NODE.md"]\n'
            '[shared]\n"campaign/DOCTRINE.md" = "0123abcd"\n'
            '[values]\nrole = "scout"\n',
            [],
            {'role': 'scout'},
        ),
        # --set overrides an unwrapped input like a flat one
        (
            '[values]\nrole = "scout"\nwork_timeout = "2h"\n',
            ['work_timeout="30m"'],
            {'role': 'scout', 'work_timeout': '30m'},
        ),
        # a scalar values key is an ordinary input, not a wrapper
        ('values = 3\n', [], {'values': 3}),
    ],
    ids=['flat', 'wrapped', 'provenance', 'set_override', 'scalar_values'],
)
def test_collect_values_reads_flat_and_provenance_shaped_sheets(
    tmp_path: pathlib.Path,
    sheet: str,
    sets: list[str],
    expected: dict,
) -> None:
    """A ``--values`` sheet reads flat or in the shape ``_template.toml`` records."""
    path = tmp_path / 'values.toml'
    path.write_text(sheet, encoding='utf-8')
    assert collect_values(values=path, sets=sets, pin=None) == expected


def test_collect_values_refuses_a_values_table_beside_inputs(
    tmp_path: pathlib.Path,
) -> None:
    """A ``[values]`` table beside other top-level inputs refuses, naming them.

    The sheet reads two ways -- the table as the inputs, or every top-level
    key as one -- so it refuses rather than guess; the provenance keys are
    the one company the table keeps.
    """
    path = tmp_path / 'values.toml'
    path.write_text(
        'role = "scout"\nteam = "blue"\ncommit = "0123abcd"\n[values]\nrole = "lead"\n',
        encoding='utf-8',
    )
    with pytest.raises(ValueError, match='ambiguous') as refused:
        collect_values(values=path, sets=None, pin=None)
    assert 'top-level keys role, team' in str(refused.value)
    assert 'commit' not in str(refused.value).split('keys', 1)[1]


@pytest.mark.parametrize(
    argnames=('statement', 'refusal'),
    argvalues=[
        (
            "{% include '/campaign/../campaign/DOCTRINE.md' %}",
            "Template 'templates/station' source NODE.md includes"
            ' \'/campaign/../campaign/DOCTRINE.md\', which contains ".." steps',
        ),
        (
            "{% include '/.Git/config' %}",
            "Template 'templates/station' source NODE.md includes '/.Git/config',"
            ' which contains a .git, .fractal or .worktrees component',
        ),
        (
            "{% include '/.Fractal/main/NODE.md' %}",
            "Template 'templates/station' source NODE.md includes"
            " '/.Fractal/main/NODE.md', which contains a .git, .fractal or"
            ' .worktrees component',
        ),
        (
            "{% include '/.worktrees/main.task/NODE.md' %}",
            "Template 'templates/station' source NODE.md includes"
            " '/.worktrees/main.task/NODE.md', which contains a .git, .fractal"
            ' or .worktrees component',
        ),
        (
            "{% include '/' %}",
            "Template 'templates/station' source NODE.md includes '/', which"
            ' names no repo-relative file',
        ),
        (
            "{% include '/.' %}",
            "Template 'templates/station' source NODE.md includes '/.', which"
            ' names no repo-relative file',
        ),
        (
            "{% include '//etc/passwd' %}",
            "Template 'templates/station' source NODE.md includes"
            " '//etc/passwd', which names no repo-relative file",
        ),
        (
            "{% include '/campaign/LINK.md' %}",
            "Template 'templates/station' source NODE.md includes"
            " '/campaign/LINK.md', a symlink at",
        ),
        (
            "{% include '/campaign' %}",
            "Template 'templates/station' source NODE.md includes '/campaign',"
            ' a folder at',
        ),
        (
            "{% include '/vendor/lib' %}",
            "Template 'templates/station' source NODE.md includes '/vendor/lib',"
            ' a submodule at',
        ),
        (
            "{% include '/campaign/MISSING.md' %}",
            "Template 'templates/station' source NODE.md includes"
            " '/campaign/MISSING.md', which is not tracked at commit",
        ),
        (
            "{% include '/campaign/Auth.json' %}",
            "Template 'templates/station' source NODE.md includes a"
            " credential-named file: '/campaign/Auth.json'",
        ),
        # a refusal inside a shared file names that file as the source
        (
            "{% include '/campaign/NESTED.md' %}",
            "Template 'templates/station' shared file campaign/NESTED.md includes"
            " '/campaign/MISSING.md', which is not tracked at commit",
        ),
        # a computed name cannot be read ahead of rendering
        (
            "{% set doctrine = '/campaign/DOCTRINE.md' %}{% include doctrine %}",
            'Template file templates/station/NODE.md:3: included source not'
            " found: '/campaign/DOCTRINE.md'; a repo-rooted include must name"
            ' its file literally',
        ),
    ],
    ids=[
        'traversal_step',
        'git_component',
        'fractal_component',
        'worktrees_component',
        'root_only',
        'root_dot',
        'absolute',
        'symlink',
        'folder',
        'submodule',
        'missing',
        'credential',
        'nested',
        'non_literal',
    ],
)
def test_init_refuses_a_shared_include_outside_the_read_rules(
    git_repo: pathlib.Path,
    statement: str,
    refusal: str,
) -> None:
    """Every refused repo-rooted include names the template, its source, and the name.

    A shared file is one plain file tracked at the template's commit: a
    name that is only the root or absolute past its ``/``, a ``..`` step,
    a ``.git``, ``.fractal``, or ``.worktrees`` component (matched
    casefolded), a symlink, a folder, a submodule, a path the commit does
    not track, and a credential-named file (matched case-blind) each
    refuse, naming the template source or shared file that holds the
    include, and so does a computed name, which the seed cannot read
    before rendering. Every refusal fires before any worktree exists.
    """
    Node(git_repo).init(agent='claude', user=True)
    # the repo carries one of each shape beside a plain shared file
    files = {
        'DOCTRINE.md': 'doctrine\n',
        'NESTED.md': "{% include '/campaign/MISSING.md' %}",
        'Auth.json': '{}\n',
    }
    _commit_template(git_repo, 'campaign', files)
    (git_repo / 'campaign' / 'LINK.md').symlink_to('DOCTRINE.md')
    head = _git(git_repo, 'rev-parse', 'HEAD').stdout.strip()
    _git(git_repo, 'update-index', '--add', '--cacheinfo', f'160000,{head},vendor/lib')
    _git(git_repo, 'add', 'campaign')
    _git(git_repo, 'commit', '-m', 'a symlink and a submodule')
    charter = f'## Instructions\n\n{statement}\n\n## Completion Requirements\n\nDone.\n'
    files = {'config.json': '{}\n', 'NODE.md': charter}
    _commit_template(git_repo, 'templates/station', files)
    with pytest.raises(ValueError, match=re.escape(refusal)):
        Node(git_repo).init(name='task', template=f'{git_repo}/templates/station')
    # nothing landed -- no worktree, no registry row
    assert not (git_repo / '.worktrees' / 'main.task').exists()
    assert not Node(git_repo).db.exists('nodes', where={'node': 'main.task'})


@pytest.mark.parametrize(
    argnames=('name', 'uncommitted'),
    argvalues=[
        ('/campaign/DRAFT.md', True),
        ('/linked/DOCTRINE.md', False),
        ('/campaign/doctrine.md', False),
    ],
    ids=['uncommitted', 'symlinked_parent', 'wrong_case'],
)
def test_init_hints_at_an_uncommitted_shared_file_only_by_its_exact_name(
    git_repo: pathlib.Path,
    name: str,
    uncommitted: bool,
) -> None:
    """The uncommitted-copy hint names only an untracked file under its own name.

    An untracked ``DRAFT.md`` draws the hint. A name that reaches a file
    on disk through an untracked symlinked folder, or that differs in
    letter case from a tracked file (which a case-insensitive filesystem
    still finds), is no uncommitted copy -- committing would not track
    the name -- so it never draws the hint.
    """
    Node(git_repo).init(agent='claude', user=True)
    _commit_template(git_repo, 'campaign', {'DOCTRINE.md': 'doctrine\n'})
    # an untracked file and an untracked folder link sit on disk only
    draft = git_repo / 'campaign' / 'DRAFT.md'
    draft.write_text('draft\n', encoding='utf-8')
    (git_repo / 'linked').symlink_to('campaign')
    # each name must reach a file on disk, which a letter-case variant does only
    # where the filesystem folds case: with nothing there the hint checks nothing
    if not (git_repo / name.lstrip('/')).exists():
        pytest.skip('requires a case-insensitive filesystem')
    include = f"{{% include '{name}' %}}"
    charter = f'## Instructions\n\n{include}\n\n## Completion Requirements\n\nDone.\n'
    files = {'config.json': '{}\n', 'NODE.md': charter}
    _commit_template(git_repo, 'templates/station', files)
    with pytest.raises(ValueError, match='which is not tracked at commit') as refused:
        Node(git_repo).init(name='task', template=f'{git_repo}/templates/station')
    hinted = '(an uncommitted copy exists on disk)' in str(refused.value)
    assert hinted == uncommitted


@pytest.mark.parametrize(
    argnames=('table', 'message'),
    argvalues=[
        ('shared = "campaign/DOCTRINE.md"\n', 'shared is not a table'),
        (
            '[shared]\n"../DOCTRINE.md" = "{blob}"\n',
            "shared path is not repo-relative: '../DOCTRINE.md'",
        ),
        (
            '[shared]\n"/etc/DOCTRINE.md" = "{blob}"\n',
            "shared path is not repo-relative: '/etc/DOCTRINE.md'",
        ),
        (
            '[shared]\n"campaign/DOCTRINE.md" = "deadbeef"\n',
            "shared blob id is not a full 40-hex id: 'deadbeef'",
        ),
        (
            '[shared]\n"campaign/DOCTRINE.md" = 7\n',
            'shared blob id is not a full 40-hex id: 7',
        ),
    ],
    ids=['not_a_table', 'traversal_step', 'absolute', 'short_id', 'not_text'],
)
def test_read_provenance_validates_the_shared_table(
    tmp_path: pathlib.Path,
    table: str,
    message: str,
) -> None:
    """A hand-edited ``[shared]`` table refuses where the record is read.

    Each recorded shared file is a repo-relative path -- never absolute,
    never a ``..`` step -- mapped to the full 40-hex blob id read.
    """
    blob = '0123456789abcdef0123456789abcdef01234567'
    head = f'path = "templates/station"\ncommit = "{blob}"\n'
    record = tmp_path / '_template.toml'
    record.write_text(head + table.format(blob=blob), encoding='utf-8')
    with pytest.raises(ValueError, match=re.escape(message)):
        read_provenance(tmp_path)
