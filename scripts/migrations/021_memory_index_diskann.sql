-- requires: vectorscale
-- provides: vector_index
-- optional
-- StreamingDiskANN (pgvectorscale): the graph lives on SSD, so the index
-- keeps working when the embeddings no longer fit in RAM.
CREATE INDEX IF NOT EXISTS code_chunks_embedding_idx
    ON code_chunks USING diskann (embedding vector_cosine_ops);
