#!/usr/bin/env python3
"""Focused checks for batched memory recall and the
derived post-insert active count.

Run directly from the module root:
  python tools/tests/test_memory_batch_recall.py
"""

import importlib
import sys
import types
from pathlib import Path
from unittest.mock import patch


def _ensure_module(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


def _install_non_strict_stubs() -> None:
    for module_name in ("anthropic", "openai"):
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError:
            module = _ensure_module(module_name)
            class_name = (
                "Anthropic"
                if module_name == "anthropic"
                else "OpenAI"
            )
            setattr(
                module,
                class_name,
                type(class_name, (), {}),
            )

    try:
        importlib.import_module("mysql.connector")
    except ModuleNotFoundError:
        mysql_module = _ensure_module("mysql")
        connector_module = _ensure_module(
            "mysql.connector"
        )
        setattr(
            mysql_module,
            "connector",
            connector_module,
        )


TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
_install_non_strict_stubs()

import chatter_memory  # noqa: E402


class _Cursor:
    """Returns a canned, already-ordered SELECT result and
    records every statement it is handed."""

    def __init__(self, db):
        self._db = db

    def execute(self, query, params=None):
        self._db.executed.append((query, params))
        if query.lstrip().upper().startswith('SELECT'):
            self._db.last_select_params = params

    def fetchall(self):
        return list(self._db.rows)

    def fetchone(self):
        return None

    def close(self):
        pass


class _Db:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []
        self.commits = 0
        self.last_select_params = None

    def cursor(self, dictionary=False):
        return _Cursor(self)

    def commit(self):
        self.commits += 1


def _row(rid, bot, text):
    return {
        'id': rid, 'bot_guid': bot, 'memory': text,
        'effective_score': 5,
    }


# Ordered exactly as the DB hands them back: grouped by
# bot_guid, best-first inside each bot.
_ROWS = [
    _row(1, 11, 'a1'), _row(2, 11, 'a2'),
    _row(3, 11, 'a3'), _row(4, 11, 'a4'),
    _row(5, 12, 'b1'), _row(6, 12, 'b2'),
    _row(7, 14, 'd1'),
]

_CONFIG = {'LLMChatter.Memory.MaxInjectTokens': '400'}


def test_batch_partitions_per_bot_and_applies_count():
    db = _Db(_ROWS)
    out = chatter_memory.get_bot_memories_batch(
        db, [11, 12, 13, 14], 800, config=_CONFIG,
        count=2,
    )
    # Per-bot `count` limit, per-bot ordering preserved.
    assert out == {
        11: ['a1', 'a2'],
        12: ['b1', 'b2'],
        14: ['d1'],
    }
    # A bot with no rows is simply absent, not empty-listed.
    assert 13 not in out


def test_batch_issues_one_select_one_update_one_commit():
    db = _Db(_ROWS)
    chatter_memory.get_bot_memories_batch(
        db, [11, 12, 14], 800, config=_CONFIG, count=2,
    )
    selects = [
        q for q, _ in db.executed
        if q.lstrip().upper().startswith('SELECT')
    ]
    updates = [
        (q, p) for q, p in db.executed
        if q.lstrip().upper().startswith('UPDATE')
    ]
    assert len(selects) == 1
    assert len(updates) == 1
    assert db.commits == 1
    # Only the rows actually returned are marked used.
    assert set(updates[0][1]) == {1, 2, 5, 6, 7}


def test_batch_mark_used_false_writes_nothing():
    db = _Db(_ROWS)
    out = chatter_memory.get_bot_memories_batch(
        db, [11, 12], 800, config=_CONFIG, count=2,
        mark_used=False,
    )
    assert out[11] == ['a1', 'a2']
    assert not [
        q for q, _ in db.executed
        if not q.lstrip().upper().startswith('SELECT')
    ]
    assert db.commits == 0


def test_batch_respects_token_budget_per_bot():
    rows = [
        _row(1, 11, 'x' * 400),
        _row(2, 11, 'y' * 400),
        _row(3, 12, 'z' * 20),
        _row(4, 12, 'w' * 20),
    ]
    db = _Db(rows)
    out = chatter_memory.get_bot_memories_batch(
        db, [11, 12], 800,
        config={'LLMChatter.Memory.MaxInjectTokens': '60'},
        count=3,
    )
    # Bot 11's first memory alone blows the budget, so it is
    # kept and the second is dropped; bot 12 is unaffected
    # by bot 11's spend.
    assert out[11] == ['x' * 400]
    assert out[12] == ['z' * 20, 'w' * 20]


def test_batch_dedupes_and_ignores_bad_guids():
    db = _Db(_ROWS)
    chatter_memory.get_bot_memories_batch(
        db, [11, 11, None, 'nope', 12], 800,
        config=_CONFIG, count=1,
    )
    params = db.last_select_params
    assert params[:2] == (11, 12)
    assert params[2] == 800


def test_batch_no_rows_returns_empty():
    db = _Db([])
    assert chatter_memory.get_bot_memories_batch(
        db, [11], 800, config=_CONFIG,
    ) == {}
    assert db.commits == 0


def test_single_bot_wrapper_matches_batch():
    db = _Db([r for r in _ROWS if r['bot_guid'] == 11])
    single = chatter_memory.get_bot_memories(
        db, 11, 800, config=_CONFIG, count=3,
    )
    db2 = _Db([r for r in _ROWS if r['bot_guid'] == 11])
    batched = chatter_memory.get_bot_memories_batch(
        db2, [11], 800, config=_CONFIG, count=3,
    )
    assert single == batched[11] == ['a1', 'a2', 'a3']


def test_zone_tiebreak_passes_zone_param():
    db = _Db(_ROWS)
    chatter_memory.get_bot_memories_batch(
        db, [11], 800, config=_CONFIG, count=1,
        current_zone_id=1497,
    )
    query, params = db.executed[0]
    assert '(zone_id = %s) DESC' in query
    assert params[-1] == 1497


# ------------------------------------------------------------
# Derived post-insert active count
# ------------------------------------------------------------

class _InsertDb:
    def __init__(self):
        self.commits = 0

    def cursor(self):
        return _Cursor(_Db([]))

    def commit(self):
        self.commits += 1


def _run_insert(active, cnt, max_per, evict_ok=True):
    seen = {'counts': 0, 'trigger': None}

    def _count(cursor, bot_guid, player_guid):
        seen['counts'] += 1
        return cnt

    def _evict(cursor, conn, bot_guid, player_guid,
               config=None):
        return evict_ok

    def _trigger(config, bot_guid, player_guid,
                 new_count, max_per_):
        seen['trigger'] = new_count

    with patch.object(
        chatter_memory, '_count_active_memories', _count
    ), patch.object(
        chatter_memory, '_evict_one_used', _evict
    ), patch.object(
        chatter_memory, '_maybe_trigger_condensation',
        _trigger
    ):
        ok = chatter_memory._ensure_cap_and_insert(
            _InsertDb(), {}, 1, 2, 3, 'ambient', 'text',
            'warm', None, 0.0, active, max_per,
        )
    return ok, seen


def test_active_count_is_derived_not_requeried():
    ok, seen = _run_insert(active=1, cnt=10, max_per=30)
    assert ok
    # The second COUNT(*) per insert is gone.
    assert seen['counts'] == 1
    assert seen['trigger'] == 11


def test_derived_count_after_eviction():
    ok, seen = _run_insert(active=1, cnt=30, max_per=30)
    assert ok
    assert seen['counts'] == 1
    # One row out, one row in -> unchanged pool size.
    assert seen['trigger'] == 30


def test_derived_count_for_pending_inactive_row():
    ok, seen = _run_insert(active=0, cnt=10, max_per=30)
    assert ok
    # active=0 rows do not count toward the active pool.
    assert seen['trigger'] == 10


def test_full_pool_with_nothing_to_evict_still_bails():
    ok, seen = _run_insert(
        active=1, cnt=30, max_per=30, evict_ok=False,
    )
    assert ok is False
    assert seen['trigger'] is None


if __name__ == '__main__':
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith('test_') and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"\n{len(tests)} tests passed")
