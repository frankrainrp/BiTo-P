ALTER TABLE members ADD COLUMN username TEXT;
ALTER TABLE members ADD COLUMN username_key TEXT;

UPDATE members
SET username = 'member-' || substr(replace(id, '-', ''), 1, 8),
    username_key = 'member-' || substr(replace(id, '-', ''), 1, 8)
WHERE username IS NULL;

CREATE UNIQUE INDEX members_username_key_unique ON members(username_key);

CREATE TABLE member_sessions (
    token_hash TEXT PRIMARY KEY,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL
);

CREATE INDEX member_sessions_expiry ON member_sessions(expires_at);

