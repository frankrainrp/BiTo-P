-- A browser identity is not an authentication credential. Keep one current
-- credential per member/browser; immutable audit rows retain every login.
ALTER TABLE member_sessions ADD COLUMN browser_hash TEXT;
CREATE UNIQUE INDEX member_sessions_browser_unique
ON member_sessions(member_id, browser_hash) WHERE browser_hash IS NOT NULL;
