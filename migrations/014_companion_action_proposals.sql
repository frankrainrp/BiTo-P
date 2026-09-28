CREATE TABLE companion_action_proposals (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    action_type TEXT NOT NULL CHECK(action_type IN ('task.create', 'task.complete', 'calendar.create')),
    payload_json TEXT NOT NULL CHECK(length(payload_json) BETWEEN 2 AND 4000),
    source TEXT NOT NULL CHECK(source IN ('model', 'deterministic')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'executing', 'confirmed', 'rejected', 'expired', 'failed')),
    result_json TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    confirmed_at TEXT,
    executed_at TEXT
);

CREATE INDEX companion_action_member_status
ON companion_action_proposals(member_id, status, created_at DESC);
