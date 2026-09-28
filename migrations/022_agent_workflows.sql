ALTER TABLE agent_jobs ADD COLUMN proposal_id TEXT REFERENCES companion_action_proposals(id) ON DELETE SET NULL;

CREATE TABLE agent_task_definitions (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    instructions TEXT NOT NULL,
    schedule_kind TEXT NOT NULL CHECK(schedule_kind IN ('once','daily','weekly')),
    timezone TEXT NOT NULL,
    local_time TEXT NOT NULL,
    weekdays_json TEXT NOT NULL DEFAULT '[]',
    one_time_at TEXT,
    next_run_at TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX agent_definitions_due ON agent_task_definitions(enabled,next_run_at);
CREATE TABLE agent_task_occurrences (
    id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL REFERENCES agent_task_definitions(id) ON DELETE CASCADE,
    scheduled_for TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    title TEXT NOT NULL,
    instructions TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('waiting','dispatched','missed','skipped')),
    job_id TEXT REFERENCES agent_jobs(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE(definition_id,scheduled_for)
);
CREATE INDEX agent_occurrences_waiting ON agent_task_occurrences(status,scheduled_for);
