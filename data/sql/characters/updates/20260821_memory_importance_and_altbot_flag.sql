-- Alt-bot flag on group bot traits, importance score on bot memories.

SET @has_is_altbot = (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'llm_group_bot_traits'
    AND COLUMN_NAME = 'is_altbot'
);

SET @sql = IF(
  @has_is_altbot = 0,
  "ALTER TABLE `llm_group_bot_traits` ADD COLUMN `is_altbot` TINYINT(1) NOT NULL DEFAULT 1 AFTER `bot_name`",
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @has_importance_score = (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'llm_bot_memories'
    AND COLUMN_NAME = 'importance_score'
);

SET @sql = IF(
  @has_importance_score = 0,
  "ALTER TABLE `llm_bot_memories` ADD COLUMN `importance_score` TINYINT UNSIGNED NOT NULL DEFAULT 5 AFTER `memory`",
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
