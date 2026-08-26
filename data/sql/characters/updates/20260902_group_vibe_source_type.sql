-- Remember WHY a session vibe was set.
--
-- llm_group_vibe only stored the mood word, so the prompt
-- could say "the group is humbled" but never name the thing
-- that humbled it. source_type carries the memory_type of the
-- memory that set the vibe (an llm_bot_memories.memory_type
-- ENUM value: wipe, boss_kill, bg_loss, ...), which
-- build_session_vibe_line() in chatter_prompts.py turns into a
-- grounded sentence. NULL for rows written before this change
-- and for anything unmapped -- the prompt falls back to the
-- sourceless phrasing. Idempotent: only adds when missing.

SET @has_vibe_source = (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'llm_group_vibe'
    AND COLUMN_NAME = 'source_type'
);

SET @sql = IF(
  @has_vibe_source = 0,
  "ALTER TABLE `llm_group_vibe` ADD COLUMN `source_type` VARCHAR(32) NULL AFTER `vibe`",
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
