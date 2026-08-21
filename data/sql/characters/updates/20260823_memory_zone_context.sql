-- Numeric zone_id on bot memories, for zone-aware
-- recall tie-breaking (see get_bot_memories()).

SET @has_zone_id = (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'llm_bot_memories'
    AND COLUMN_NAME = 'zone_id'
);

SET @sql = IF(
  @has_zone_id = 0,
  "ALTER TABLE `llm_bot_memories` ADD COLUMN `zone_id` INT UNSIGNED DEFAULT NULL AFTER `importance_score`",
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
