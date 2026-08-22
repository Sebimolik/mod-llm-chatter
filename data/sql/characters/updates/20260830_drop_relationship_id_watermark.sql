-- Drop the vestigial relationship id-watermark column.
--
-- llm_bot_relationships.updated_through_memory_id was replaced
-- by the timestamp watermark (updated_through_created_at) and is
-- no longer read or written (see chatter_memory.py's
-- _maybe_update_relationship()). Drop it so the live schema
-- matches the base table. Idempotent: only drops when present.

SET @has_id_watermark = (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'llm_bot_relationships'
    AND COLUMN_NAME = 'updated_through_memory_id'
);

SET @sql = IF(
  @has_id_watermark = 1,
  "ALTER TABLE `llm_bot_relationships` DROP COLUMN `updated_through_memory_id`",
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
