CREATE TABLE companion_conversations (
    id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 120),
    retention_days INTEGER NOT NULL DEFAULT 365 CHECK(retention_days IN (0, 7, 30, 90, 365)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_companion_conversations_member_updated
ON companion_conversations(member_id, updated_at DESC);

CREATE TABLE companion_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES companion_conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 2000),
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(id, conversation_id)
);

CREATE INDEX idx_companion_messages_conversation_created
ON companion_messages(conversation_id, created_at, id);

CREATE TABLE companion_attachments (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES companion_conversations(id) ON DELETE CASCADE,
    message_id TEXT,
    original_name TEXT NOT NULL CHECK(length(original_name) BETWEEN 1 AND 255),
    content_type TEXT NOT NULL CHECK(length(content_type) BETWEEN 1 AND 120),
    size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 0 AND 16777216),
    storage_key TEXT NOT NULL UNIQUE CHECK(length(storage_key) BETWEEN 1 AND 255),
    created_at TEXT NOT NULL,
    FOREIGN KEY(message_id, conversation_id) REFERENCES companion_messages(id, conversation_id) ON DELETE CASCADE
);

CREATE INDEX idx_companion_attachments_conversation
ON companion_attachments(conversation_id, created_at);
