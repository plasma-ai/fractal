"""Test the ``fractal.core.render`` module.

The engine is pinned against GNU ``envsubst`` -- the grammar the renderer is
matched to -- so a template renders byte-identically to what ``envsubst``
would produce. The seed-time slot grammar renders ``{{slot}}`` placeholders
once, at init, and must leave the ``$VAR`` grammar untouched. The remaining
tests cover what the static map substitutes and how a chat sees it (real
paths, ``N/A (chat)`` run-state).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional

import pytest

import fractal.core.render
from fractal.core.node import Node
from fractal.core.render import _SlotTemplate, _VarTemplate

__all__ = [
    'test_var_template_matches_envsubst',
    'test_slot_template_fills_byte_for_byte',
    'test_slot_template_raises_on_an_unfilled_slot',
    'test_slot_template_refuses_every_stray_brace_pair',
    'test_render_template_substitutes_static_and_passes_runtime',
    'test_strip_frontmatter_edges',
    'test_build_prompt_assembles_charter_step_and_modes',
    'test_chat_seed_renders_paths_and_chat_sentinels',
]

_ENVSUBST = shutil.which('envsubst')

# a controlled map + templates exercising every substitution edge envsubst knows
_VARS = {'AA': '/x/y', 'BB': 'two words', 'CC': '7'}
_TEMPLATES = [
    'plain $AA end',
    'braced ${AA}/sub',
    'unknown $ZZ and ${ZZ} stay',
    'adjacent $AA$BB',
    'trailing $AA.',
    'dollars $$ and $$AA and a$$b',
    'bare $ and $ space',
    'mixed $AA ${BB} $CC literal',
    'punctuation ($AA), [${BB}]',
]


@pytest.mark.skipif(_ENVSUBST is None, reason='envsubst not installed')
@pytest.mark.parametrize('template', _TEMPLATES)
def test_var_template_matches_envsubst(template: str) -> None:
    """``_VarTemplate`` substitutes byte-identically to GNU ``envsubst``."""
    shell_format = ' '.join(f'${key}' for key in _VARS)
    result = subprocess.run(
        [_ENVSUBST, shell_format],
        input=template,
        capture_output=True,
        text=True,
        env={**os.environ, **_VARS},
    )
    assert _VarTemplate(template).safe_substitute(_VARS) == result.stdout


# the slot fill map + templates exercising the fills (padding spaces,
# adjacency) and the pass-through ($VAR text, shell parameter expansion,
# lone braces) the seed-time grammar must leave byte-identical
_SLOT_VALUES = {'pin': 'abc123', 'mission': 'prove the lemma'}
_SLOT_TEMPLATES = [
    ('pin: {{pin}}\n', 'pin: abc123\n'),
    ('do {{ mission }} now', 'do prove the lemma now'),
    ('{{pin}}{{mission}}', 'abc123prove the lemma'),
    ('keep $VAR and ${CURRENT_BRANCH%.*} and $$', None),
    ('lone { and } and }} stay', None),
]


@pytest.mark.parametrize(('template', 'expected'), _SLOT_TEMPLATES)
def test_slot_template_fills_byte_for_byte(
    template: str,
    expected: Optional[str],
) -> None:
    """``_SlotTemplate`` fills lowercase slots and touches nothing else.

    A ``None`` expectation means byte-identity: ``$VAR`` text, shell text
    such as ``${CURRENT_BRANCH%.*}``, ``$$``, and lone braces pass through
    unchanged, so the prompt-time envsubst grammar is undisturbed.
    """
    if expected is None:
        expected = template
    assert _SlotTemplate(template).substitute(_SLOT_VALUES) == expected


def test_slot_template_raises_on_an_unfilled_slot() -> None:
    """A slot with no value raises ``KeyError`` naming the slot."""
    with pytest.raises(KeyError, match='mission'):
        _SlotTemplate('do {{mission}} now').substitute({'pin': 'abc123'})


@pytest.mark.parametrize(
    argnames='residue',
    argvalues=['{{PIN}}', '{{Pin}}', '{{9lives}}', '{{pin', '{{ }}'],
)
def test_slot_template_refuses_every_stray_brace_pair(residue: str) -> None:
    """Any ``{{`` that is not a lowercase slot raises ``ValueError``.

    The grammar itself refuses an uppercase or mixed-case name, a name
    starting with a digit, an unclosed pair, and an empty pair -- there is
    no escape and no way to write a literal ``{{``.
    """
    with pytest.raises(ValueError, match='Invalid placeholder'):
        _SlotTemplate(residue).substitute(_SLOT_VALUES)


def test_render_template_substitutes_static_and_passes_runtime(
    node_with_db: Node,
) -> None:
    """Static vars resolve and run-scoped vars pass through to the caller.

    An override wins over the (absent) derived value, and ``$MAX_DESCENDANTS``
    substitutes as a static var.
    """
    node = node_with_db
    template = 'wt=$WORKTREE_DIR step=$STEP_LABEL desc=$MAX_DESCENDANTS none=$NOPE'
    rendered = node.render_template(template)
    assert f'wt={node.worktree}' in rendered  # static var -> real path
    assert 'step=$STEP_LABEL' in rendered  # run-scoped: left for the caller
    assert '$MAX_DESCENDANTS' not in rendered  # static var -> substituted
    assert 'none=$NOPE' in rendered  # unknown placeholder passes through
    # an override wins over the (absent) derived value
    overridden = node.render_template(template, overrides={'STEP_LABEL': 'step 1 of 3'})
    assert 'step=step 1 of 3' in overridden


@pytest.mark.parametrize(
    argnames=('text', 'expected'),
    argvalues=[
        # a plain body passes through, gaining a trailing newline if absent
        ('body\n', 'body\n'),
        ('body', 'body\n'),
        # a closed block strips, leading whitespace tolerated on fences
        ('---\nkey: v\n---\nbody\n', 'body\n'),
        ('  ---\nkey: v\n  ---\nbody\n', 'body\n'),
        # a block that never closes swallows the rest of the text
        ('---\nkey: v\nbody\n', ''),
        # a fence with trailing whitespace is not a fence
        ('---\nkey: v\n--- \nbody\n', ''),
        # CRLF fences keep their \r bytes, so no frontmatter is detected
        ('---\r\nkey: v\r\n---\r\nbody\r\n', '---\r\nkey: v\r\n---\r\nbody\r\n'),
        # empty input stays empty
        ('', ''),
    ],
)
def test_strip_frontmatter_edges(text: str, expected: str) -> None:
    """``strip_frontmatter`` holds its documented edge semantics.

    The edges are the byte-contract surface of the prompt assembly: fence
    detection trims leading whitespace only, an unclosed block swallows
    the body, lines split on the newline byte alone (CRLF passes through
    raw), and every surviving line comes back newline-terminated.
    """
    assert fractal.core.render.strip_frontmatter(text) == expected


def test_build_prompt_assembles_charter_step_and_modes(node_with_db: Node) -> None:
    """``build_prompt`` joins charter + stripped step + active modes, rendered.

    One merged variable map both selects the mode docs and substitutes the
    text: an override turns a run-scoped mode on, config-derived modes stay
    off, ``SYNC.md`` never joins, and the step's frontmatter never reaches
    the prompt.
    """
    node = node_with_db
    (node.node_dir / 'NODE.md').write_text(
        'charter for $CURRENT_BRANCH\n',
        encoding='utf-8',
    )
    step = node.node_dir / 'step.md'
    step.write_text(
        '---\nrequires_approval: true\n---\n\ndo the work ($STEP_LABEL)\n',
        encoding='utf-8',
    )
    prompt = node.build_prompt(
        f'{step}',
        overrides={'STEP_LABEL': 'step 1 of 2', 'RESUME_MODE': 'true'},
    )
    # charter leads, the step body follows with its frontmatter stripped,
    # and the activated mode doc trails -- all substituted in one pass
    assert prompt.startswith(f'charter for {node.branch}\n')
    assert 'do the work (step 1 of 2)' in prompt
    assert 'requires_approval' not in prompt
    assert 'This node was paused mid-run' in prompt  # RESUME.md (override: on)
    assert 'Check radio and act on anything' not in prompt  # SYNC.md never joins
    assert 'separate session' not in prompt  # DETACHED.md off (config-derived)
    assert prompt.index('do the work') < prompt.index('This node was paused')


def test_chat_seed_renders_paths_and_chat_sentinels(node_with_db: Node) -> None:
    """A chat seed renders real paths and ``N/A (chat)`` for run-scoped fields."""
    node = node_with_db
    (node.node_dir / 'NODE.md').write_text(
        'node=$NODE_DIR step=$STEP_LABEL desc=$MAX_DESCENDANTS\n',
        encoding='utf-8',
    )
    seed = fractal.core.render.chat_seed(node, fresh=True)
    assert f'node={node.node_dir}' in seed
    assert 'step=N/A (chat)' in seed
    assert '$MAX_DESCENDANTS' not in seed
