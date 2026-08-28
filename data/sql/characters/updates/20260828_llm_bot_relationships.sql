-- --------------------------------------------------------
-- Persistent bot-player relationship tracking.
--
-- One running, LLM-maintained description per (bot_guid,
-- player_guid) pair of how a bot feels about a specific
-- player -- a standing disposition, distinct from the
-- individual llm_bot_memories journal entries it is
-- periodically condensed from (see chatter_memory.py's
-- _maybe_update_relationship()).
--
-- updated_through_created_at is the watermark marking the
-- newest memory already folded into the summary. It is a
-- timestamp rather than a memory id because condensation
-- deletes its source rows and re-inserts their content as
-- digests with fresh, higher ids; a digest instead inherits
-- its oldest source's created_at, so a timestamp watermark
-- keeps already-summarized material from being folded in a
-- second time.
-- --------------------------------------------------------

CREATE TABLE IF NOT EXISTS `llm_bot_relationships` (
    `bot_guid`                   INT UNSIGNED NOT NULL,
    `player_guid`                INT UNSIGNED NOT NULL,
    `summary`                    TEXT NOT NULL,
    `updated_through_created_at` DATETIME NOT NULL DEFAULT '1970-01-01 00:00:00',
    `updated_at`                 TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`bot_guid`, `player_guid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
