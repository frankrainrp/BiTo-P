ALTER TABLE agent_jobs ADD COLUMN execution_kind TEXT NOT NULL DEFAULT 'agent' CHECK (execution_kind IN ('agent','webhook'));
ALTER TABLE agent_jobs ADD COLUMN delivery_started_at TEXT;
ALTER TABLE agent_task_definitions ADD COLUMN execution_kind TEXT NOT NULL DEFAULT 'agent' CHECK (execution_kind IN ('agent','webhook'));
ALTER TABLE agent_task_occurrences ADD COLUMN execution_kind TEXT NOT NULL DEFAULT 'agent' CHECK (execution_kind IN ('agent','webhook'));
