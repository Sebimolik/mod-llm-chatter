-- --------------------------------------------------------
-- Persistent bot-player relationship tracking.
--
-- One running, LLM-maintained description per (bot_guid,
-- player_guid) pair of how a bot feels about a specific
-- player -- a standing disposition, distinct from the
-- individual llm_bot_memories journal entries it is
-- periodically condensed from.
-- --------------------------------------------------------

CREATE TABLE IF NOT EXISTS `llm_bot_relationships` (
    `bot_guid`                   INT UNSIGNED NOT NULL,
    `player_guid`                INT UNSIGNED NOT NULL,
    `summary`                    TEXT NOT NULL,
    `updated_through_memory_id`  BIGINT UNSIGNED NOT NULL DEFAULT 0,
    `updated_at`                 TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`bot_guid`, `player_guid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
