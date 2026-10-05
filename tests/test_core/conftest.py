"""Shared fixtures for ``fractal`` tests."""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

import fractal.core
from fractal.core.db import Database
from fractal.core.node import Node
from fractal.core.radio import Radio


@pytest.fixture
def git_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """Return a fresh git repo with an initial commit."""
    return _make_git_repo(tmp_path)


@pytest.fixture
def schema() -> pathlib.Path:
    """Return the path to the packaged node database schema."""
    return pathlib.Path(fractal.core.__file__).parent / 'schema.sql'


@pytest.fixture
def database(tmp_path: pathlib.Path, schema: pathlib.Path) -> Database:
    """Return a fresh initialized database."""
    db = Database(tmp_path / '.db', schema)
    db.init()
    return db


@pytest.fixture
def node_with_db(git_repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Node:
    """Return a node with an initialized DB (no worktree, no init script).

    Creates ``.fractal/<branch>/`` with a minimal ``config.json``
    (so ``node.exists()`` returns True) and an initialized ``.db``
    directly. Fast (~50ms). For tests that only need DB operations.
    """
    node = Node(git_repo)
    result = subprocess.run(
        ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    branch = result.stdout.strip()
    node_dir = git_repo / '.fractal' / branch
    node_dir.mkdir(parents=True)
    config = {
        'project': '.',
        'root': branch,
        'scope': '',
        'agent': 'claude',
        'local': False,
        'detached': False,
    }
    (node_dir / 'config.json').write_text(
        json.dumps(config, indent=2),
        encoding='utf-8',
    )
    (node_dir / '.status').write_text('idle\n', encoding='utf-8')
    node.db.init()
    # default the liveness probe to alive: tests run with no real tmux session,
    # so an 'active' status would otherwise reconcile to 'exited' on any
    # reject-active op; crashed-path tests override with _tmux_session_exists=False
    monkeypatch.setattr(node, '_tmux_session_exists', lambda: True)
    return node


@pytest.fixture
def radio(node_with_db: Node) -> Radio:
    """Return a radio initialized with default channels on a node with DB."""
    radio = Radio(node_with_db)
    radio.init()
    return radio


@pytest.fixture
def radio_pair(radio: Radio) -> tuple[Radio, Radio]:
    """Return two radios over the central DB: the root plus a registered peer.

    The peer gets a real worktree (so its branch resolves) and a hand-built
    config -- no init script. Mirrors production order: the peer's channels
    seed before the root subscribes to it (``child_add``).
    """
    root = radio.node
    repo = root.worktree
    branch = root.branch
    peer_branch = f'{branch}.peer'
    worktree = repo / '.worktrees' / peer_branch
    subprocess.run(
        ['git', 'worktree', 'add', '-b', peer_branch, f'{worktree}', branch],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    node_dir = worktree / '.fractal' / peer_branch
    node_dir.mkdir(parents=True)
    config = {
        'project': '.',
        'root': branch,
        'scope': '',
        'agent': 'claude',
        'local': False,
        'detached': False,
    }
    (node_dir / 'config.json').write_text(
        json.dumps(config, indent=2),
        encoding='utf-8',
    )
    (node_dir / '.status').write_text('idle\n', encoding='utf-8')
    # register + seed the peer, then subscribe the root (mirrors child_add)
    root.db.merge({'node': peer_branch, 'status': 'idle'}, 'nodes')
    peer = Radio(Node(worktree))
    peer.init()
    radio.subscribe(peer_branch)
    return radio, peer


@pytest.fixture(scope='session')
def initialized_node(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Return a fully initialized node (built once per session).

    Creates a git repo, runs ``node init``, and returns a dict
    with ``output``, ``project_dir``, ``node_dir``, ``branch``,
    and ``repo``. Shared across read-only integration tests.
    """
    tmp_path = tmp_path_factory.mktemp('node')
    repo = _make_git_repo(tmp_path)
    node = Node(repo)
    node.init(user=True)
    output = node.init(name='task', agent='claude')
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    node_dir = project_dir / '.fractal' / branch
    return {
        'output': output,
        'project_dir': project_dir,
        'node_dir': node_dir,
        'branch': branch,
        'repo': repo,
    }


# ------ helpers


def _active_run(node: Node) -> int:
    """The node's active run id (the central DB holds every node's runs)."""
    where = {'node': node.branch, 'status': 'active'}
    return node.db.read('runs', where=where, limit=1)[0]['run_id']


def _record_step_cost(
    node: Node,
    *,
    run_id: int,
    cost: float,
    iter: int = 1,
) -> None:
    """Record one completed step of ``cost`` USD in ``run_id`` (for cost rollups)."""
    iter_id = node.record.iter_start(run_id=run_id, iter=iter)
    step_id = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.record.step_cost(step_id=step_id, cost=cost)
    node.record.step_end(step_id=step_id, status='completed', exit_code=0)


def _spawn_parent_child(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Node, Node]:
    """Build a user -> parent -> child tree of real worktrees.

    Returns the parent and child ``Node`` objects, each set ``active`` with a
    started run -- mirroring a running node, so signals attach to a live run.
    """
    Node(git_repo).init(agent='claude', user=True)
    Node(git_repo).init(name='parent')
    parent_wt = git_repo / '.worktrees' / 'main.parent'
    # spawn the child under the parent (_NODE makes it the resolved caller)
    node_dir = parent_wt / '.fractal' / 'main.parent'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(git_repo).init(name='kid')
    monkeypatch.delenv('_NODE')
    child_wt = git_repo / '.worktrees' / 'main.parent.kid'
    parent = Node(parent_wt)
    child = Node(child_wt)
    # bring both up like running nodes -- present live tmux sessions, so the
    # reject-active/reconcile probe and list --live both read the loops alive
    # (else mutating signals reconcile to exited and --live relabels them)
    sessions = frozenset({parent.tmux_session, child.tmux_session})
    monkeypatch.setattr('fractal.util.tmux.probe', lambda: sessions)
    for node in (parent, child):
        node.status_set('active')
        node.record.run_start()
    return parent, child


def _spawn_chain(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Node, Node, Node]:
    """Build a user -> p (active) -> c (completed) -> g (active) chain of worktrees.

    Returns the ``(p, c, g)`` nodes three levels deep. ``p`` and the grandchild
    ``g`` are ``active`` with started runs; the intermediate ``c`` is
    ``completed``, so the only live-active descendant of ``p`` sits below a
    non-active node -- exercising the flat-registry walk reaching a deep one.
    """
    Node(git_repo).init(agent='claude', user=True)
    Node(git_repo).init(name='p')
    p_wt = git_repo / '.worktrees' / 'main.p'
    # spawn the child under p (_NODE makes it the resolved caller)
    node_dir = p_wt / '.fractal' / 'main.p'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(git_repo).init(name='c')
    c_wt = git_repo / '.worktrees' / 'main.p.c'
    # spawn the grandchild under c
    node_dir = c_wt / '.fractal' / 'main.p.c'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(git_repo).init(name='g')
    monkeypatch.delenv('_NODE')
    g_wt = git_repo / '.worktrees' / 'main.p.c.g'
    p = Node(p_wt)
    c = Node(c_wt)
    g = Node(g_wt)
    # bring p and g up like running nodes -- present live tmux sessions, so the
    # reject-active/reconcile probe and list --live both read the loops alive
    # (else mutating signals reconcile to exited and --live relabels them)
    sessions = frozenset({p.tmux_session, g.tmux_session})
    monkeypatch.setattr('fractal.util.tmux.probe', lambda: sessions)
    for node in (p, g):
        node.status_set('active')
        node.record.run_start()
    c.status_set('completed')
    return p, c, g


def _make_git_repo(path: pathlib.Path, *, wiki: str = 'wiki') -> pathlib.Path:
    """Create a git repo with an initial commit at ``path``.

    ``wiki`` is the shared project-wiki folder; any other folder than the
    default is named by a committed ``.fractal/.settings.json``, and
    ``wiki/`` then holds a corpus that is itself a wiki with a stale index,
    so a refresh that reached it would rewrite it.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ['git', 'init', '-b', 'main'],
        cwd=path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'config', 'user.email', 'test@test.com'],
        cwd=path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'config', 'user.name', 'Test'],
        cwd=path,
        capture_output=True,
        check=True,
    )
    readme = path / 'README.md'
    readme.write_text('# test\n', encoding='utf-8')
    gitignore = path / '.gitignore'
    gitignore.write_text('.venv\n.worktrees/\n.db\n.db-*\n.status\n', encoding='utf-8')
    # project wiki -- required precondition for node init
    wiki_dir = path / wiki
    wiki_dir.mkdir(parents=True)
    wiki_index = wiki_dir / '_index.md'
    wiki_index.write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    tracked = ['README.md', '.gitignore', wiki]
    # a non-default folder is named by the project's settings file, beside a
    # corpus wiki/ whose index omits its page
    if wiki != 'wiki':
        settings = path / '.fractal' / '.settings.json'
        settings.parent.mkdir()
        settings.write_text(json.dumps({'wiki': wiki}) + '\n', encoding='utf-8')
        corpus = path / 'wiki'
        corpus.mkdir()
        index = wiki_index.read_text(encoding='utf-8')
        (corpus / '_index.md').write_text(index, encoding='utf-8')
        (corpus / 'theorem.md').write_text('# theorem\n', encoding='utf-8')
        tracked += ['.fractal/.settings.json', 'wiki']
    subprocess.run(
        ['git', 'add', *tracked],
        cwd=path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'commit', '-m', 'initial'],
        cwd=path,
        capture_output=True,
        check=True,
    )
    return path


def _parse_project_dir(output: str) -> pathlib.Path:
    """Extract the project directory from ``node init`` output."""
    for line in reversed(output.strip().split('\n')):
        if line.startswith('Initialized /'):
            return pathlib.Path(line.removeprefix('Initialized '))
    raise ValueError('No "Initialized /" line found in output.')


def _resolve_branch(project_dir: pathlib.Path) -> str:
    """Resolve the current git branch for a directory."""
    cmd = ['git', '-C', f'{project_dir}', 'rev-parse', '--abbrev-ref', 'HEAD']
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout.strip()
