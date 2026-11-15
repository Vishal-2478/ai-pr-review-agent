-- requires: timescaledb
-- provides: rollups
-- optional
-- If this file fails on your TimescaleDB version, the migration runner logs the
-- error and applies 033 instead (plain views with the same names and columns):
-- slower, but correct. `pr-review doctor` reports which one is active.
--
-- Rollups: summary tables TimescaleDB keeps current for us. The dashboard and
-- the BudgetGuard read these, never the raw events.
-- materialized_only = false turns on real-time aggregation: the newest,
-- not-yet-materialized minute is included, so the budget cap is not a minute late.
CREATE MATERIALIZED VIEW IF NOT EXISTS agent_health_1m
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket(INTERVAL '1 minute', ts)                                   AS bucket,
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
GROUP BY bucket, agent
WITH NO DATA;

SELECT add_continuous_aggregate_policy('agent_health_1m',
    start_offset      => INTERVAL '2 hours',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists     => TRUE);

CREATE MATERIALIZED VIEW IF NOT EXISTS pr_cost_hourly
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket(INTERVAL '1 hour', ts)                                     AS bucket,
       review_id,
       sum(cost_usd)                                                          AS cost_usd,
       sum(tokens_in)                                                         AS tokens_in,
       sum(tokens_out)                                                        AS tokens_out,
       count(DISTINCT agent) FILTER (WHERE event_type = 'llm.call')           AS agents_used,
       max(confidence)                                                        AS max_confidence
FROM agent_events
WHERE review_id IS NOT NULL
GROUP BY bucket, review_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('pr_cost_hourly',
    start_offset      => INTERVAL '1 day',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '15 minutes',
    if_not_exists     => TRUE);
