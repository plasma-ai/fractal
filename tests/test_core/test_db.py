"""Test the ``fractal.core.db`` module."""

from __future__ import annotations

import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import threading

import pytest

from fractal.core.db import Database

__all__ = [
    'test_schema_tables_exist',
    'test_init_stamps_schema_version',
    'test_connect_lifts_a_version_one_database',
    'test_two_writers_lift_the_same_database_once',
    'test_failed_lift_leaves_the_database_at_its_version',
    'test_activity_start_rows_snapshot_the_launch',
    'test_init_is_idempotent_on_a_populated_db',
    'test_crud_workflow_round_trips_rows',
    'test_merge_inserts_replaces_and_partial_upserts',
    'test_read_executes_raw_sql_queries',
    'test_read_rejects_invalid_argument_combinations',
    'test_read_orders_by_write_order',
    'test_where_none_filters_match_null_columns',
    'test_connect_sets_generous_busy_timeout',
    'test_connect_timeout_override_wins',
    'test_wal_sidecars_persist_across_last_close',
    'test_checkpoint_truncates_wal_and_keeps_the_db_complete',
    'test_concurrent_writers_serialize',
    'test_transaction_commits_and_rolls_back',
    'test_update_reports_rowcount_for_compare_and_swap',
]

# runs.started_at is NOT NULL with no SQL default (the Python writers supply it)
# and every row table carries its owning node, so direct db.write() inserts in
# these generic tests go through a minimal-valid-row helper
_STARTED = '2026-01-01T00:00:00.000Z'
_NODE = 'main'


def _run(**extra: object) -> dict:
    """A minimal valid ``runs`` row (override or extend via ``extra``)."""
    return {'node': _NODE, 'status': 'active', 'started_at': _STARTED, **extra}


# ------ schema and init


def test_schema_tables_exist(database: Database) -> None:
    """All expected tables and indexes are created by ``init()``."""
    tables = database.read(
        query="SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
    )
    names = [row['name'] for row in tables]
    assert 'channels' in names
    assert 'events' in names
    assert 'iters' in names
    assert 'messages' in names
    assert 'nodes' in names
    assert 'reads' in names
    assert 'reacts' in names
    assert 'runs' in names
    assert 'signals' in names
    assert 'steps' in names
    assert 'subs' in names
    # the hot-lookup indexes land with the tables (one idempotent init)
    indexes = database.read(
        query="SELECT name FROM sqlite_master WHERE type='index' ORDER BY name",
    )
    index_names = [row['name'] for row in indexes]
    assert 'idx_messages_parent' in index_names
    assert 'idx_messages_node_channel' in index_names
    assert 'idx_runs_node' in index_names
    assert 'idx_runs_parent' in index_names
    assert 'idx_steps_run' in index_names
    assert 'idx_steps_iter' in index_names


def test_init_stamps_schema_version(database: Database) -> None:
    """A fresh database carries the current schema version.

    ``init`` stamps ``PRAGMA user_version`` so the version step on connect
    can tell an older database from a current one.
    """
    assert _version(database) == 2


def test_connect_lifts_a_version_one_database(database: Database) -> None:
    """A database stamped at version 1 gains the step effort column on connect.

    The lift happens on the first handle of either mode, a second handle
    finds nothing to do, and a column already present (a database altered
    out of band) is stamped past rather than refused.
    """
    _downgrade(database)
    assert 'effort' not in _step_columns(database)
    # a read-only handle lifts it like a writable one
    database.connect(read_only=True).close()
    assert _version(database) == 2
    assert 'effort' in _step_columns(database)
    # a lifted database is left alone
    database.connect().close()
    assert _version(database) == 2
    # a column added out of band is stamped past
    _downgrade(database)
    connection = database.connect(read_only=True)
    connection.close()
    assert _version(database) == 2
    connection = sqlite3.connect(f'{database.path}')
    connection.execute('PRAGMA user_version = 1')
    connection.commit()
    connection.close()
    database.connect().close()
    assert _version(database) == 2
    assert [name for name in _step_columns(database) if name == 'effort'] == ['effort']


def test_two_writers_lift_the_same_database_once(database: Database) -> None:
    """Two processes opening a version-1 database at once both succeed.

    The step runs inside an immediate transaction, so the writers serialize
    on the lock and the second re-reads the version under it: one column,
    one stamp, no error from either side.
    """
    _downgrade(database)
    code = (
        'import sys\n'
        'from fractal.core.db import Database\n'
        'Database(sys.argv[1], sys.argv[2]).connect().close()\n'
    )
    command = [sys.executable, '-c', code, f'{database.path}', f'{database._schema}']
    writers = [
        subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(2)
    ]
    outcomes = [writer.communicate(timeout=60) for writer in writers]
    assert [writer.returncode for writer in writers] == [0, 0], outcomes
    assert _version(database) == 2
    assert [name for name in _step_columns(database) if name == 'effort'] == ['effort']


def test_failed_lift_leaves_the_database_at_its_version(
    database: Database,
) -> None:
    """A step that cannot run leaves the database readable at its old version.

    A writable handle reports the failure; a read-only handle reads the
    schema it finds. Neither half-stamps: the version stays 1 until a
    later handle lifts it.
    """
    run_id = database.write(_run(), 'runs')
    _downgrade(database)
    # a read-only file refuses the write the lift needs
    os.chmod(database.path, 0o444)
    try:
        with pytest.raises(sqlite3.OperationalError):
            database.connect().close()
        assert _version(database) == 1
        assert 'effort' not in _step_columns(database)
        # a read-only handle reads at version 1 rather than failing
        assert [row['run_id'] for row in database.read('runs')] == [run_id]
        assert _version(database) == 1
    finally:
        # sidecars SQLite recreates while the file is read-only inherit its
        # mode, so they are restored with it
        for suffix in ('', '-wal', '-shm'):
            sidecar = database.path.with_name(database.path.name + suffix)
            if sidecar.exists():
                os.chmod(sidecar, 0o644)
    # the next writable handle lifts it
    database.connect().close()
    assert _version(database) == 2


def _version(database: Database) -> int:
    """Return the stamped schema version, read without lifting it."""
    connection = sqlite3.connect(f'file:{database.path}?mode=ro', uri=True)
    try:
        version, *_ = connection.execute('PRAGMA user_version').fetchone()
    finally:
        connection.close()
    return version


def _step_columns(database: Database) -> list[str]:
    """Return the ``steps`` table's column names, read without lifting."""
    connection = sqlite3.connect(f'file:{database.path}?mode=ro', uri=True)
    try:
        return [row[1] for row in connection.execute('PRAGMA table_info(steps)')]
    finally:
        connection.close()


def _downgrade(database: Database) -> None:
    """Put a current database back at version 1 without the effort column."""
    connection = sqlite3.connect(f'{database.path}')
    try:
        connection.execute('ALTER TABLE steps DROP COLUMN effort')
        connection.execute('PRAGMA user_version = 1')
        connection.commit()
    finally:
        connection.close()


def test_activity_start_rows_snapshot_the_launch(database: Database) -> None:
    """Start rows read the launch instant; the outcome stays on the end rows.

    The view synthesizes an entity's start and end rows from the same
    table row, so a terminal status or reason must never bleed backwards
    into history -- start rows always read ``active`` with bare metadata.
    Events project their stored ``actor``; synthesized entity rows carry
    an empty one.
    """
    # one settled lineage, every level ended abnormally with a reason
    ended = '2026-01-01T00:01:30.000Z'
    run_id = database.write(
        data=_run(status='exited', metadata='Timed out', ended_at=ended),
        table='runs',
    )
    iter_id = database.write(
        data={
            'node': _NODE,
            'run_id': run_id,
            'iter': 1,
            'status': 'killed',
            'metadata': 'killed by operator',
            'started_at': _STARTED,
            'ended_at': ended,
        },
        table='iters',
    )
    database.write(
        data={
            'node': _NODE,
            'iter_id': iter_id,
            'run_id': run_id,
            'step': 1,
            'step_name': 'EXECUTE',
            'status': 'failed',
            'metadata': 'agent error',
            'started_at': _STARTED,
            'ended_at': ended,
        },
        table='steps',
    )
    database.write(
        data={
            'node': _NODE,
            'event': 'kill',
            'status': 'completed',
            'actor': 'operator',
        },
        table='events',
    )

    rows = database.read(
        query='SELECT * FROM activity WHERE node = ?',
        params=(_NODE,),
    )
    # every start row reads as the launch instant, whatever the row's fate
    starts = [row for row in rows if row['event'] == 'start']
    assert len(starts) == 3
    assert all((row['status'], row['metadata']) == ('active', '') for row in starts)
    # the terminal status and reason stay on the end rows
    ends = {(row['status'], row['metadata']) for row in rows if row['event'] == 'end'}
    assert ends == {
        ('exited', 'Timed out'),
        ('killed', 'killed by operator'),
        ('failed', 'agent error'),
    }
    # the event carries its stored actor; the synthesized rows an empty one
    [kill] = [row for row in rows if row['event'] == 'kill']
    assert kill['actor'] == 'operator'
    assert all(row['actor'] == '' for row in starts)


def test_init_is_idempotent_on_a_populated_db(
    tmp_path: pathlib.Path,
    schema: pathlib.Path,
) -> None:
    """Re-running ``init`` on an already-populated database is a safe no-op.

    ``init`` is the documented idempotent recovery for a stranded tree, so a
    second call on a database that already holds rows must not raise and must
    leave the existing data intact -- ``CREATE TABLE IF NOT EXISTS`` skips the
    tables and the rows survive.
    """
    path = tmp_path / '.db'
    db = Database(path, schema)
    db.init()
    db.write(_run(metadata='seed'), 'runs')

    # the idempotent recovery re-runs init without error and keeps the row
    db.init()
    rows = db.read('runs', where={'node': _NODE})
    assert len(rows) == 1
    assert rows[0]['metadata'] == 'seed'


# ------ reads and writes


def test_crud_workflow_round_trips_rows(database: Database) -> None:
    """Write, read, update, exists, count, and delete in sequence."""
    # write rows
    id_1 = database.write(_run(metadata='first'), 'runs')
    id_2 = database.write(_run(metadata='second'), 'runs')
    assert id_1 != id_2

    # read all rows (most-recently-written first)
    rows = database.read('runs')
    assert len(rows) == 2
    assert rows[0]['run_id'] == id_2

    # read with where filter
    rows = database.read('runs', where={'run_id': id_1})
    assert len(rows) == 1
    assert rows[0]['metadata'] == 'first'

    # read with limit
    rows = database.read('runs', limit=1)
    assert len(rows) == 1

    # update
    database.update({'status': 'completed'}, 'runs', where={'run_id': id_1})
    rows = database.read('runs', where={'run_id': id_1})
    assert rows[0]['status'] == 'completed'

    # exists
    assert database.exists('runs', where={'run_id': id_1})
    assert not database.exists('runs', where={'run_id': 999})

    # count
    assert database.count('runs') == 2
    assert database.count('runs', where={'status': 'active'}) == 1

    # delete
    database.delete('runs', where={'run_id': id_1})
    assert database.count('runs') == 1
    assert not database.exists('runs', where={'run_id': id_1})


def test_merge_inserts_replaces_and_partial_upserts(database: Database) -> None:
    """Merge inserts, replaces, partial-upserts, returns the id, and no-ops."""
    # insert via merge
    database.merge({'node': 'task.a', 'status': 'idle', 'max_depth': 2}, 'nodes')
    rows = database.read('nodes', where={'node': 'task.a'})
    assert len(rows) == 1
    assert rows[0]['max_depth'] == 2

    # replace via merge (same unique key)
    database.merge({'node': 'task.a', 'status': 'idle', 'max_depth': 5}, 'nodes')
    rows = database.read('nodes', where={'node': 'task.a'})
    assert len(rows) == 1
    assert rows[0]['max_depth'] == 5

    # conflict upsert: a partial merge updates only the given columns and
    # preserves the rest (a whole-row replace would have wiped max_depth)
    database.merge({'node': 'task.a', 'status': 'active'}, 'nodes', conflict=['node'])
    rows = database.read('nodes', where={'node': 'task.a'})
    assert len(rows) == 1
    assert rows[0]['status'] == 'active'
    assert rows[0]['max_depth'] == 5

    # the conflict upsert returns the affected row's real id (not 0: lastrowid is
    # 0 on the ON CONFLICT update path because no INSERT happens)
    node_id = rows[0]['node_id']
    returned = database.merge(
        data={'node': 'task.a', 'status': 'idle'},
        table='nodes',
        conflict=['node'],
    )
    assert returned == node_id

    # a conflict-only merge (no non-conflict columns) is a no-op, not a crash --
    # ON CONFLICT DO NOTHING would trip the NOT NULL on nodes.status first
    again = database.merge({'node': 'task.a'}, 'nodes', conflict=['node'])
    assert again == node_id
    rows = database.read('nodes', where={'node': 'task.a'})
    assert rows[0]['status'] == 'idle'  # unchanged, no exception raised


def test_read_executes_raw_sql_queries(database: Database) -> None:
    """Read with raw SQL query returns expected results."""
    database.write(_run(), 'runs')
    database.write(_run(status='completed'), 'runs')
    rows = database.read(
        query="SELECT COUNT(*) AS n FROM runs WHERE status = 'active'",
    )
    assert rows[0]['n'] == 1


def test_read_rejects_invalid_argument_combinations(database: Database) -> None:
    """Read raises ``ValueError`` for invalid argument combinations."""
    # query + where is invalid
    with pytest.raises(ValueError):
        database.read('runs', query='SELECT 1', where={'status': 'active'})

    # no table and no query is invalid
    with pytest.raises(ValueError):
        database.read()


def test_read_orders_by_write_order(database: Database) -> None:
    """Built SELECTs return rows most-recently-written first (``rowid DESC``).

    The order is the true insertion order (a monotonic alias of the INTEGER
    PRIMARY KEY), so it holds without any ``created_at`` column and is stable
    regardless of timestamp values.
    """
    # write three rows sharing one start timestamp -- order must still be stable
    id_1 = database.write(_run(), 'runs')
    id_2 = database.write(_run(), 'runs')
    id_3 = database.write(_run(), 'runs')

    # newest first, descending by write order
    rows = database.read('runs')
    assert [row['run_id'] for row in rows] == [id_3, id_2, id_1]
    assert database.read('runs', limit=1)[0]['run_id'] == id_3


def test_where_none_filters_match_null_columns(database: Database) -> None:
    """``where`` filters with ``None`` match SQL NULL via ``IS NULL``.

    A row written without ``exit_code`` stores NULL; filtering by
    ``exit_code=None`` must find it (``col IS NULL``) rather than silently
    matching nothing (``col = NULL`` is never true in SQL).
    """
    # write one row with a NULL exit_code and one with a value
    null_id = database.write(_run(), 'runs')
    set_id = database.write(_run(exit_code=0), 'runs')

    # read/count/exists with a None filter match only the NULL row
    rows = database.read('runs', where={'exit_code': None})
    assert [row['run_id'] for row in rows] == [null_id]
    assert database.count('runs', where={'exit_code': None}) == 1
    assert database.exists('runs', where={'exit_code': None})

    # a None filter AND-joins with equality filters
    rows = database.read('runs', where={'status': 'active', 'exit_code': None})
    assert [row['run_id'] for row in rows] == [null_id]

    # update and delete also target NULL rows through the None filter
    database.update({'status': 'completed'}, 'runs', where={'exit_code': None})
    assert database.read('runs', where={'run_id': null_id})[0]['status'] == 'completed'
    database.delete('runs', where={'exit_code': None})
    assert [row['run_id'] for row in database.read('runs')] == [set_id]


# ------ connection settings


def test_connect_sets_generous_busy_timeout(database: Database) -> None:
    """Every handle carries the 30s busy timeout (raised from the 5s default).

    Under wide node fan-out the fleet contends on one DB; a generous busy timeout
    makes a writer wait for the lock rather than failing fast and aborting the
    loop. ``PRAGMA busy_timeout`` reports it in milliseconds.
    """
    for read_only in (False, True):
        connection = database.connect(read_only=read_only)
        try:
            timeout_ms = connection.execute('PRAGMA busy_timeout').fetchone()[0]
        finally:
            connection.close()
        assert timeout_ms == 30000


def test_connect_timeout_override_wins(database: Database) -> None:
    """An explicit ``timeout=`` overrides the 30s default on any handle.

    A UI-thread reader passes a short timeout so a busy writer never stalls a
    refresh. ``PRAGMA busy_timeout`` reports it in milliseconds.
    """
    for read_only in (False, True):
        connection = database.connect(read_only=read_only, timeout=2.0)
        try:
            timeout_ms = connection.execute('PRAGMA busy_timeout').fetchone()[0]
        finally:
            connection.close()
        assert timeout_ms == 2000


def test_wal_sidecars_persist_across_last_close(database: Database) -> None:
    """The WAL sidecars survive every last close, keeping readers attachable.

    Stock SQLite checkpoints and unlinks ``-wal``/``-shm`` on a database's
    last close; a write-denied (sandboxed) reader landing in that gap gets
    ``SQLITE_CANTOPEN`` until a writer rebuilds the wal-index. Writable
    handles disable the close-time checkpoint, so the sidecars created at
    init persist -- and the mode comes from the file header, never a
    per-connect re-stamp.
    """
    # a one-shot write whose close is the process's last connection
    database.write(_run(), 'runs')
    wal = database.path.with_name(f'{database.path.name}-wal')
    shm = database.path.with_name(f'{database.path.name}-shm')
    assert wal.exists()
    assert shm.exists()
    # a read-only one-shot attaches to the stable wal-index and sees the row
    connection = database.connect(read_only=True)
    try:
        count = connection.execute('SELECT COUNT(*) AS n FROM runs').fetchone()['n']
    finally:
        connection.close()
    assert count == 1
    # the mode is inherited pragma-free on a fresh writable handle
    connection = database.connect()
    try:
        mode, *_ = connection.execute('PRAGMA journal_mode').fetchone()
    finally:
        connection.close()
    assert mode == 'wal'


def test_checkpoint_truncates_wal_and_keeps_the_db_complete(
    database: Database,
) -> None:
    """``checkpoint`` backfills and zeroes the WAL, sidecars left in place.

    Close-time checkpoints are disabled, so recent commits live only in
    the ``-wal`` until an explicit consolidation -- after it, the bare
    ``.db`` alone is a complete database at rest while the sidecars stay
    put for write-denied readers.
    """
    database.write(_run(), 'runs')
    wal = database.path.with_name(f'{database.path.name}-wal')
    assert wal.stat().st_size > 0
    database.checkpoint()
    assert wal.exists()
    assert wal.stat().st_size == 0
    # the bare .db alone is now the complete database
    bare = database.path.parent / 'bare.db'
    shutil.copyfile(database.path, bare)
    connection = sqlite3.connect(f'file:{bare}?mode=ro&immutable=1', uri=True)
    try:
        count, *_ = connection.execute('SELECT COUNT(*) FROM runs').fetchone()
    finally:
        connection.close()
    assert count == 1


# ------ concurrency and transactions


def test_concurrent_writers_serialize(database: Database) -> None:
    """Concurrent writers to one DB all succeed rather than failing on a lock.

    SQLite (WAL) admits one writer at a time; the connection's busy timeout makes
    a contending writer wait for the lock instead of failing with "database is
    locked". Every running node writes to the one central DB, so this pins the
    contract: N threads each writing M rows leave exactly N*M rows, with no
    errors.
    """
    # each of N threads writes M rows, recording any error it hits
    writers = 6
    per_writer = 20
    errors: list[Exception] = []

    def writer(tag: int) -> None:
        try:
            for index in range(per_writer):
                database.write(_run(metadata=f'{tag}:{index}'), 'runs')
        except Exception as e:
            errors.append(e)

    # run the writers concurrently against the shared DB
    threads = [threading.Thread(target=writer, args=(tag,)) for tag in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # every write lands: no lock errors, exactly N*M rows
    assert errors == [], errors
    assert database.count('runs') == writers * per_writer


def test_transaction_commits_and_rolls_back(database: Database) -> None:
    """A transaction's writes land together on exit and vanish on error.

    ``transaction()`` yields the connection every table operation joins via
    ``connection=``: reads inside the block see the block's uncommitted
    writes, a clean exit commits them all at once, and an exception rolls
    the whole block back -- the atomicity a multi-statement lifecycle
    transition (read, decide, write) is built on.
    """
    # a clean block commits, and in-transaction reads see uncommitted
    # writes -- every table operation joins through connection=
    with database.transaction() as connection:
        run_id = database.write(_run(), 'runs', connection=connection)
        database.update(
            data={'status': 'completed'},
            table='runs',
            where={'run_id': run_id},
            connection=connection,
        )
        inside = database.read(
            'runs',
            where={'run_id': run_id},
            connection=connection,
        )
        assert inside[0]['status'] == 'completed'
        assert database.exists('runs', where={'run_id': run_id}, connection=connection)
        assert database.count('runs', connection=connection) == 1
        node_id = database.merge(
            data={'node': 'task.a', 'status': 'idle'},
            table='nodes',
            conflict=['node'],
            connection=connection,
        )
        assert node_id
        database.delete('nodes', where={'node': 'task.a'}, connection=connection)
        assert not database.exists(
            'nodes',
            where={'node': 'task.a'},
            connection=connection,
        )
    assert database.read('runs', where={'run_id': run_id})[0]['status'] == 'completed'

    # an exception rolls the whole block back and re-raises
    def fail_mid_transaction() -> None:
        with database.transaction() as connection:
            database.write(_run(node='rollback'), 'runs', connection=connection)
            raise RuntimeError('boom')

    with pytest.raises(RuntimeError, match='boom'):
        fail_mid_transaction()
    assert not database.exists('runs', where={'node': 'rollback'})


def test_update_reports_rowcount_for_compare_and_swap(database: Database) -> None:
    """``update`` returns the matched-row count -- the observable CAS verdict.

    A fenced transition writes through a guarded ``where`` (the run closers
    guard on ``ended_at IS NULL``): rowcount 1 means this writer won the
    swap, 0 means another writer got there first and the loser observes
    instead of overwriting.
    """
    run_id = database.write(_run(), 'runs')
    # the first closer wins the swap
    won = database.update(
        data={'status': 'completed', 'ended_at': _STARTED},
        table='runs',
        where={'run_id': run_id, 'ended_at': None},
    )
    assert won == 1
    # a competing closer on the same guard observes the loss
    lost = database.update(
        data={'status': 'killed', 'ended_at': _STARTED},
        table='runs',
        where={'run_id': run_id, 'ended_at': None},
    )
    assert lost == 0
    assert database.read('runs', where={'run_id': run_id})[0]['status'] == 'completed'
