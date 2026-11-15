-- requires: timescaledb
-- Hypertable: one chunk per day, so "the last hour" touches one chunk instead
-- of the whole history.
SELECT create_hypertable('agent_events', by_range('ts', INTERVAL '1 day'),
                         if_not_exists => TRUE, migrate_data => TRUE);
