-- Add gear_change and mount_change to llm_bot_memories.
-- memory_type so the Python bridge can actually store the
-- memories queued by _gear_change_post_success()/
-- _mount_change_post_success() (20260826_gear_mount_-
-- change_event_types.sql only extended the event-queue
-- enum, not this one). Idempotent: guarded by a
-- COLUMN_TYPE check, same pattern as that migration.

SET @has_gear_change = (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME   = 'llm_bot_memories'
    AND COLUMN_NAME  = 'memory_type'
    AND COLUMN_TYPE LIKE '%gear_change%'
);
SET @sql = IF(@has_gear_change = 0,
  "ALTER TABLE `llm_bot_memories`
  MODIFY COLUMN `memory_type` ENUM(
    'ambient', 'boss_kill', 'wipe', 'rare_kill',
    'dungeon', 'party_member', 'player_message',
    'first_meeting', 'quest_complete', 'achievement',
    'level_up', 'bg_win', 'bg_loss',
    'discovery', 'pvp_kill',
    'gear_change', 'mount_change'
  ) NOT NULL",
  'SELECT 1');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
