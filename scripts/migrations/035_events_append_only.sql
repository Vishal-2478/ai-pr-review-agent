-- Invariant I5: the event spine is append-only. An audit trail that can be
-- edited is not an audit trail, so the rule is enforced by the database, not
-- by convention.
--
-- Triggers, not rules: Postgres rules rewrite queries, and TimescaleDB
-- hypertables do not support them. TRUNCATE needs its own statement-level
-- trigger because it is not a series of DELETEs and never fires row triggers.

CREATE OR REPLACE FUNCTION reject_agent_events_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'agent_events is append-only: % is not allowed', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

DROP TRIGGER IF EXISTS agent_events_no_update_delete ON agent_events;
CREATE TRIGGER agent_events_no_update_delete
    BEFORE UPDATE OR DELETE ON agent_events
    FOR EACH ROW EXECUTE FUNCTION reject_agent_events_mutation();

DROP TRIGGER IF EXISTS agent_events_no_truncate ON agent_events;
CREATE TRIGGER agent_events_no_truncate
    BEFORE TRUNCATE ON agent_events
    FOR EACH STATEMENT EXECUTE FUNCTION reject_agent_events_mutation();
