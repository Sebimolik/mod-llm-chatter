-- --------------------------------------------------------
-- Memory scoring/zone context and the alt-bot flag.
--
--   1. llm_group_bot_traits.is_altbot -- distinguishes a
--      playerbot alt from a randombot.
--   2. llm_bot_memories.importance_score -- the 1-10 rubric
--      used throughout chatter_memory.py
--      (_IMPORTANCE_RUBRIC / _effective_score_sql()), with a
--      CHECK constraint keeping rows inside that range.
--   3. llm_bot_memories.zone_id -- numeric zone for
--      zone-aware recall tie-breaking (get_bot_memories()).
--   4. llm_bot_memories.condensation_generation -- how many
--      rounds of condensation a row is deep, so digests
--      can't be re-folded forever (capped by
--      LLMChatter.Memory.Condensation.MaxGenerations).
-- --------------------------------------------------------

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

SET @has_chk_importance_score = (
  SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'llm_bot_memories'
    AND CONSTRAINT_NAME = 'chk_importance_score'
);

SET @sql = IF(
  @has_chk_importance_score = 0,
  "ALTER TABLE `llm_bot_memories` ADD CONSTRAINT `chk_importance_score` CHECK (`importance_score` BETWEEN 1 AND 10)",
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

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

SET @has_generation = (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'llm_bot_memories'
    AND COLUMN_NAME = 'condensation_generation'
);

SET @sql = IF(
  @has_generation = 0,
  "ALTER TABLE `llm_bot_memories`
     ADD COLUMN `condensation_generation` TINYINT UNSIGNED
     NOT NULL DEFAULT 0
     AFTER `zone_id`",
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
