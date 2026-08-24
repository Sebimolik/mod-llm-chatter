#!/usr/bin/env python3
"""Focused checks for low-value-memory condensation.

Run directly from the module root:
  python tools/tests/test_memory_condensation.py
"""

import datetime
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


_BASE_TIME = datetime.datetime(2026, 8, 1, 12, 0, 0)


def _row(
    row_id, memory, importance, group_id=41,
    created_at=None, generation=0,
):
    return {
        'id': row_id,
        'group_id': group_id,
        'memory': memory,
        'importance_score': importance,
        'effective_score': importance,
        'created_at': (
            created_at
            if created_at is not None
            else _BASE_TIME + datetime.timedelta(days=row_id)
        ),
        'condensation_generation': generation,
    }


def _insert_fields(query, params):
    """Map an INSERT's bound params onto its column names, so
    assertions don't depend on positional indexes that shift
    every time a column is added.
    """
    columns = [
        col.strip().strip('`')
        for col in query[
            query.index('(') + 1:query.index(')')
        ].split(',')
    ]
    return dict(zip(columns, params))


def _digest_inserts(db):
    return [
        _insert_fields(query, params)
        for query, params in db.executed
        if query.startswith("INSERT INTO llm_bot_memories")
    ]


def _insert_idx(db):
    return next(
        i for i, (query, _) in enumerate(db.executed)
        if query.startswith("INSERT INTO llm_bot_memories")
    )


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
    # (bot_guid, player_guid, protect_floor, max_generations,
    #  bot_guid, player_guid, max_candidates)
    assert cursor.last_params == (100, 200, 6, 2, 100, 200, 3)


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
    assert _insert_fields(*db.executed[_insert_idx(db)])[
        'importance_score'
    ] == 6  # clamped down to max source


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
    assert _insert_fields(*db.executed[_insert_idx(db)])[
        'importance_score'
    ] == 4  # untouched, already lower


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
        if not q.startswith("SELECT ")
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


# ============================================================
# Digest lifecycle: created_at inheritance, used flag,
# generation counter (Fix 2)
# ============================================================

def _condense(rows, response, **config_overrides):
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
            return_value=response,
        ),
    ):
        result = chatter_memory._condense_low_value_memories(
            _base_config(**config_overrides),
            bot_guid=1283000001, player_guid=1283000099,
        )
    return db, result


_ONE_DIGEST = json.dumps({
    'digests': [
        {'memory': 'Folded memory.', 'importance': 3},
    ],
})


def test_digest_inherits_oldest_source_created_at():
    oldest = datetime.datetime(2026, 6, 1, 8, 30, 0)
    rows = [
        _row(1, 'Older.', 2, created_at=oldest),
        _row(2, 'Newer.', 3, created_at=datetime.datetime(
            2026, 7, 4, 9, 0, 0,
        )),
        _row(3, 'Newest.', 3, created_at=datetime.datetime(
            2026, 8, 9, 10, 0, 0,
        )),
    ]
    db, result = _condense(rows, _ONE_DIGEST)
    assert result is True
    fields = _digest_inserts(db)[0]
    # Not NOW(): the digest keeps decaying on the same clock its
    # sources were on, so it can never outrank them.
    assert fields['created_at'] == oldest


def test_digest_created_at_column_is_bound_not_now():
    rows = [
        _row(1, 'a', 2), _row(2, 'b', 2), _row(3, 'c', 2),
    ]
    db, _ = _condense(rows, _ONE_DIGEST)
    query = db.executed[_insert_idx(db)][0]
    assert 'created_at' in query
    assert 'NOW()' not in query


def test_digest_falls_back_to_now_without_source_timestamps():
    rows = []
    for row_id in (1, 2, 3):
        row = _row(row_id, f'mem {row_id}', 2)
        row['created_at'] = None
        rows.append(row)
    db, result = _condense(rows, _ONE_DIGEST)
    assert result is True
    assert 'NOW()' in db.executed[_insert_idx(db)][0]


def test_digest_inserted_used_so_it_participates_in_eviction():
    rows = [
        _row(1, 'a', 2), _row(2, 'b', 2), _row(3, 'c', 2),
    ]
    db, _ = _condense(rows, _ONE_DIGEST)
    # used=0 would make digests the last rows _evict_one_used()
    # is ever willing to delete.
    assert _digest_inserts(db)[0]['used'] == 1


def test_digest_generation_is_max_source_plus_one():
    rows = [
        _row(1, 'a', 2, generation=0),
        _row(2, 'b', 2, generation=1),
        _row(3, 'c', 2, generation=0),
    ]
    db, _ = _condense(rows, _ONE_DIGEST)
    assert (
        _digest_inserts(db)[0]['condensation_generation'] == 2
    )


def test_digest_generation_starts_at_one_for_fresh_memories():
    rows = [
        _row(1, 'a', 2), _row(2, 'b', 2), _row(3, 'c', 2),
    ]
    db, _ = _condense(rows, _ONE_DIGEST)
    assert (
        _digest_inserts(db)[0]['condensation_generation'] == 1
    )


def test_candidates_query_caps_generations():
    cursor = _CandidatesCursor(rows=[])
    chatter_memory._get_condensation_candidates(
        cursor, 1, 2,
        {'LLMChatter.Memory.Condensation.MaxGenerations': 3},
    )
    assert 'condensation_generation < %s' in cursor.last_query
    assert cursor.last_params[3] == 3


def test_max_generations_defaults_and_clamps():
    for config, expected in (
        ({}, chatter_memory
            .DEFAULT_CONDENSATION_MAX_GENERATIONS),
        ({'LLMChatter.Memory.Condensation.MaxGenerations': 0},
         chatter_memory
            .DEFAULT_CONDENSATION_MAX_GENERATIONS),
    ):
        cursor = _CandidatesCursor(rows=[])
        chatter_memory._get_condensation_candidates(
            cursor, 1, 2, config,
        )
        assert cursor.last_params[3] == expected


# ============================================================
# Relationship watermark is a timestamp (Fix 1), and a digest
# of already-summarized memories does not replay into it
# ============================================================

class _RelationshipCursor:
    """Fake dictionary cursor that actually applies the
    `created_at > watermark` filter, so the tests below exercise
    the real selection semantics rather than a canned row set.
    """

    def __init__(self, db):
        self.db = db
        self._rows = []

    def execute(self, query, params=None):
        self.db.executed.append((query, params))
        if query.startswith("SELECT summary,"):
            self._rows = [dict(self.db.relationship)]
        elif query.startswith("SELECT NOW()"):
            self._rows = [{
                'now_ts': self.db.now_ts,
                'newest_memory_at': max(
                    (m['created_at'] for m in self.db.memories),
                    default=None,
                ),
            }]
        elif query.startswith("SELECT id, memory, created_at"):
            watermark = params[2]
            self._rows = sorted(
                (
                    dict(m) for m in self.db.memories
                    if m['created_at'] > watermark
                ),
                key=lambda m: (m['created_at'], m['id']),
            )
        elif query.startswith("SELECT guid, name"):
            self._rows = [
                {'guid': params[0], 'name': 'Bottington'},
                {'guid': params[1], 'name': 'Playerella'},
            ]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _RelationshipDb:
    def __init__(self, memories, relationship, now_ts):
        self.memories = memories
        self.relationship = relationship
        self.now_ts = now_ts
        self.executed = []
        self.commits = 0

    def cursor(self, *args, **kwargs):
        return _RelationshipCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def _memory(row_id, text, created_at):
    return {
        'id': row_id, 'memory': text, 'created_at': created_at,
    }


def _run_relationship_update(db):
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
            return_value=json.dumps(
                {'message': 'They trust each other.'}
            ),
        ),
    ):
        chatter_memory._maybe_update_relationship(
            {}, 1283000001, 1283000099,
        )


def _folded_memories(db):
    """Texts the relationship prompt actually folded in."""
    for query, params in db.executed:
        if query.startswith("SELECT id, memory, created_at"):
            watermark = params[2]
            return [
                m['memory'] for m in db.memories
                if m['created_at'] > watermark
            ]
    return []


def test_relationship_selects_by_created_at_not_id():
    t0 = datetime.datetime(2026, 8, 1, 10, 0, 0)
    db = _RelationshipDb(
        memories=[
            _memory(1, 'old one', t0),
            _memory(2, 'new one', t0 + datetime.timedelta(
                days=1,
            )),
        ],
        relationship={
            'summary': 'Cordial.',
            'updated_through_created_at': t0,
        },
        now_ts=t0 + datetime.timedelta(days=2),
    )
    _run_relationship_update(db)
    select = next(
        (q, p) for q, p in db.executed
        if q.startswith("SELECT id, memory, created_at")
    )
    assert 'created_at > %s' in select[0]
    assert 'id > %s' not in select[0]
    assert select[1][2] == t0
    assert _folded_memories(db) == ['new one']


def test_relationship_writes_timestamp_watermark():
    t0 = datetime.datetime(2026, 8, 1, 10, 0, 0)
    newest = t0 + datetime.timedelta(days=1)
    db = _RelationshipDb(
        memories=[_memory(7, 'a new memory', newest)],
        relationship={
            'summary': '', 'updated_through_created_at': t0,
        },
        now_ts=newest + datetime.timedelta(hours=1),
    )
    _run_relationship_update(db)
    write = next(
        (q, p) for q, p in db.executed
        if q.startswith("INSERT INTO llm_bot_relationships")
    )
    assert 'updated_through_created_at' in write[0]
    assert 'updated_through_memory_id' not in write[0]
    # (bot, player, summary, timestamp watermark)
    assert write[1][3] == newest
    assert db.commits == 1


def test_digest_of_summarized_memories_is_not_replayed():
    """The Fix1 + Fix2 interaction, in one test.

    Three memories are summarized (watermark = newest of them),
    then condensed away into a digest that inherits the OLDEST
    source's created_at and is re-inserted with a much higher
    id. Under the old id watermark that digest looked new; under
    the timestamp watermark it correctly does not.
    """
    t0 = datetime.datetime(2026, 8, 1, 10, 0, 0)
    sources = [
        _memory(1, 'ran Deadmines', t0),
        _memory(2, 'shared a drink', t0 + datetime.timedelta(
            hours=1,
        )),
        _memory(3, 'ganked in Redridge', t0 +
                datetime.timedelta(hours=2)),
    ]
    watermark = sources[-1]['created_at']

    # Condensation: sources gone, digest inserted with a fresh
    # (much higher) id but the oldest source's created_at.
    digest = _memory(999, 'we adventured together', t0)
    db = _RelationshipDb(
        memories=[digest],
        relationship={
            'summary': 'Old friends.',
            'updated_through_created_at': watermark,
        },
        now_ts=t0 + datetime.timedelta(days=1),
    )
    with patch.object(chatter_memory.logger, 'warning') as warn:
        _run_relationship_update(db)

    assert _folded_memories(db) == []
    # A watermark newer than every surviving memory is the
    # normal post-condensation state, not a fault -- the sanity
    # guard must not reset it (which would re-fold the digest).
    assert not warn.called
    # Nothing new -> no LLM write-back at all.
    assert not [
        q for q, _ in db.executed
        if q.startswith("INSERT INTO llm_bot_relationships")
    ]
    assert digest['id'] > max(m['id'] for m in sources)


def test_implausible_watermark_resets_to_epoch_with_warning():
    t0 = datetime.datetime(2026, 8, 1, 10, 0, 0)
    db = _RelationshipDb(
        memories=[_memory(1, 'a memory', t0)],
        relationship={
            'summary': 'Cordial.',
            # Watermark far ahead of both NOW() and the newest
            # memory -- e.g. after a restore.
            'updated_through_created_at':
                t0 + datetime.timedelta(days=3650),
        },
        now_ts=t0 + datetime.timedelta(hours=1),
    )
    with patch.object(chatter_memory.logger, 'warning') as warn:
        _run_relationship_update(db)
    assert warn.called
    # Recovered rather than silently stalled forever.
    assert _folded_memories(db) == ['a memory']


if __name__ == '__main__':
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith('test_') and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"\n{len(tests)} tests passed")
