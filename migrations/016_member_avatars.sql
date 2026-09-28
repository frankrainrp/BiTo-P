CREATE TABLE member_avatars (
    member_id TEXT PRIMARY KEY REFERENCES members(id) ON DELETE CASCADE,
    content_type TEXT NOT NULL CHECK(content_type IN ('image/png', 'image/jpeg', 'image/webp')),
    image BLOB NOT NULL CHECK(length(image) > 0 AND length(image) <= 2097152),
    updated_at TEXT NOT NULL
);
