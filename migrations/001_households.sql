CREATE TABLE households (
    id TEXT PRIMARY KEY,
    singleton INTEGER NOT NULL DEFAULT 1 UNIQUE CHECK(singleton = 1),
    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
    timezone TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE members (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 1 AND 80),
    role TEXT NOT NULL CHECK(role IN ('adult', 'child', 'caregiver', 'visitor')),
    is_admin INTEGER NOT NULL DEFAULT 0 CHECK(is_admin IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX one_household_name ON households(lower(name));
