CREATE TABLE monitor_subscriptions (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 120),
    url TEXT NOT NULL CHECK(length(url) <= 2048),
    source_type TEXT NOT NULL CHECK(source_type IN ('text','json')),
    json_path TEXT NOT NULL DEFAULT '',
    condition TEXT NOT NULL CHECK(condition IN ('change','contains','above','below')),
    target TEXT NOT NULL DEFAULT '',
    interval_seconds INTEGER NOT NULL CHECK(interval_seconds BETWEEN 60 AND 604800),
    cooldown_seconds INTEGER NOT NULL CHECK(cooldown_seconds BETWEEN 60 AND 604800),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    channel TEXT NOT NULL CHECK(channel IN ('in_app','webhook','telegram')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    next_check_at INTEGER NOT NULL,
    last_checked_at TEXT,
    last_value TEXT,
    last_fingerprint TEXT,
    last_status TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    last_matched INTEGER NOT NULL DEFAULT 0,
    pending_notification INTEGER NOT NULL DEFAULT 0,
    last_notified_at INTEGER,
    claimed_until INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX monitor_member ON monitor_subscriptions(member_id,created_at);
CREATE INDEX monitor_due ON monitor_subscriptions(enabled,next_check_at);

CREATE TABLE monitor_runs (
    id TEXT PRIMARY KEY,
    subscription_id TEXT NOT NULL REFERENCES monitor_subscriptions(id) ON DELETE CASCADE,
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ok','error')),
    value TEXT,
    matched INTEGER NOT NULL DEFAULT 0,
    notified INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    delivery_status TEXT NOT NULL DEFAULT 'none',
    evidence_json TEXT NOT NULL
);
CREATE INDEX monitor_runs_time ON monitor_runs(subscription_id,checked_at);

CREATE TABLE monitor_alerts (
    id TEXT PRIMARY KEY,
    subscription_id TEXT NOT NULL REFERENCES monitor_subscriptions(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL UNIQUE REFERENCES monitor_runs(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read_at TEXT,
    delivery_status TEXT NOT NULL
);
CREATE INDEX monitor_alerts_time ON monitor_alerts(created_at);
