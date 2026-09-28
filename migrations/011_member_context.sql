ALTER TABLE companion_preferences
ADD COLUMN model_provider TEXT NOT NULL DEFAULT 'cloud'
CHECK(model_provider IN ('cloud', 'ollama'));

CREATE TABLE member_profiles (
    member_id TEXT PRIMARY KEY REFERENCES members(id) ON DELETE CASCADE,
    preferred_language TEXT NOT NULL DEFAULT 'zh-CN' CHECK(length(preferred_language) BETWEEN 2 AND 16),
    timezone_override TEXT CHECK(timezone_override IS NULL OR length(timezone_override) BETWEEN 1 AND 64),
    quiet_start TEXT CHECK(quiet_start IS NULL OR quiet_start GLOB '[0-2][0-9]:[0-5][0-9]'),
    quiet_end TEXT CHECK(quiet_end IS NULL OR quiet_end GLOB '[0-2][0-9]:[0-5][0-9]'),
    assistance_preferences TEXT NOT NULL DEFAULT '' CHECK(length(assistance_preferences) <= 2000),
    updated_at TEXT NOT NULL
);

CREATE TABLE member_relationships (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    related_member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    relationship TEXT NOT NULL CHECK(length(relationship) BETWEEN 1 AND 40),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(member_id <> related_member_id),
    UNIQUE(member_id, related_member_id)
);
CREATE INDEX member_relationships_household ON member_relationships(household_id, member_id);

CREATE TABLE consent_grants (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    grantor_member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    grantee_member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    data_scope TEXT NOT NULL CHECK(data_scope IN ('calendar', 'tasks', 'habits', 'activity_summary', 'profile')),
    purpose TEXT NOT NULL CHECK(length(purpose) BETWEEN 1 AND 240),
    expires_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    CHECK(grantor_member_id <> grantee_member_id)
);
CREATE INDEX consent_grants_member ON consent_grants(household_id, grantor_member_id, grantee_member_id);
CREATE UNIQUE INDEX consent_grants_active_unique
ON consent_grants(grantor_member_id, grantee_member_id, data_scope)
WHERE revoked_at IS NULL;
