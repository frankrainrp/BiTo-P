CREATE TABLE IF NOT EXISTS companion_preferences (
    member_id TEXT PRIMARY KEY REFERENCES members(id) ON DELETE CASCADE,
    morning_time TEXT NOT NULL DEFAULT '07:30',
    morning_enabled INTEGER NOT NULL DEFAULT 0 CHECK(morning_enabled IN (0,1)),
    interests TEXT NOT NULL DEFAULT '',
    companion_name TEXT NOT NULL DEFAULT '小友',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS morning_briefings (
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    local_date TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(member_id,local_date)
);
