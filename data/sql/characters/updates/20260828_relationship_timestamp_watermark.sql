-- Timestamp watermark for relationship summaries.
--
-- llm_bot_relationships.updated_through_memory_id tracked the
-- newest memory already folded into the running summary by its
-- auto-increment id. Memory condensation breaks that: it deletes
-- the source rows and re-inserts their content as digests with
-- fresh, higher ids, so already-summarized material looks new
-- again and gets folded in a second time.
--
-- The replacement watermark is a timestamp, which works because
-- a digest inherits its oldest source's created_at (see
-- chatter_memory.py's _condense_low_value_memories()).
-- updated_through_memory_id is kept as a vestigial debugging /
-- rollback breadcrumb: still written, no longer read.

SET @has_watermark_ts = (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'llm_bot_relationships'
    AND COLUMN_NAME = 'updated_through_created_at'
);

SET @sql = IF(
  @has_watermark_ts = 0,
  "ALTER TABLE `llm_bot_relationships`
     ADD COLUMN `updated_through_created_at` DATETIME NOT NULL
     DEFAULT '1970-01-01 00:00:00'
     AFTER `updated_through_memory_id`",
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Backfill, only on the pass that added the column: use the
-- created_at of the memory the old id watermark pointed at; if
-- that row is gone (evicted or condensed away), fall back to
-- when the summary itself was last written; otherwise leave the
-- epoch default, which simply re-folds the pair once.
-- updated_at is explicitly re-assigned to itself so this
-- backfill does not trip the column's ON UPDATE
-- CURRENT_TIMESTAMP and destroy the fallback it just used.
SET @sql = IF(
  @has_watermark_ts = 0,
  "UPDATE `llm_bot_relationships` r
     LEFT JOIN `llm_bot_memories` m
       ON m.id = r.updated_through_memory_id
      AND m.bot_guid = r.bot_guid
      AND m.player_guid = r.player_guid
     SET r.updated_through_created_at = COALESCE(
           m.created_at, r.updated_at, '1970-01-01 00:00:00'
         ),
         r.updated_at = r.updated_at
   WHERE r.updated_through_memory_id > 0",
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
