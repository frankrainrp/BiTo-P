ALTER TABLE companion_preferences
ADD COLUMN system_prompt TEXT NOT NULL DEFAULT ''
CHECK(length(system_prompt) <= 4000);
