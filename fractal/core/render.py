"""Functions for template rendering and prompt assembly."""

from __future__ import annotations

import pathlib
import string
import typing
from typing import Optional

if typing.TYPE_CHECKING:
    from .node import Node

__all__ = []

# run-scoped template vars have no value in a chat (no run/iteration/step);
# show a clear sentinel rather than a blank or stale value -- the loop
# overrides these with live state when it renders the same templates
_CHAT_RUNTIME = {
    'STEP_LABEL': 'N/A (chat)',
    'ITER_LABEL': 'N/A (chat)',
    'ITER_TIMESTAMP': 'N/A (chat)',
    'ITER_REF': 'N/A (chat)',
    'TIME_BUDGET': 'N/A (chat)',
    'COST_BUDGET': 'N/A (chat)',
    'CONTINUE_MODE': 'false',
    'RESUME_MODE': 'false',
    'RESERVE_MODE': 'false',
    'DRAIN_MODE': 'false',
}


class _VarTemplate(string.Template):
    """``$VAR`` substitution matched to GNU ``envsubst`` (the pinned grammar).

    Only ``$NAME`` and ``${NAME}`` are references; everything else -- notably
    ``$$`` -- is passed through verbatim (the ``escaped`` group is made
    unreachable, so ``$$`` is not collapsed to ``$``). Rendering is pinned
    byte-for-byte against ``envsubst``, so a template means exactly what that
    grammar says regardless of what substitutes it.
    """

    pattern = r"""
        \$(?:
          (?P<escaped>(?!))                       |  # unreachable: no $$-escape
          (?P<named>[A-Za-z_][A-Za-z0-9_]*)       |
          \{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}  |
          (?P<invalid>)
        )
    """


def render(text: str, variables: dict[str, str]) -> str:
    """Substitute ``$VAR`` placeholders into ``text``.

    Substitution matches GNU ``envsubst`` (``$NAME``/``${NAME}`` only;
    unknown placeholders and ``$$`` pass through verbatim) -- the grammar
    the renderer is pinned against.

    Args:
        text: The text to render.
        variables: A ``name -> value`` map of template variables.

    Returns:
        The rendered text.

    """
    return _VarTemplate(text).safe_substitute(variables)


def variables(node: Node, overrides: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Derive a node's full template variable map (``overrides`` win).

    The static variables (paths, git, config, modes) are the single
    derivation the loop and every other renderer share. Run-scoped
    variables (step/iteration/budget and the continue/resume/reserve modes)
    are not derived here -- a caller supplies them via ``overrides`` (the
    loop with live state, a chat with ``N/A (chat)``); any placeholder left
    unsupplied stays verbatim.

    Args:
        node: The node whose variables to derive.
        overrides: Variable values layered over (and winning against) the
            derived map.

    Returns:
        A ``name -> value`` map of the template variables.

    """
    # alias branch
    branch = node.branch
    # alias repo/node/worktree dirs
    repo_dir = node.repo_dir
    node_dir = node.node_dir
    worktree_dir = node.worktree
    # alias plans/memory dirs
    plans_dir = node_dir / 'plans'
    memory_dir = node_dir / 'memory'
    # alias project/wiki dirs
    if node.project_path == '.':
        project_dir = repo_dir
        wiki_dir = worktree_dir / 'wiki'
    else:
        project_dir = repo_dir / node.project_path
        wiki_dir = worktree_dir / node.project_path / 'wiki'
    # alias scope dir (space-joined when several roots are scoped)
    if scope := node.config.get('scope'):
        scope_dir = ' '.join(f'{project_dir / root}' for root in scope)
    else:
        scope_dir = ''
    # alias config limits and modes
    max_depth = node.config.get('max_depth', -1)
    max_children = node.config.get('max_children', -1)
    max_descendants = node.config.get('max_descendants', -1)
    detached = node.config.get('detached', False)
    meta = node.config.get('meta') or ''
    # merge the static map under the caller's overrides
    static = {
        'REPO_DIR': f'{repo_dir}',
        'PROJECT_DIR': f'{project_dir}',
        'SCOPE_DIR': scope_dir,
        'WORKTREE_DIR': f'{worktree_dir}',
        'NODE_DIR': f'{node_dir}',
        'PLANS_DIR': f'{plans_dir}',
        'MEMORY_DIR': f'{memory_dir}',
        'WIKI_DIR': f'{wiki_dir}',
        'CURRENT_BRANCH': branch,
        'MAX_DEPTH': f'{max_depth}',
        'MAX_CHILDREN': f'{max_children}',
        'MAX_DESCENDANTS': f'{max_descendants}',
        'DETACHED_MODE': 'true' if detached else 'false',
        'META_MODE': 'true' if meta else 'false',
        'META_TARGET': meta,
    }
    return {**static, **(overrides or {})}


def prompt(
    node: Node,
    step_text: str,
    overrides: Optional[dict[str, str]] = None,
) -> str:
    """Assemble and render a step's full prompt.

    The prompt is the ``NODE.md`` charter, the step body (frontmatter
    stripped), and every active mode doc, joined by blank lines and
    substituted in one pass -- one merged variable map (:func:`variables`)
    both selects the active modes and renders the text. A mode doc
    ``<NAME>.md`` activates when ``<NAME>_MODE`` is ``true`` in that map:
    static modes (``DETACHED``/``META``) derive from config, run-scoped
    modes (``CONTINUE``/``RESUME``/``RESERVE``/``DRAIN``) ride in as
    overrides.
    ``SYNC.md`` never joins -- sync runs as its own step.

    Args:
        node: The node whose prompt to assemble.
        step_text: The step markdown text.
        overrides: Run-scoped variable values (see :func:`variables`).

    Returns:
        The rendered prompt.

    """
    # merge the variable map once -- it both selects the active modes
    # and substitutes the assembled text (overrides win)
    merged = variables(node, overrides)
    # assemble: charter, step body (frontmatter stripped), active modes
    charter = (node.node_dir / 'NODE.md').read_text(encoding='utf-8')
    parts = [charter, '\n', strip_frontmatter(step_text)]
    modes_dir = pathlib.Path(__file__).parent.parent / '_node' / 'modes'
    for mode_file in sorted(modes_dir.glob('*.md')):
        if mode_file.name == 'SYNC.md':
            continue
        if merged.get(f'{mode_file.stem}_MODE') == 'true':
            parts += ['\n', mode_file.read_text(encoding='utf-8')]
    return render(''.join(parts), merged)


def chat_seed(node: Node, *, fresh: bool) -> str:
    """Render the chat framing prepended to a chat turn.

    Always the ``modes/CHAT.md`` mode; for a fresh chat it is preceded by
    the node's ``NODE.md`` charter. Rendered with the node's template
    variables -- real paths/limits, plus chat sentinels (``N/A (chat)``) for
    the run-scoped fields a chat has no value for.

    Args:
        node: The node being chatted with.
        fresh: Also prepend the node's ``NODE.md`` charter (for a fresh
            chat; a fork already carries the node's context).

    Returns:
        The rendered seed text, or ``''`` when the files are absent.

    """
    sections = []
    if fresh:
        node_md = node.node_dir / 'NODE.md'
        if node_md.exists():
            sections.append(node_md.read_text(encoding='utf-8').strip())
    chat_md = pathlib.Path(__file__).parent.parent / '_node' / 'modes' / 'CHAT.md'
    if chat_md.exists():
        sections.append(chat_md.read_text(encoding='utf-8').strip())
    seed = '\n\n'.join(sections)
    return render(seed, variables(node, _CHAT_RUNTIME)) if seed else seed


def strip_frontmatter(text: str) -> str:
    """Return ``text`` with a leading ``---`` frontmatter block removed.

    Byte-conservative line handling: lines split on the newline byte
    alone (a CRLF file keeps its carriage returns, so a CRLF fence line
    is not a fence), the fence check trims leading whitespace only, and
    a block that never closes swallows the rest of the text; every
    surviving line comes back newline-terminated, so a file without a
    trailing newline gains one.

    Args:
        text: The markdown text.

    Returns:
        The body below the frontmatter.

    """
    lines = text.split('\n')
    # a trailing newline yields a final empty element, not an extra line
    if lines and lines[-1] == '':
        lines.pop()
    if lines and lines[0].lstrip() == '---':
        for index, line in enumerate(lines[1:], start=1):
            if line.lstrip() == '---':
                lines = lines[index + 1 :]
                break
        else:
            lines = []
    return ''.join(f'{line}\n' for line in lines)
