-- Truth lane: what was reviewed, what was found, what humans decided.
-- Plain relational tables. Idempotent: safe to run on every deploy.

CREATE TABLE IF NOT EXISTS pr_review_records (
    id                 UUID PRIMARY KEY,
    repo               TEXT NOT NULL,
    pr_number          INTEGER NOT NULL,
    head_sha           TEXT NOT NULL,
    base_sha           TEXT NOT NULL DEFAULT '',
    title              TEXT NOT NULL DEFAULT '',
    author             TEXT NOT NULL DEFAULT '',
    html_url           TEXT NOT NULL DEFAULT '',
    installation_id    BIGINT,
    delivery_id        TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'queued',
    decision           TEXT,
    overall_confidence DOUBLE PRECISION,
    coverage           DOUBLE PRECISION,
    reasons            JSONB NOT NULL DEFAULT '[]'::jsonb,
    summary            JSONB NOT NULL DEFAULT '{}'::jsonb,
    cost_usd           DOUBLE PRECISION NOT NULL DEFAULT 0,
    github_review_id   BIGINT,
    error              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at       TIMESTAMPTZ,
    -- One review per commit of a pull request: the last line of defence
    -- against a duplicate review when a webhook is redelivered.
    CONSTRAINT pr_review_one_per_commit UNIQUE (repo, pr_number, head_sha)
);
CREATE INDEX IF NOT EXISTS pr_review_created_idx ON pr_review_records (created_at DESC);
CREATE INDEX IF NOT EXISTS pr_review_status_idx ON pr_review_records (status, created_at DESC);

CREATE TABLE IF NOT EXISTS finding_records (
    review_id       UUID NOT NULL REFERENCES pr_review_records (id) ON DELETE CASCADE,
    finding_id      TEXT NOT NULL,
    repo            TEXT NOT NULL,
    pr_number       INTEGER NOT NULL,
    agent_type      TEXT NOT NULL,
    severity        TEXT NOT NULL,
    category        TEXT NOT NULL,
    summary         TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    line_start      INTEGER NOT NULL,
    line_end        INTEGER NOT NULL,
    suggestion      TEXT NOT NULL DEFAULT '',
    confidence      DOUBLE PRECISION NOT NULL,
    raw_confidence  DOUBLE PRECISION,
    rationale       TEXT NOT NULL DEFAULT '',
    evidence        TEXT NOT NULL DEFAULT '',
    grounded        BOOLEAN NOT NULL DEFAULT FALSE,
    in_diff         BOOLEAN NOT NULL DEFAULT FALSE,
    source          TEXT NOT NULL DEFAULT 'llm',
    corroborated_by JSONB NOT NULL DEFAULT '[]'::jsonb,
    adjustments     JSONB NOT NULL DEFAULT '[]'::jsonb,
    status          TEXT NOT NULL DEFAULT 'proposed',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (review_id, finding_id)
);
CREATE INDEX IF NOT EXISTS finding_repo_path_idx ON finding_records (repo, file_path, created_at DESC);
CREATE INDEX IF NOT EXISTS finding_pr_idx ON finding_records (repo, pr_number, finding_id);

CREATE TABLE IF NOT EXISTS hitl_reviews (
    id           UUID PRIMARY KEY,
    review_id    UUID NOT NULL REFERENCES pr_review_records (id) ON DELETE CASCADE,
    kind         TEXT NOT NULL DEFAULT 'approval',   -- approval | escalation | dispute
    finding_id   TEXT,                               -- set for disputes
    status       TEXT NOT NULL DEFAULT 'pending',    -- pending | approved | rejected | expired
    priority     INTEGER NOT NULL DEFAULT 0,
    reason       TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL DEFAULT '',
    reviewer     TEXT,
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS hitl_queue_idx ON hitl_reviews (status, priority DESC, created_at);
-- A review can wait for a human at most once, and a finding can be under
-- dispute at most once. Enforced here so two racing workers cannot double-queue.
CREATE UNIQUE INDEX IF NOT EXISTS hitl_one_pending_approval
    ON hitl_reviews (review_id) WHERE status = 'pending' AND kind <> 'dispute';
CREATE UNIQUE INDEX IF NOT EXISTS hitl_one_pending_dispute
    ON hitl_reviews (review_id, finding_id) WHERE status = 'pending' AND kind = 'dispute';

CREATE TABLE IF NOT EXISTS hitl_feedback (
    id         BIGSERIAL PRIMARY KEY,
    review_id  UUID NOT NULL,
    finding_id TEXT NOT NULL,
    repo       TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    category   TEXT NOT NULL,
    verdict    TEXT NOT NULL,                 -- accepted | rejected | disputed
    actor      TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'hitl',  -- hitl | dispute | api
    note       TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hitl_feedback_calibration_idx
    ON hitl_feedback (repo, agent_type, category, created_at DESC);
