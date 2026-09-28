CREATE TABLE activity_app_icons (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    source_device TEXT NOT NULL CHECK(length(source_device) BETWEEN 1 AND 80),
    app_name TEXT NOT NULL CHECK(length(app_name) BETWEEN 1 AND 120),
    icon_png BLOB NOT NULL CHECK(length(icon_png) BETWEEN 33 AND 65536),
    icon_sha256 TEXT NOT NULL CHECK(length(icon_sha256) = 64),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(member_id, source_device, app_name)
);

CREATE INDEX activity_app_icons_member ON activity_app_icons(member_id, source_device, app_name);
