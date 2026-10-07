CREATE TABLE IF NOT EXISTS nodes (
    node_id         INTEGER PRIMARY KEY,
    node            TEXT    NOT NULL UNIQUE,
    title           TEXT,
    status          TEXT    NOT NULL,
    max_cost        REAL,
    max_depth       INTEGER,
    max_children    INTEGER,
    max_descendants INTEGER,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS runs (
    run_id        INTEGER PRIMARY KEY,
    node          TEXT    NOT NULL,
    parent_run_id INTEGER REFERENCES runs(run_id),
    agent         TEXT,
    max_cost      REAL,
    status        TEXT    NOT NULL,
    exit_code     INTEGER,
    metadata      TEXT    NOT NULL DEFAULT '',
    started_at    TEXT    NOT NULL,
    ended_at      TEXT
);

CREATE TABLE IF NOT EXISTS iters (
    iter_id    INTEGER PRIMARY KEY,
    node       TEXT    NOT NULL,
    run_id     INTEGER NOT NULL REFERENCES runs(run_id),
    iter       INTEGER NOT NULL,
    agent      TEXT,
    model      TEXT,
    session    TEXT,
    status     TEXT    NOT NULL,
    exit_code  INTEGER,
    metadata   TEXT    NOT NULL DEFAULT '',
    started_at TEXT    NOT NULL,
    ended_at   TEXT
);

CREATE TABLE IF NOT EXISTS steps (
    step_id    INTEGER PRIMARY KEY,
    node       TEXT    NOT NULL,
    iter_id    INTEGER NOT NULL REFERENCES iters(iter_id),
    run_id     INTEGER NOT NULL REFERENCES runs(run_id),
    step       INTEGER NOT NULL,
    step_name  TEXT    NOT NULL,
    agent      TEXT,
    model      TEXT,
    effort     TEXT,
    session    TEXT,
    status     TEXT    NOT NULL,
    exit_code  INTEGER,
    cost       REAL,
    approved   TEXT,
    metadata   TEXT    NOT NULL DEFAULT '',
    started_at TEXT    NOT NULL,
    ended_at   TEXT
);

CREATE TABLE IF NOT EXISTS events (
    event_id   INTEGER PRIMARY KEY,
    node       TEXT    NOT NULL,
    step_id    INTEGER REFERENCES steps(step_id),
    iter_id    INTEGER REFERENCES iters(iter_id),
    run_id     INTEGER REFERENCES runs(run_id),
    event      TEXT    NOT NULL,
    actor      TEXT    NOT NULL DEFAULT '',
    status     TEXT    NOT NULL,
    exit_code  INTEGER,
    metadata   TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id  INTEGER PRIMARY KEY,
    node       TEXT    NOT NULL,
    run_id     INTEGER NOT NULL REFERENCES runs(run_id),
    signal     TEXT    NOT NULL,
    metadata   TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS messages (
    message_id          INTEGER PRIMARY KEY,
    node                TEXT    NOT NULL,
    message_uuid        TEXT    NOT NULL UNIQUE,
    parent_message_id   INTEGER,
    parent_message_uuid TEXT,
    channel             TEXT    NOT NULL,
    sender              TEXT    NOT NULL,
    session             TEXT,
    priority            INTEGER NOT NULL,
    subject             TEXT    NOT NULL,
    data                TEXT    NOT NULL,
    metadata            TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS archive (
    archive_id          INTEGER PRIMARY KEY,
    node                TEXT    NOT NULL,
    message_id          INTEGER NOT NULL,
    message_uuid        TEXT    NOT NULL,
    parent_message_id   INTEGER,
    parent_message_uuid TEXT,
    channel             TEXT    NOT NULL,
    sender              TEXT    NOT NULL,
    session             TEXT,
    owner               TEXT    NOT NULL,
    priority            INTEGER NOT NULL,
    subject             TEXT    NOT NULL,
    data                TEXT    NOT NULL,
    metadata            TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(node, message_uuid)
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id INTEGER PRIMARY KEY,
    node       TEXT    NOT NULL,
    channel    TEXT    NOT NULL,
    read_only  INTEGER NOT NULL,
    write_only INTEGER NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(node, channel)
);

CREATE TABLE IF NOT EXISTS subs (
    sub_id     INTEGER PRIMARY KEY,
    node       TEXT    NOT NULL,
    target     TEXT    NOT NULL,
    channel    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(node, target, channel)
);

CREATE TABLE IF NOT EXISTS reacts (
    react_id   INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL,
    node       TEXT    NOT NULL,
    channel    TEXT    NOT NULL,
    value      INTEGER NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(message_id, node)
);

CREATE TABLE IF NOT EXISTS reads (
    read_id    INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL,
    node       TEXT    NOT NULL,
    channel    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(message_id, node)
);

-- start arms snapshot the launch instant ('active') -- an entity's
-- terminal outcome belongs to its end row alone; the run-start arm's
-- metadata labels the armed cost cap (start-time-immutable, empty when
-- uncapped)
CREATE VIEW IF NOT EXISTS activity AS
        -- run start
        SELECT
            started_at AS timestamp,
            node       AS node,
            NULL       AS event_id,
            NULL       AS step_id,
            NULL       AS iter_id,
            run_id     AS run_id,
            'start'    AS event,
            ''         AS actor,
            'active'   AS status,
            NULL       AS exit_code,
            COALESCE('max_cost $' || max_cost, '') AS metadata,
            NULL       AS duration,
            NULL       AS cost
        FROM runs
    UNION ALL
        -- run end
        SELECT
            ended_at,
            node,
            NULL,
            NULL,
            NULL,
            run_id,
            'end',
            '',
            status,
            exit_code,
            metadata,
            ROUND((julianday(ended_at) - julianday(started_at)) * 86400, 3),
            (SELECT COALESCE(SUM(s.cost), 0) FROM steps s WHERE s.run_id = runs.run_id)
        FROM runs WHERE ended_at IS NOT NULL
    UNION ALL
        -- iter start
        SELECT
            started_at,
            node,
            NULL,
            NULL,
            iter_id,
            run_id,
            'start',
            '',
            'active',
            NULL,
            '',
            NULL,
            NULL
        FROM iters
    UNION ALL
        -- iter end
        SELECT
            ended_at,
            node,
            NULL,
            NULL,
            iter_id,
            run_id,
            'end',
            '',
            status,
            exit_code,
            metadata,
            ROUND((julianday(ended_at) - julianday(started_at)) * 86400, 3),
            (SELECT COALESCE(SUM(s.cost), 0) FROM steps s WHERE s.iter_id = iters.iter_id)
        FROM iters WHERE ended_at IS NOT NULL
    UNION ALL
        -- step start
        SELECT
            started_at,
            node,
            NULL,
            step_id,
            iter_id,
            run_id,
            'start',
            '',
            'active',
            NULL,
            '',
            NULL,
            NULL
        FROM steps
    UNION ALL
        -- step end
        SELECT
            ended_at,
            node,
            NULL,
            step_id,
            iter_id,
            run_id,
            'end',
            '',
            status,
            exit_code,
            metadata,
            ROUND((julianday(ended_at) - julianday(started_at)) * 86400, 3),
            cost
        FROM steps WHERE ended_at IS NOT NULL
    UNION ALL
        -- node event
        SELECT
            created_at,
            node,
            event_id,
            step_id,
            iter_id,
            run_id,
            event,
            actor,
            status,
            exit_code,
            metadata,
            NULL,
            NULL
        FROM events;

-- indexes

CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages (parent_message_id);
CREATE INDEX IF NOT EXISTS idx_messages_node_channel ON messages (node, channel);
CREATE INDEX IF NOT EXISTS idx_runs_node ON runs (node);
CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs (parent_run_id);
CREATE INDEX IF NOT EXISTS idx_steps_run ON steps (run_id);
CREATE INDEX IF NOT EXISTS idx_steps_iter ON steps (iter_id);
