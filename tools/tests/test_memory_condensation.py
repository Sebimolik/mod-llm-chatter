#!/usr/bin/env python3
"""Focused checks for low-value-memory condensation.

Run directly from the module root:
  python tools/tests/test_memory_condensation.py
"""

import importlib
import json
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


# ============================================================
# Fakes
# ============================================================

class _CandidatesCursor:
    """Records the query/params passed to
    _get_condensation_candidates() and returns a canned
    row set, mirroring what a real cursor(dictionary=True)
    would hand back.
    """

    def __init__(self, rows):
        self.rows = rows
        self.last_query = None
        self.last_params = None

    def execute(self, query, params=None):
        self.last_query = query
        self.last_params = params

    def fetchall(self):
        return self.rows


class _CondenseCursor:
    """Records every statement _condense_low_value_memories()
    issues against its own connection, distinguishing the
    candidate SELECT from the digest INSERTs and the source
    DELETE by a query-text prefix, same convention as
    test_guild_player_replies.py's _SummaryCursor.
    """

    def __init__(self, db):
        self.db = db
        self._rows = []

    def execute(self, query, params=None):
        self.db.executed.append((query, params))
        if query.startswith(
            "SELECT id, group_id, memory, importance_score,"
        ):
            self._rows = self.db.candidate_rows
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _CondenseDb:
    def __init__(self, candidate_rows):
        self.candidate_rows = candidate_rows
        self.executed = []
        self.commits = 0
        self.closed = False

    def cursor(self, *args, **kwargs):
        return _CondenseCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _row(row_id, memory, importance, group_id=41):
    return {
        'id': row_id,
        'group_id': group_id,
        'memory': memory,
        'importance_score': importance,
        'effective_score': importance,
    }


# ============================================================
# Candidate selection (_get_condensation_candidates)
# ============================================================

def test_candidates_query_passes_protect_floor_and_max_candidates():
    cursor = _CandidatesCursor(rows=[])
    config = {
        'LLMChatter.Memory.Condensation.ProtectFloor': 6,
        'LLMChatter.Memory.Condensation.MaxCandidates': 3,
    }
    chatter_memory._get_condensation_candidates(
        cursor, 100, 200, config,
    )
    assert 'LIMIT %s' in cursor.last_query
    assert 'ORDER BY effective_score ASC' in cursor.last_query
    # (bot_guid, player_guid, protect_floor, bot_guid,
    #  player_guid, max_candidates) -- the middle pair is the
    # top-row-exclusion subquery's own params.
    assert cursor.last_params == (100, 200, 6, 100, 200, 3)


def test_candidates_query_excludes_pairs_top_row():
    cursor = _CandidatesCursor(rows=[])
    chatter_memory._get_condensation_candidates(
        cursor, 1, 2, {},
    )
    assert 'id != (SELECT id FROM (' in cursor.last_query


def test_candidates_max_candidates_defaults_when_unset():
    cursor = _CandidatesCursor(rows=[])
    chatter_memory._get_condensation_candidates(
        cursor, 1, 2, {},
    )
    assert (
        cursor.last_params[-1]
        == chatter_memory.DEFAULT_CONDENSATION_MAX_CANDIDATES
    )


def test_candidates_max_candidates_clamped_when_non_positive():
    cursor = _CandidatesCursor(rows=[])
    config = {
        'LLMChatter.Memory.Condensation.MaxCandidates': 0,
    }
    chatter_memory._get_condensation_candidates(
        cursor, 1, 2, config,
    )
    assert (
        cursor.last_params[-1]
        == chatter_memory.DEFAULT_CONDENSATION_MAX_CANDIDATES
    )


# ============================================================
# _cap_candidates_by_chars (prompt input-size guard)
# ============================================================

def test_cap_candidates_by_chars_trims_once_budget_exceeded():
    rows = [
        {'memory': 'a' * 100},
        {'memory': 'b' * 100},
        {'memory': 'c' * 100},
    ]
    kept = chatter_memory._cap_candidates_by_chars(rows, 150)
    assert kept == rows[:1]


def test_cap_candidates_by_chars_keeps_everything_within_budget():
    rows = [{'memory': 'x' * 10} for _ in range(5)]
    kept = chatter_memory._cap_candidates_by_chars(rows, 1000)
    assert kept == rows


def test_cap_candidates_by_chars_always_keeps_first_row():
    rows = [{'memory': 'z' * 10000}]
    kept = chatter_memory._cap_candidates_by_chars(rows, 100)
    assert kept == rows


# ============================================================
# _condense_low_value_memories: digest clamping + atomicity
# ============================================================

def _base_config(**overrides):
    config = {
        'LLMChatter.Memory.Condensation.MinCandidates': 2,
        'LLMChatter.Memory.Condensation.MaxDigests': 2,
        'LLMChatter.Memory.Condensation.MaxCandidates': 8,
        'LLMChatter.Memory.Condensation.ProtectFloor': 7,
    }
    config.update(overrides)
    return config


def test_digest_importance_clamped_to_max_source_importance():
    rows = [
        _row(1, 'Saw a rare bird.', 2),
        _row(2, 'Chatted about the weather.', 3),
        _row(3, 'Helped carry ore.', 6),
    ]
    db = _CondenseDb(candidate_rows=rows)
    response = json.dumps({
        'digests': [
            {
                'memory': 'A quiet stretch of small moments.',
                'importance': 9,  # above max source (6)
                'mood': 'wistful',
            },
        ],
    })

    with (
        patch.object(
            chatter_memory, 'get_db_connection',
            return_value=db,
        ),
        patch.object(
            chatter_memory, 'get_llm_client',
            return_value=object(),
        ),
        patch.object(
            chatter_memory, 'call_llm',
            return_value=response,
        ),
    ):
        result = chatter_memory._condense_low_value_memories(
            _base_config(), bot_guid=1283000001,
            player_guid=1283000099,
        )

    assert result is True
    inserts = [
        params for query, params in db.executed
        if query.startswith("INSERT INTO llm_bot_memories")
    ]
    assert len(inserts) == 1
    # importance_score is the second-to-last bound param
    # (..., importance_score, zone_id).
    assert inserts[0][-2] == 6  # clamped down to max source


def test_digest_importance_kept_when_below_max_source():
    rows = [
        _row(1, 'Saw a rare bird.', 2),
        _row(2, 'Chatted about the weather.', 3),
        _row(3, 'Helped carry ore.', 6),
    ]
    db = _CondenseDb(candidate_rows=rows)
    response = json.dumps({
        'digests': [
            {
                'memory': 'A quiet stretch of small moments.',
                'importance': 4,  # below max source (6)
                'mood': 'wistful',
            },
        ],
    })

    with (
        patch.object(
            chatter_memory, 'get_db_connection',
            return_value=db,
        ),
        patch.object(
            chatter_memory, 'get_llm_client',
            return_value=object(),
        ),
        patch.object(
            chatter_memory, 'call_llm',
            return_value=response,
        ),
    ):
        chatter_memory._condense_low_value_memories(
            _base_config(), bot_guid=1283000001,
            player_guid=1283000099,
        )

    inserts = [
        params for query, params in db.executed
        if query.startswith("INSERT INTO llm_bot_memories")
    ]
    assert inserts[0][-2] == 4  # untouched, already lower


def test_condensation_commits_once_after_inserts_and_delete():
    rows = [
        _row(1, 'Saw a rare bird.', 2),
        _row(2, 'Chatted about the weather.', 3),
        _row(3, 'Helped carry ore.', 4),
    ]
    db = _CondenseDb(candidate_rows=rows)
    response = json.dumps({
        'digests': [
            {'memory': 'Folded memory.', 'importance': 3},
        ],
    })

    with (
        patch.object(
            chatter_memory, 'get_db_connection',
            return_value=db,
        ),
        patch.object(
            chatter_memory, 'get_llm_client',
            return_value=object(),
        ),
        patch.object(
            chatter_memory, 'call_llm',
            return_value=response,
        ),
    ):
        result = chatter_memory._condense_low_value_memories(
            _base_config(), bot_guid=1283000001,
            player_guid=1283000099,
        )

    assert result is True
    assert db.commits == 1
    queries = [q for q, _ in db.executed]
    insert_idx = next(
        i for i, q in enumerate(queries)
        if q.startswith("INSERT INTO llm_bot_memories")
    )
    delete_idx = next(
        i for i, q in enumerate(queries)
        if q.startswith("DELETE FROM llm_bot_memories")
    )
    assert insert_idx < delete_idx
    delete_params = db.executed[delete_idx][1]
    assert set(delete_params) == {1, 2, 3}


def test_condensation_skips_when_below_min_candidates():
    rows = [_row(1, 'Only one.', 2)]
    db = _CondenseDb(candidate_rows=rows)

    with (
        patch.object(
            chatter_memory, 'get_db_connection',
            return_value=db,
        ),
        patch.object(
            chatter_memory, 'call_llm',
        ) as call,
    ):
        result = chatter_memory._condense_low_value_memories(
            _base_config(
                **{
                    'LLMChatter.Memory.Condensation'
                    '.MinCandidates': 4,
                }
            ),
            bot_guid=1283000001, player_guid=1283000099,
        )

    assert result is False
    call.assert_not_called()
    assert len(db.executed) == 1
    assert db.executed[0][0].startswith(
        "SELECT id, group_id, memory, importance_score,"
    )
    assert db.commits == 0


def test_condensation_leaves_sources_untouched_on_bad_response():
    rows = [
        _row(1, 'Saw a rare bird.', 2),
        _row(2, 'Chatted about the weather.', 3),
        _row(3, 'Helped carry ore.', 4),
    ]
    db = _CondenseDb(candidate_rows=rows)

    with (
        patch.object(
            chatter_memory, 'get_db_connection',
            return_value=db,
        ),
        patch.object(
            chatter_memory, 'get_llm_client',
            return_value=object(),
        ),
        patch.object(
            chatter_memory, 'call_llm',
            return_value='not json at all',
        ),
    ):
        result = chatter_memory._condense_low_value_memories(
            _base_config(), bot_guid=1283000001,
            player_guid=1283000099,
        )

    assert result is False
    assert db.commits == 0
    inserts_or_deletes = [
        q for q, _ in db.executed
        if not q.startswith(
            "SELECT id, group_id, memory, importance_score,"
        )
    ]
    assert inserts_or_deletes == []


# ============================================================
# _maybe_trigger_condensation: submit() failure guard
# ============================================================

def test_submit_failure_clears_guard_and_does_not_raise():
    class _FailingExecutor:
        def submit(self, *args, **kwargs):
            raise RuntimeError("executor is shutting down")

    pair = (555000001, 555000002)
    chatter_memory._condensing_pairs.discard(pair)
    config = {
        'LLMChatter.Memory.Condensation.Enable': 1,
        'LLMChatter.Memory.Condensation.TriggerPercent': 50,
    }

    with patch.object(
        chatter_memory, 'condensation_executor',
        _FailingExecutor(),
    ):
        # Must not raise even though submit() does.
        chatter_memory._maybe_trigger_condensation(
            config, bot_guid=pair[0], player_guid=pair[1],
            active_count=10, max_per=10,
        )

    assert pair not in chatter_memory._condensing_pairs


def test_successful_submit_leaves_pair_marked_in_flight():
    class _RecordingExecutor:
        def __init__(self):
            self.submitted = []

        def submit(self, fn, *args, **kwargs):
            self.submitted.append((fn, args, kwargs))
            return object()

    pair = (555000003, 555000004)
    chatter_memory._condensing_pairs.discard(pair)
    executor = _RecordingExecutor()
    config = {
        'LLMChatter.Memory.Condensation.Enable': 1,
        'LLMChatter.Memory.Condensation.TriggerPercent': 50,
    }

    try:
        with patch.object(
            chatter_memory, 'condensation_executor', executor,
        ):
            chatter_memory._maybe_trigger_condensation(
                config, bot_guid=pair[0], player_guid=pair[1],
                active_count=10, max_per=10,
            )
        assert len(executor.submitted) == 1
        assert pair in chatter_memory._condensing_pairs
    finally:
        chatter_memory._condensing_pairs.discard(pair)


if __name__ == '__main__':
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith('test_') and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"\n{len(tests)} tests passed")
