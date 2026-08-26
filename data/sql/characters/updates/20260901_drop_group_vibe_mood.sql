-- Drop the redundant session-vibe mood column.
--
-- llm_group_vibe.mood was always written with the exact same
-- value as llm_group_vibe.vibe (upsert_group_vibe() in
-- chatter_db.py was called as upsert_group_vibe(..., mood,
-- mood, ...)) and was never read back: get_group_vibe() only
-- ever selects vibe and set_at. Drop it so the live schema
-- matches the base table. Idempotent: only drops when present.

SET @has_vibe_mood = (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'llm_group_vibe'
    AND COLUMN_NAME = 'mood'
);

SET @sql = IF(
  @has_vibe_mood = 1,
  "ALTER TABLE `llm_group_vibe` DROP COLUMN `mood`",
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
