"""Prompts retrieve the configured number of memories."""

import unittest

import test_persona_coherence  # dependency stubs and tools path
from chatter_memory import get_bot_memories, memory_recall_count


class _Cursor:
    def __init__(self, rows):
        self.params = None
        self._rows = rows

    def execute(self, sql, params):
        if self.params is None:
            self.params = params

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _DB:
    def __init__(self, rows=()):
        self._rows = rows
        self.last = _Cursor(rows)

    def cursor(self, *args, **kwargs):
        self.last = _Cursor(self._rows)
        return self.last

    def commit(self):
        pass


class MemoryRecallCountTests(unittest.TestCase):
    def test_defaults_keep_three_and_two(self):
        self.assertEqual(memory_recall_count({}), 3)
        self.assertEqual(memory_recall_count({}, idle=True), 2)

    def test_configured_counts(self):
        config = {
            'LLMChatter.Memory.RecallCount': '5',
            'LLMChatter.Memory.IdleRecallCount': '1',
        }
        self.assertEqual(memory_recall_count(config), 5)
        self.assertEqual(memory_recall_count(config, idle=True), 1)

    def test_clamped_and_invalid_values(self):
        self.assertEqual(
            memory_recall_count({'LLMChatter.Memory.RecallCount': '0'}), 1
        )
        self.assertEqual(
            memory_recall_count({'LLMChatter.Memory.RecallCount': '50'}),
            10,
        )
        self.assertEqual(
            memory_recall_count(
                {'LLMChatter.Memory.IdleRecallCount': 'many'}, idle=True
            ),
            2,
        )

    def test_count_limits_what_comes_back(self):
        """The configured count caps the memories retrieved.

        It is not a SQL parameter here: candidates are over-fetched in
        decay-aware order and trimmed in Python, because one LIMIT
        cannot be applied per bot in the batched party query.
        """
        rows = [
            {'id': n, 'bot_guid': 11, 'memory': 'memory %d' % n,
             'effective_score': 10 - n}
            for n in range(8)
        ]
        db = _DB(rows)
        got = get_bot_memories(
            db, 11, 22,
            count=memory_recall_count(
                {'LLMChatter.Memory.RecallCount': '4'}
            ),
        )
        self.assertEqual(len(got), 4)
        self.assertEqual(db.last.params[:2], (11, 22))


if __name__ == '__main__':
    unittest.main()
