-- requires: !rollups
-- Fallback when continuous aggregates are unavailable (plain Postgres, or 032 failed): the same two rollups as ordinary views, so
-- application code reads identical names and columns in every environment.
-- Fine for development; on real volume use TimescaleDB (031).
CREATE OR REPLACE VIEW agent_health_1m AS
SELECT date_trunc('minute', ts)                                               AS bucket,
       agent,
       count(*) FILTER (WHERE event_type = 'llm.call')                        AS llm_calls,
       sum(cost_usd)                                                          AS cost_usd,
       sum(tokens_in)                                                         AS tokens_in,
       sum(tokens_out)                                                        AS tokens_out,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
           FILTER (WHERE event_type = 'llm.call')                             AS p95_latency_ms,
       count(*) FILTER (WHERE event_type = 'feedback')                        AS feedback_total,
       count(*) FILTER (WHERE event_type = 'feedback'
                          AND outcome IN ('rejected', 'disputed'))            AS feedback_negative,
       count(*) FILTER (WHERE event_type = 'error')                           AS errors
FROM agent_events
GROUP BY 1, 2;

CREATE OR REPLACE VIEW pr_cost_hourly AS
SELECT date_trunc('hour', ts)                                                 AS bucket,
       review_id,
       sum(cost_usd)                                                          AS cost_usd,
       sum(tokens_in)                                                         AS tokens_in,
       sum(tokens_out)                                                        AS tokens_out,
       count(DISTINCT agent) FILTER (WHERE event_type = 'llm.call')           AS agents_used,
       max(confidence)                                                        AS max_confidence
FROM agent_events
WHERE review_id IS NOT NULL
GROUP BY 1, 2;
