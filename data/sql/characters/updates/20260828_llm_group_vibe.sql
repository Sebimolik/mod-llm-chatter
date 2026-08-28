-- --------------------------------------------------------
-- Group session vibe persistence.
--
-- The group's ambient "vibe" (a short-lived mood cue set
-- when an important memory lands, see get_session_vibe()
-- in chatter_memory.py) is persisted here so it survives
-- bridge restarts and the in-memory session CLEANUP wipe.
-- Rows are UPSERTed by _ensure_cap_and_insert() whenever a
-- memory's importance_score crosses
-- LLMChatter.GroupChatter.VibeImportanceThreshold and are
-- lazily expired/deleted by get_session_vibe() once
-- LLMChatter.GroupChatter.VibeDurationSeconds have elapsed
-- since set_at.
--
-- source_type carries the memory_type of the memory that
-- set the vibe, so build_session_vibe_line() in
-- chatter_prompts.py can name the cause ("still humbled
-- after a recent wipe") and not just the mood. NULL when
-- the cause is unknown or unmapped; the prompt then falls
-- back to sourceless phrasing. Idempotent: CREATE TABLE IF
-- NOT EXISTS.
-- --------------------------------------------------------

CREATE TABLE IF NOT EXISTS `llm_group_vibe` (
    `group_id`   INT UNSIGNED NOT NULL,
    `vibe`       VARCHAR(64)  NOT NULL,
    `source_type` VARCHAR(32) NULL,
    `importance` TINYINT UNSIGNED NOT NULL DEFAULT 5,
    `set_at`     INT UNSIGNED NOT NULL,
    `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
