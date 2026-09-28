CREATE TABLE activity_records (
    id TEXT PRIMARY KEY,
    client_event_id TEXT NOT NULL CHECK(length(client_event_id) BETWEEN 8 AND 80),
    household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    source_device TEXT NOT NULL CHECK(length(source_device) BETWEEN 1 AND 80),
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL CHECK(ended_at > started_at),
    app_name TEXT NOT NULL CHECK(length(app_name) BETWEEN 1 AND 120),
    window_title TEXT NOT NULL DEFAULT '' CHECK(length(window_title) <= 240),
    ocr_excerpt TEXT NOT NULL DEFAULT '' CHECK(length(ocr_excerpt) <= 1200),
    activity_summary TEXT NOT NULL DEFAULT '' CHECK(length(activity_summary) <= 600),
    category TEXT NOT NULL CHECK(category IN ('work','study','communication','entertainment','system','life','sensitive','other')),
    analysis_method TEXT NOT NULL CHECK(analysis_method IN ('accessibility','windows_ocr','local_ocr','local_vision','manual')),
    analysis_confidence REAL NOT NULL CHECK(analysis_confidence BETWEEN 0 AND 1),
    redacted INTEGER NOT NULL DEFAULT 1 CHECK(redacted = 1),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE(member_id, source_device, client_event_id)
);

CREATE INDEX activity_records_member_time ON activity_records(member_id, started_at, ended_at);
CREATE INDEX activity_records_expiry ON activity_records(expires_at);

CREATE TABLE activity_reports (
    id TEXT PRIMARY KEY,
    client_report_id TEXT NOT NULL CHECK(length(client_report_id) BETWEEN 8 AND 80),
    household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    source_device TEXT NOT NULL CHECK(length(source_device) BETWEEN 1 AND 80),
    report_date TEXT NOT NULL CHECK(length(report_date) = 10),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 160),
    summary TEXT NOT NULL CHECK(length(summary) BETWEEN 1 AND 12000),
    highlights_json TEXT NOT NULL,
    next_actions_json TEXT NOT NULL,
    analysis_method TEXT NOT NULL CHECK(analysis_method IN ('deterministic','local_ollama','manual')),
    model_name TEXT NOT NULL DEFAULT '' CHECK(length(model_name) <= 160),
    sample_size INTEGER NOT NULL CHECK(sample_size >= 0),
    observed_minutes REAL NOT NULL CHECK(observed_minutes >= 0),
    generated_at TEXT NOT NULL,
    redacted INTEGER NOT NULL DEFAULT 1 CHECK(redacted = 1),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE(member_id, source_device, client_report_id)
);

CREATE INDEX activity_reports_member_date ON activity_reports(member_id, report_date, generated_at);
CREATE INDEX activity_reports_expiry ON activity_reports(expires_at);
