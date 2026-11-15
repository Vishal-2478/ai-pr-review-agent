-- Memory lane: semantic (code_chunks), procedural (repo_conventions) and the
-- freshness bookkeeping that keeps the semantic memory honest.
-- {{EMBEDDING_DIM}} is substituted from EMBEDDING_DIMENSIONS at migration time
-- so the column and the embedder can never silently disagree.

CREATE TABLE IF NOT EXISTS code_chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo        TEXT NOT NULL,
    path        TEXT NOT NULL,
    symbol      TEXT NOT NULL DEFAULT '',
    chunk_index INTEGER NOT NULL,
    start_line  INTEGER NOT NULL DEFAULT 1,
    end_line    INTEGER NOT NULL DEFAULT 1,
    content     TEXT NOT NULL,
    embedding   VECTOR({{EMBEDDING_DIM}}) NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 'simple' keeps identifiers intact: no stemming, no stop words.
    content_tsv TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(symbol, '') || ' ' || coalesce(path, '') || ' ' || content)
    ) STORED,
    CONSTRAINT code_chunks_upsert_key UNIQUE (repo, path, chunk_index)
);
CREATE INDEX IF NOT EXISTS code_chunks_tsv_idx ON code_chunks USING GIN (content_tsv);
CREATE INDEX IF NOT EXISTS code_chunks_repo_path_idx ON code_chunks (repo, path);

CREATE TABLE IF NOT EXISTS repo_file_index (
    repo            TEXT NOT NULL,
    path            TEXT NOT NULL,
    blob_sha        TEXT NOT NULL,
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    last_indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, path)
);

CREATE TABLE IF NOT EXISTS repo_conventions (
    id         BIGSERIAL PRIMARY KEY,
    repo       TEXT NOT NULL,
    rule       TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'manual',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT repo_conventions_unique UNIQUE (repo, rule)
);
