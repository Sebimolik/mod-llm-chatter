-- Condensation generation counter on bot memories.
--
-- A condensation digest always lands below
-- LLMChatter.Memory.Condensation.ProtectFloor by construction,
-- so nothing stopped it from being condensed again, and again --
-- digests of digests, drifting further from the source facts
-- every round. Each digest now carries
-- max(source generations) + 1, and rows at or above
-- LLMChatter.Memory.Condensation.MaxGenerations are never
-- picked as candidates again.

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

-- Digests that already exist predate the counter but are, by
-- definition, one round of folding deep -- record them as
-- generation 1 so they get the same remaining budget a digest
-- created today would.
SET @sql = IF(
  @has_generation = 0,
  "UPDATE `llm_bot_memories`
     SET `condensation_generation` = 1
   WHERE `memory_type` = 'condensed'",
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
