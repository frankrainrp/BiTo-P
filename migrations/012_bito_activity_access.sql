ALTER TABLE companion_preferences
ADD COLUMN activity_access_enabled INTEGER NOT NULL DEFAULT 0
CHECK(activity_access_enabled IN (0, 1));
