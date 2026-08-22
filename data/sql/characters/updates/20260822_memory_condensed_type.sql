-- Add 'condensed' to llm_bot_memories.memory_type so
-- background condensation (see chatter_memory.py's
-- _condense_low_value_memories()) can store digest memories
-- with a distinct, visible type instead of overloading
-- 'ambient'. No C++ change needed: .llmc memory's existing
-- [Type] (importance N, emote) text display already surfaces
-- memory_type generically. Idempotent: guarded by a
-- COLUMN_TYPE check, same pattern as
-- 20260827_memory_gear_mount_change_types.sql.

SET @has_condensed = (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME   = 'llm_bot_memories'
    AND COLUMN_NAME  = 'memory_type'
    AND COLUMN_TYPE LIKE '%condensed%'
);
SET @sql = IF(@has_condensed = 0,
  "ALTER TABLE `llm_bot_memories`
  MODIFY COLUMN `memory_type` ENUM(
    'ambient', 'boss_kill', 'wipe', 'rare_kill',
    'dungeon', 'party_member', 'player_message',
    'first_meeting', 'quest_complete', 'achievement',
    'level_up', 'bg_win', 'bg_loss',
    'discovery', 'pvp_kill',
    'gear_change', 'mount_change', 'condensed'
  ) NOT NULL",
  'SELECT 1');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
