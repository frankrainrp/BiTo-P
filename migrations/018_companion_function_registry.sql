CREATE TABLE companion_function_registry (
    function_key TEXT PRIMARY KEY CHECK(length(function_key) BETWEEN 3 AND 80),
    action_type TEXT NOT NULL UNIQUE CHECK(length(action_type) BETWEEN 3 AND 80),
    category TEXT NOT NULL CHECK(category IN ('local', 'mcp', 'webhook')),
    display_name_zh TEXT NOT NULL CHECK(length(display_name_zh) BETWEEN 1 AND 80),
    display_name_en TEXT NOT NULL CHECK(length(display_name_en) BETWEEN 1 AND 80),
    description_zh TEXT NOT NULL CHECK(length(description_zh) BETWEEN 1 AND 240),
    description_en TEXT NOT NULL CHECK(length(description_en) BETWEEN 1 AND 240),
    input_schema_json TEXT NOT NULL CHECK(length(input_schema_json) BETWEEN 2 AND 4000),
    output_schema_json TEXT NOT NULL CHECK(length(output_schema_json) BETWEEN 2 AND 2000),
    minimum_permission TEXT NOT NULL CHECK(minimum_permission IN ('member', 'admin')),
    scope TEXT NOT NULL CHECK(scope IN ('member', 'household')),
    risk TEXT NOT NULL CHECK(risk IN ('read', 'confirm', 'restricted')),
    handler_key TEXT NOT NULL CHECK(length(handler_key) BETWEEN 3 AND 80),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
    health_status TEXT NOT NULL DEFAULT 'disabled' CHECK(health_status IN ('ready', 'unconfigured', 'disabled')),
    timeout_seconds INTEGER NOT NULL DEFAULT 10 CHECK(timeout_seconds BETWEEN 1 AND 60),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE companion_function_calls (
    id TEXT PRIMARY KEY,
    function_key TEXT NOT NULL REFERENCES companion_function_registry(function_key) ON DELETE RESTRICT,
    proposal_id TEXT UNIQUE REFERENCES companion_action_proposals(id) ON DELETE SET NULL,
    household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK(source IN ('model', 'deterministic', 'member')),
    status TEXT NOT NULL CHECK(status IN ('proposed', 'executing', 'succeeded', 'rejected', 'expired', 'failed')),
    request_summary_json TEXT NOT NULL CHECK(length(request_summary_json) BETWEEN 2 AND 4000),
    result_summary_json TEXT CHECK(result_summary_json IS NULL OR length(result_summary_json) BETWEEN 2 AND 4000),
    error_code TEXT CHECK(error_code IS NULL OR length(error_code) BETWEEN 1 AND 80),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    executed_at TEXT
);

CREATE INDEX companion_function_calls_member_created
ON companion_function_calls(member_id, household_id, created_at DESC);

CREATE INDEX companion_function_calls_function_status
ON companion_function_calls(function_key, status, created_at DESC);

INSERT INTO companion_function_registry(
    function_key, action_type, category, display_name_zh, display_name_en,
    description_zh, description_en, input_schema_json, output_schema_json,
    minimum_permission, scope, risk, handler_key, enabled, health_status,
    timeout_seconds, created_at, updated_at
) VALUES
(
    'task.create', 'task.create', 'local', '新增任务', 'Create task',
    '为当前成员提出一项私有任务，确认后才写入。',
    'Proposes a private task for the current member and writes only after confirmation.',
    '{"type":"object","properties":{"title":{"type":"string","maxLength":160},"due_at":{"type":"string"}},"required":["title"],"additionalProperties":false}',
    '{"type":"object"}', 'member', 'member', 'confirm', 'task.create', 1, 'ready', 10,
    datetime('now'), datetime('now')
),
(
    'task.complete', 'task.complete', 'local', '完成任务', 'Complete task',
    '为当前成员提出完成一项可写任务，确认后才写入。',
    'Proposes completing a writable task and writes only after confirmation.',
    '{"type":"object","properties":{"task_id":{"type":"string","format":"uuid"}},"required":["task_id"],"additionalProperties":false}',
    '{"type":"object"}', 'member', 'member', 'confirm', 'task.complete', 1, 'ready', 10,
    datetime('now'), datetime('now')
),
(
    'calendar.create', 'calendar.create', 'local', '新增日程', 'Create calendar event',
    '为当前成员提出一项私有日程，确认后才写入。',
    'Proposes a private calendar event for the current member and writes only after confirmation.',
    '{"type":"object","properties":{"title":{"type":"string","maxLength":160},"starts_at":{"type":"string"},"ends_at":{"type":"string"}},"required":["title","starts_at","ends_at"],"additionalProperties":false}',
    '{"type":"object"}', 'member', 'member', 'confirm', 'calendar.create', 1, 'ready', 10,
    datetime('now'), datetime('now')
);
