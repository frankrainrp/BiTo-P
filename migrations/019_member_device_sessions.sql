ALTER TABLE member_sessions ADD COLUMN session_id TEXT;
ALTER TABLE member_sessions ADD COLUMN device_label TEXT NOT NULL DEFAULT 'Unknown device';
ALTER TABLE member_sessions ADD COLUMN trusted INTEGER NOT NULL DEFAULT 0 CHECK (trusted IN (0, 1));
ALTER TABLE member_sessions ADD COLUMN revoked_at INTEGER;

UPDATE member_sessions
SET session_id = lower(hex(randomblob(16)))
WHERE session_id IS NULL;

CREATE UNIQUE INDEX member_sessions_session_id_unique ON member_sessions(session_id);
CREATE INDEX member_sessions_member_activity ON member_sessions(member_id, last_seen_at DESC);

CREATE TABLE member_session_audit (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    actor_member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('created', 'trusted', 'untrusted', 'revoked', 'signed_out')),
    created_at INTEGER NOT NULL
);

CREATE INDEX member_session_audit_member_time
ON member_session_audit(member_id, created_at DESC);
