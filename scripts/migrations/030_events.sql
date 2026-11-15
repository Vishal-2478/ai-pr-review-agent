-- Time lane: the event spine. Every span, model call, tool call, decision and
-- escalation is one row, in time order. This single table powers the trace
-- viewer, the audit trail and the cost dashboard.

CREATE TABLE IF NOT EXISTS agent_events (
    ts          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    review_id   UUID,
    agent       TEXT NOT NULL DEFAULT 'system',
    span_id     TEXT NOT NULL DEFAULT '',
    parent_span TEXT,
    event_type  TEXT NOT NULL,   -- span.start | span.end | llm.call | tool.call | decision | escalation | feedback | error | audit
    name        TEXT NOT NULL DEFAULT '',
    model       TEXT,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    cost_usd    DOUBLE PRECISION NOT NULL DEFAULT 0,
    latency_ms  INTEGER,
    outcome     TEXT,
    confidence  DOUBLE PRECISION,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS agent_events_review_idx ON agent_events (review_id, ts);
CREATE INDEX IF NOT EXISTS agent_events_type_idx ON agent_events (event_type, ts DESC);
