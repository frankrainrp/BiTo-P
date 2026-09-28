CREATE TABLE calendar_events (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    owner_member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 160),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL CHECK(ends_at > starts_at),
    visibility TEXT NOT NULL DEFAULT 'private' CHECK(visibility IN ('private', 'household')),
    source TEXT NOT NULL DEFAULT 'manual' CHECK(length(source) BETWEEN 1 AND 40),
    created_at TEXT NOT NULL
);

CREATE INDEX calendar_events_member_time ON calendar_events(owner_member_id, starts_at, ends_at);

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    owner_member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 160),
    due_at TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'completed')),
    visibility TEXT NOT NULL DEFAULT 'private' CHECK(visibility IN ('private', 'household')),
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX tasks_member_due ON tasks(owner_member_id, status, due_at);

CREATE TABLE habits (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    owner_member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 120),
    target_per_week INTEGER NOT NULL CHECK(target_per_week BETWEEN 1 AND 7),
    visibility TEXT NOT NULL DEFAULT 'private' CHECK(visibility IN ('private', 'household')),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived')),
    created_at TEXT NOT NULL
);

CREATE TABLE habit_checkins (
    id TEXT PRIMARY KEY,
    habit_id TEXT NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    occurred_on TEXT NOT NULL CHECK(length(occurred_on) = 10),
    created_at TEXT NOT NULL,
    UNIQUE(habit_id, occurred_on)
);

CREATE INDEX habit_checkins_member_date ON habit_checkins(member_id, occurred_on);

CREATE TABLE household_events (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    actor_member_id TEXT REFERENCES members(id) ON DELETE SET NULL,
    subject_member_id TEXT REFERENCES members(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL CHECK(length(event_type) BETWEEN 1 AND 80),
    source TEXT NOT NULL CHECK(length(source) BETWEEN 1 AND 40),
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE INDEX household_events_time ON household_events(household_id, occurred_at);

