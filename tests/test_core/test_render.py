"""Test the ``fractal.core.render`` module.

Runtime substitution is pinned against GNU ``envsubst``. Seed templates
compose committed sources through Jinja without reinterpreting supplied data
or changing the runtime variable grammar. The remaining tests cover the
static variable map, prompt assembly, and the chat's runtime sentinels.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib

import pytest
import tomli_w

import fractal.core.render
from fractal.core.node import Node
from fractal.core.render import _VarTemplate, render_seed

__all__ = [
    'test_var_template_matches_envsubst',
    'test_seed_composes_selected_outputs_from_literal_inputs',
    'test_seed_replays_native_values_with_stable_table_order',
    'test_seed_reports_the_source_of_rendering_errors',
    'test_seed_requires_values_displayed_through_containers',
    'test_seed_refuses_mutation_and_ambient_state',
    'test_seed_preserves_text_with_jinja_whitespace_rules',
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


@pytest.mark.parametrize('reverse', [False, True])
def test_seed_composes_selected_outputs_from_literal_inputs(reverse: bool) -> None:
    """Includes share immutable source and data across independently rendered outputs."""
    sources = {
        'NODE.md': (
            b"{% extends '_partials/base.md' %}"
            b"{% import '_partials/macros.md' as prose %}"
            b'{% block title %}{{ prose.title(role) }}{% endblock %}'
            b'{% block body %}'
            b"{% include 'steps/01-summary.md' %}"
            b'{% for target in targets %}- {{ target }}\n{% endfor %}'
            b'{% if enabled %}{{ required_when_enabled }}{% else %}disabled{% endif %}\n'
            b"{% include 'optional.md' ignore missing %}"
            b'{% raw %}{{literal}}{% endraw %}{% endblock %}'
        ),
        'steps/01-summary.md': b"mission: {% include '_partials/mission.md' %}\n",
        '_partials/base.md': (
            b'# {% block title %}{% endblock %}\n{% block body %}{% endblock %}\n'
        ),
        '_partials/macros.md': b'{% macro title(role) %}Role: {{ role }}{% endmacro %}',
        '_partials/mission.md': (
            b'{{ mission }} | {{ details.note }} | $VAR ${CURRENT_BRANCH%.*} $$'
        ),
        'README.md': b'{{ missing_documentation_example }}',
        'unused.bin': b'\xff',
    }
    values = {
        'role': 'reviewer',
        'mission': '{{ untouched }}',
        'targets': ['first', 'second'],
        'enabled': False,
        'details': {'note': '<a & b>'},
    }
    files = ['NODE.md', 'steps/01-summary.md']
    if reverse:
        files.reverse()
    rendered = render_seed(sources, files, values=values, path='nodes/worker')
    summary = b'mission: {{ untouched }} | <a & b> | $VAR ${CURRENT_BRANCH%.*} $$\n'
    assert rendered == {
        'NODE.md': (
            b'# Role: reviewer\n'
            + summary
            + b'- first\n- second\ndisabled\n{{literal}}\n'
        ),
        'steps/01-summary.md': summary,
    }
    assert sources['steps/01-summary.md'] == (
        b"mission: {% include '_partials/mission.md' %}\n"
    )


def test_seed_replays_native_values_with_stable_table_order() -> None:
    """TOML round-trips preserve rendering, including nested table iteration."""
    values = {
        'profile': {'nested': {'z': 2, 'a': 1}, 'enabled': False},
        'targets': [{'z': 2, 'a': 1}, {'b': 4, 'a': 3}],
        **tomllib.loads('created = 2026-09-04\ncount = 3\nratio = 1.5\n'),
    }
    sources = {
        'NODE.md': (
            b'{{ profile | join(",") }}\n{{ profile.nested | join(",") }}\n'
            b'{% for target in targets %}{{ target | join(",") }};{% endfor %}\n'
            b'{{ count + ratio }} {{ created }} {{ created.year }}\n'
        ),
    }
    recorded = tomllib.loads(tomli_w.dumps(values))
    seeded = render_seed(sources, ['NODE.md'], values=values, path='nodes/worker')
    replayed = render_seed(sources, ['NODE.md'], values=recorded, path='nodes/worker')
    expected = {
        'NODE.md': b'enabled,nested\na,z\na,z;a,b;\n4.5 2026-09-04 2026\n',
    }
    assert seeded == expected
    assert replayed == expected
    assert list(values['profile']) == ['nested', 'enabled']


@pytest.mark.parametrize(
    argnames=('body', 'message'),
    argvalues=[
        (b'header\n{{ mission }}', '_partials/body.md:2'),
        (b'header\n{% if %}', '_partials/body.md:2'),
        (b"header\n{% include '../secret.md' %}", '_partials/body.md:2'),
        (b"header\n{% include '/secret.md' %}", '_partials/body.md:2'),
        (b'header\n{{ 1 / 0 }}', '_partials/body.md:2'),
        (b'header\n{{ "text" + 1 }}', '_partials/body.md:2'),
        (b'header\n\xff', '_partials/body.md:2: source is not UTF-8'),
    ],
)
def test_seed_reports_the_source_of_rendering_errors(body: bytes, message: str) -> None:
    """Errors name the included source while escaped names cannot resolve."""
    sources = {
        'NODE.md': b"{% include '_partials/body.md' %}",
        '_partials/body.md': body,
        'secret.md': b'not reachable through an absolute or traversing name',
    }
    with pytest.raises(ValueError) as exc:
        render_seed(
            sources,
            ['NODE.md'],
            values={},
            path='nodes/worker',
            remedy='add the input to the recorded [values] table',
        )
    assert f'nodes/worker/{message}' in str(exc.value)
    if b'{{ mission }}' in body:
        assert 'mission' in str(exc.value)
        assert 'add the input to the recorded [values] table' in str(exc.value)
    if b'secret.md' in body:
        assert 'included source not found' in str(exc.value)


@pytest.mark.parametrize(
    argnames='expression',
    argvalues=[
        'missing',
        '[missing] | join(",")',
        '[missing]',
        '{"value": missing}',
        '[missing] | string',
        '{"value": missing} | string',
    ],
)
def test_seed_requires_values_displayed_through_containers(expression: str) -> None:
    """Only optional inputs may remain undefined, including inside displayed data."""
    source = '{{ ' + expression + ' }}'
    sources = {
        'NODE.md': source.encode('utf-8'),
        'optional.md': (
            b'{{ missing is defined }} {{ missing | default("fallback") }}'
            b'{% if false %}{{ missing }}{% endif %}\n'
        ),
    }
    rendered = render_seed(sources, ['optional.md'], values={}, path='nodes/worker')
    assert rendered == {'optional.md': b'False fallback\n'}
    with pytest.raises(ValueError, match="'missing' is undefined"):
        render_seed(sources, ['NODE.md'], values={}, path='nodes/worker')


@pytest.mark.parametrize(
    argnames='expression',
    argvalues=[
        'targets.append("extra")',
        'targets | random',
        'lipsum()',
        'created.today()',
        'created.strftime("%B")',
        '"{0:%B}".format(created)',
        '"{created:%B}".format_map({"created": created})',
        'timestamp.now()',
        'timestamp.astimezone()',
        'timestamp.tzinfo.tzname(timestamp)',
    ],
)
def test_seed_refuses_mutation_and_ambient_state(expression: str) -> None:
    """Templates cannot mutate their inputs or obtain random, clock, or locale data."""
    values = tomllib.loads(
        'targets = ["first"]\ncreated = 2026-09-04\ntimestamp = 2026-09-04T12:00:00Z\n'
    )
    source = '{{ ' + expression + ' }}'
    with pytest.raises(ValueError, match=r'nodes/worker/NODE\.md:1'):
        render_seed(
            {'NODE.md': source.encode('utf-8')},
            ['NODE.md'],
            values=values,
            path='nodes/worker',
        )
    assert values['targets'] == ['first']


@pytest.mark.parametrize(
    argnames=('source', 'expected'),
    argvalues=[
        (b'plain', b'plain'),
        (b'plain\n', b'plain\n'),
        (b'plain\r\n{{ value }}\r\n', b'plain\nfirst\r\nsecond\n'),
        (b'plain\r{{ value }}\r', b'plain\nfirst\r\nsecond\n'),
        (b'  {% if true %}\nbody\n{% endif %}\n', b'  \nbody\n\n'),
        (b'  {%- if true -%}\nbody\n{%- endif -%}\n', b'body'),
        (b'{% raw %}{{ x }}{% endraw %} {# hidden #}', b'{{ x }} '),
    ],
)
def test_seed_preserves_text_with_jinja_whitespace_rules(
    source: bytes,
    expected: bytes,
) -> None:
    """Jinja normalizes source newlines while values and explicit spacing stay literal."""
    rendered = render_seed(
        {'NODE.md': source},
        ['NODE.md'],
        values={'value': 'first\r\nsecond'},
        path='nodes/worker',
    )
    assert rendered['NODE.md'] == expected


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
