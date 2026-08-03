CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    occurred_at REAL NOT NULL,
    received_at REAL NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '',
    processed INTEGER NOT NULL DEFAULT 0 CHECK (processed IN (0, 1))
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    source_event_ids_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    text TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    possible_duplicate INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    kind TEXT NOT NULL DEFAULT 'text',
    media_path TEXT,
    payload_json TEXT NOT NULL DEFAULT '',
    reply_expectation TEXT NOT NULL DEFAULT '',
    target_channel TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS continuity_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    content TEXT NOT NULL,
    source_event_ids_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_message_id INTEGER NOT NULL,
    end_message_id INTEGER NOT NULL UNIQUE,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reflections (
    id TEXT PRIMARY KEY,
    local_date TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'completed')),
    scheduled_at REAL NOT NULL,
    retry_at REAL,
    claimed_at REAL,
    summary TEXT NOT NULL DEFAULT '',
    memories_json TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    created_at REAL NOT NULL,
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS reflections_due
    ON reflections(scheduled_at, retry_at) WHERE state='pending';
CREATE TABLE IF NOT EXISTS reflection_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    content TEXT NOT NULL,
    evidence TEXT NOT NULL,
    confidence REAL NOT NULL,
    source_reflection_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(kind, key),
    FOREIGN KEY (source_reflection_id) REFERENCES reflections(id)
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    content TEXT NOT NULL,
    authority TEXT NOT NULL CHECK (authority = 'owner'),
    source_event_id TEXT NOT NULL,
    evidence_quote TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL,
    superseded_by INTEGER
);
CREATE INDEX IF NOT EXISTS memories_active
    ON memories(kind, key) WHERE superseded_by IS NULL;
CREATE TABLE IF NOT EXISTS memory_tombstones (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    evidence_quote TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS memory_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL,
    source_event_id TEXT NOT NULL,
    quote TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(memory_id, source_event_id, quote)
);
CREATE TABLE IF NOT EXISTS memory_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    existing_memory_id INTEGER NOT NULL,
    candidate_content TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    evidence_quote TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    status TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
    resolution TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS memory_conflicts_open
    ON memory_conflicts(kind, key) WHERE status='open';
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    success_criteria TEXT NOT NULL,
    authority TEXT NOT NULL CHECK (authority IN ('owner', 'agent')),
    source_event_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('active', 'waiting', 'blocked', 'done', 'cancelled')
    ),
    plan_json TEXT NOT NULL,
    next_action TEXT NOT NULL DEFAULT '',
    waiting_for TEXT NOT NULL DEFAULT '',
    blocked_reason TEXT NOT NULL DEFAULT '',
    latest_result TEXT NOT NULL DEFAULT '',
    schedule_json TEXT NOT NULL DEFAULT '',
    next_review_at REAL,
    retry_at REAL,
    failure_count INTEGER NOT NULL DEFAULT 0,
    review_claimed_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS goals_due
    ON goals(next_review_at) WHERE status IN ('active', 'waiting');
CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'fired', 'cancelled')),
    fire_at REAL NOT NULL,
    schedule_json TEXT NOT NULL DEFAULT '',
    claimed_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS reminders_due
    ON reminders(fire_at) WHERE status='pending';
CREATE TABLE IF NOT EXISTS self_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mood_state TEXT NOT NULL,
    mood_intensity REAL NOT NULL,
    mood_cause TEXT NOT NULL,
    mood_updated_at REAL NOT NULL,
    mood_settle_at REAL,
    activity TEXT NOT NULL,
    activity_result TEXT NOT NULL DEFAULT '',
    activity_since REAL NOT NULL,
    last_heartbeat_at REAL,
    next_heartbeat_at REAL NOT NULL DEFAULT 0,
    heartbeat_claimed_at REAL,
    pending_reply_turn_id TEXT,
    pending_reply_expectation TEXT NOT NULL DEFAULT '',
    pending_reply_since REAL,
    pending_reply_checks INTEGER NOT NULL DEFAULT 0,
    pending_reply_channel TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL UNIQUE,
    goal_id TEXT NOT NULL,
    notification_key TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('normal', 'urgent')),
    reason TEXT NOT NULL,
    messages_json TEXT NOT NULL,
    reply_expectation TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL CHECK (state IN ('pending', 'queued')),
    not_before REAL NOT NULL,
    claimed_at REAL,
    created_at REAL NOT NULL,
    queued_at REAL,
    target_channel TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS notifications_due
    ON notifications(not_before) WHERE state='pending';
CREATE TABLE IF NOT EXISTS emotions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    capability TEXT NOT NULL DEFAULT 'external_effect',
    arguments_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('dispatching', 'completed')),
    result_json TEXT,
    ok INTEGER,
    started_at REAL NOT NULL,
    completed_at REAL,
    UNIQUE(turn_id, tool_call_id)
);
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('owner', 'autonomous')),
    source_ids_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('running', 'needs_reconciliation', 'completed', 'cancelled')
    ),
    external_effect_started INTEGER NOT NULL DEFAULT 0
        CHECK (external_effect_started IN (0, 1)),
    stage TEXT NOT NULL DEFAULT 'started',
    failure_reason TEXT,
    llm_calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_episodes (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'closing', 'closed')),
    title TEXT NOT NULL,
    working_summary TEXT NOT NULL DEFAULT '',
    summarized_through_ordinal INTEGER NOT NULL DEFAULT 0
        CHECK (summarized_through_ordinal >= 0),
    summary TEXT NOT NULL DEFAULT '',
    topics_json TEXT NOT NULL DEFAULT '[]',
    entities_json TEXT NOT NULL DEFAULT '[]',
    open_loops_json TEXT NOT NULL DEFAULT '[]',
    salience REAL NOT NULL DEFAULT 0.5 CHECK (salience BETWEEN 0 AND 1),
    summary_claimed_at REAL,
    summary_retry_at REAL,
    summary_failure_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    closed_at REAL
);
CREATE INDEX IF NOT EXISTS conversation_episodes_candidates
    ON conversation_episodes(status, salience DESC, updated_at DESC);
CREATE TABLE IF NOT EXISTS episode_turns (
    episode_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    relation TEXT NOT NULL CHECK (relation IN ('primary', 'related')),
    unit_ids_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (episode_id, turn_id),
    UNIQUE (episode_id, ordinal),
    FOREIGN KEY (episode_id) REFERENCES conversation_episodes(id) ON DELETE CASCADE,
    FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS episode_turns_by_turn
    ON episode_turns(turn_id, relation);
CREATE TABLE IF NOT EXISTS episode_links (
    from_episode_id TEXT NOT NULL,
    to_episode_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('continues', 'references', 'supersedes')),
    PRIMARY KEY (from_episode_id, to_episode_id, kind),
    CHECK (from_episode_id <> to_episode_id),
    FOREIGN KEY (from_episode_id) REFERENCES conversation_episodes(id)
        ON DELETE CASCADE,
    FOREIGN KEY (to_episode_id) REFERENCES conversation_episodes(id)
        ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS context_plans (
    turn_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    source_event_ids_json TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    retrieval_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'planned'
        CHECK (state IN ('planned', 'recalled', 'superseded', 'degraded')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (turn_id, revision),
    FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS context_plans_latest
    ON context_plans(turn_id, revision DESC);
CREATE TABLE IF NOT EXISTS reconciliations (
    turn_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('open', 'resolved', 'resumed')),
    reason TEXT NOT NULL,
    resolution TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turn_progress (
    turn_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    part_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (turn_id, tool_call_id, part_index)
);
CREATE TABLE IF NOT EXISTS webhook_runs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    idempotency_key TEXT,
    plan_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'waiting_delivery',
                  'succeeded', 'failed', 'ambiguous')
    ),
    current_step INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(workflow_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS webhook_runs_ready
    ON webhook_runs(state, created_at);
CREATE TABLE IF NOT EXISTS webhook_steps (
    run_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    step_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('message', 'exec')),
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'waiting_delivery',
                  'succeeded', 'failed', 'ambiguous')
    ),
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    started_at REAL,
    completed_at REAL,
    PRIMARY KEY (run_id, step_index),
    FOREIGN KEY (run_id) REFERENCES webhook_runs(id) ON DELETE CASCADE
);
