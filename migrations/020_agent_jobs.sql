CREATE TABLE agent_jobs (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    agent_key TEXT NOT NULL DEFAULT 'bito' CHECK(agent_key = 'bito'),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 120),
    instructions TEXT NOT NULL CHECK(length(instructions) BETWEEN 1 AND 2000),
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'needs_attention')),
    stage TEXT NOT NULL DEFAULT 'queued' CHECK(length(stage) BETWEEN 1 AND 80),
    progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
    result_text TEXT CHECK(result_text IS NULL OR length(result_text) <= 4000),
    error_code TEXT CHECK(error_code IS NULL OR length(error_code) <= 80),
    conversation_id TEXT REFERENCES companion_conversations(id) ON DELETE SET NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0, 1)),
    takeover_requested INTEGER NOT NULL DEFAULT 0 CHECK(takeover_requested IN (0, 1)),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX agent_jobs_member_created
ON agent_jobs(member_id, household_id, created_at DESC);

CREATE INDEX agent_jobs_status_created
ON agent_jobs(status, created_at);

CREATE UNIQUE INDEX agent_jobs_one_active_per_member
ON agent_jobs(member_id)
WHERE status IN ('queued', 'running');

CREATE TABLE agent_job_events (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK(length(event_type) BETWEEN 1 AND 80),
    message TEXT NOT NULL CHECK(length(message) BETWEEN 1 AND 400),
    progress INTEGER NOT NULL CHECK(progress BETWEEN 0 AND 100),
    created_at TEXT NOT NULL
);

CREATE INDEX agent_job_events_job_created
ON agent_job_events(job_id, created_at, id);
