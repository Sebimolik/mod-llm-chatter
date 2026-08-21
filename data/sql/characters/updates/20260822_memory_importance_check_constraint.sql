-- CHECK constraint enforcing importance_score stays within
-- the 1-10 rubric used throughout chatter_memory.py
-- (_IMPORTANCE_RUBRIC / _effective_score_sql()).

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
