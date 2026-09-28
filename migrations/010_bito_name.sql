-- Rename only the two former shipped defaults; preserve household custom names.
UPDATE companion_preferences SET companion_name = 'Bito' WHERE companion_name IN ('贾维斯', 'Jarvis');
