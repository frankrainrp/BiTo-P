INSERT INTO companion_function_registry(
    function_key, action_type, category, display_name_zh, display_name_en,
    description_zh, description_en, input_schema_json, output_schema_json,
    minimum_permission, scope, risk, handler_key, enabled, health_status,
    timeout_seconds, created_at, updated_at
) VALUES (
    'today.read', 'today.read', 'local', '读取今日安排', 'Read today',
    '只读取当前成员可见的今日任务、日程和习惯；不会修改数据。',
    'Reads today’s tasks, events and habits visible to the current member without changing data.',
    '{"type":"object","properties":{},"additionalProperties":false}',
    '{"type":"object"}', 'member', 'member', 'read', 'today.read', 1, 'ready', 10,
    datetime('now'), datetime('now')
);
