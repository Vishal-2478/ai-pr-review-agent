-- requires: !vector_index
-- Fallback when DiskANN is unavailable (plain Postgres + pgvector, or 021 failed).
CREATE INDEX IF NOT EXISTS code_chunks_embedding_idx
    ON code_chunks USING hnsw (embedding vector_cosine_ops);
