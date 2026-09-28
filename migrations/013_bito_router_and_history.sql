ALTER TABLE companion_preferences
ADD COLUMN model_route TEXT NOT NULL DEFAULT 'auto'
CHECK(model_route IN ('auto', 'cloud', 'ollama'));

ALTER TABLE companion_preferences
ADD COLUMN cloud_model_override TEXT NOT NULL DEFAULT ''
CHECK(length(cloud_model_override) <= 160);

ALTER TABLE companion_preferences
ADD COLUMN ollama_model_override TEXT NOT NULL DEFAULT ''
CHECK(length(ollama_model_override) <= 160);
