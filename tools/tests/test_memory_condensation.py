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
        # Real DBAPI cursors always expose rowcount; the
        # watermark-rewind UPDATE reads it to tell "rolled back"
        # from "no relationship row for this pair".
        self.rowcount = 0

    def execute(self, query, params=None):
        self.db.executed.append((query, params))
        if query.startswith(
            "SELECT id, group_id, memory, importance_score,"
        ):
            self._rows = self.db.candidate_rows
            self.rowcount = len(self._rows)
        elif query.startswith("UPDATE llm_bot_relationships"):
            self._rows = []
            # How many relationship rows the pair has that match
            # the UPDATE's own guards. Defaults to 0: the fake
            # pair has no relationship row, so no rewind.
            self.rowcount = self.db.relationship_update_rowcount
        else:
            self._rows = []
            self.rowcount = 0

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _CondenseDb:
    def __init__(
        self, candidate_rows, relationship_update_rowcount=0,
    ):
        self.candidate_rows = candidate_rows
        self.relationship_update_rowcount = (
            relationship_update_rowcount
        )
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
# _build_condensation_prompt: identity + gender rules
# ============================================================

def test_condensation_prompt_includes_bot_and_player_gender_rules():
    rows = [_row(1, 'Saw a rare bird.', 2)]
    prompt = chatter_memory._build_condensation_prompt(
        rows, 1,
        player_name='Di', player_gender='male',
        bot_name='Stella', bot_race='Blood Elf',
        bot_class='Priest', bot_gender='female',
    )
    assert 'You are Stella, a female Blood Elf Priest.' in prompt
    assert 'grammatically male' in prompt
    assert 'grammatically female' in prompt


def test_condensation_prompt_without_identity_has_no_gender_rules():
    rows = [_row(1, 'Saw a rare bird.', 2)]
    prompt = chatter_memory._build_condensation_prompt(
        rows, 1,
    )
    assert 'You are ' not in prompt
    assert 'grammatically' not in prompt


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
        # Real cursors always expose rowcount; the relationship
        # write path reads it to tell "watermark advanced" from
        # "someone moved it while the LLM call was in flight".
        self.rowcount = 0

    def execute(self, query, params=None):
        self.db.executed.append((query, params))
        self.rowcount = 0
        if query.startswith("SELECT summary,"):
            self._rows = (
                [dict(self.db.relationship)]
                if self.db.relationship is not None else []
            )
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
        elif query.startswith("UPDATE llm_bot_relationships"):
            self._rows = []
            self._apply_update(query, params)
        elif query.startswith("INSERT INTO llm_bot_relationships"):
            self._rows = []
            # Real ON DUPLICATE KEY UPDATE semantics: writes
            # unconditionally, whatever the current watermark is.
            bot_guid, player_guid, summary, watermark = params
            if self.db.relationship is None:
                self.db.relationship = {}
            self.db.relationship['summary'] = summary
            self.db.relationship[
                'updated_through_created_at'
            ] = watermark
            self.rowcount = 1
        else:
            self._rows = []

    def _apply_update(self, query, params):
        """Apply the relationship UPDATEs with real MySQL
        semantics, so the WHERE guards are actually enforced
        (and a test can prove one is missing).
        """
        relationship = self.db.relationship
        if relationship is None:
            self.rowcount = 0  # UPDATE never creates a row
            return
        if 'updated_through_created_at <=> %s' in query:
            summary, watermark, _bot, _player, expected = params
            if (
                relationship['updated_through_created_at']
                != expected
            ):
                self.rowcount = 0  # moved underneath us
                return
            relationship['summary'] = summary
            relationship[
                'updated_through_created_at'
            ] = watermark
            self.rowcount = 1
            return
        # Summary-only fallback: never touches the watermark.
        assert 'updated_through_created_at' not in query
        relationship['summary'] = params[0]
        self.rowcount = 1

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
        # Tracks whether the read connection has been closed yet,
        # same convention as _CondenseDb.closed -- lets a test
        # observe that the read connection is gone before the LLM
        # call fires, without the fake having to actually enforce
        # closed-connection errors.
        self.closed = False

    def cursor(self, *args, **kwargs):
        return _RelationshipCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


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
    """Existing pair, nobody racing: the watermark advances to
    the newest memory folded in.
    """
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
        if q.startswith("UPDATE llm_bot_relationships")
    )
    assert 'updated_through_created_at = %s' in write[0]
    assert 'updated_through_memory_id' not in write[0]
    # (summary, new watermark, bot, player, watermark at read)
    assert write[1][1] == newest
    assert write[1][4] == t0
    assert db.relationship['updated_through_created_at'] == newest
    assert db.commits == 1


def test_first_relationship_for_a_pair_inserts():
    """No row at read time -> nothing to race against, so the
    plain upsert path still has to run (otherwise a pair would
    never get its first relationship row at all).
    """
    t0 = datetime.datetime(2026, 8, 1, 10, 0, 0)
    db = _RelationshipDb(
        memories=[_memory(7, 'a new memory', t0)],
        relationship=None,
        now_ts=t0 + datetime.timedelta(hours=1),
    )
    _run_relationship_update(db)
    write = next(
        (q, p) for q, p in db.executed
        if q.startswith("INSERT INTO llm_bot_relationships")
    )
    assert write[1][3] == t0
    assert db.relationship['updated_through_created_at'] == t0
    assert db.relationship['summary'] == 'They trust each other.'
    assert db.commits == 1


def test_relationship_read_connection_is_closed_before_the_llm_call():
    """Mirrors
    test_read_connection_is_closed_before_the_llm_call() in the
    condensation path (1b744f9): with autocommit off, holding
    the read connection open across the LLM round-trip would pin
    its read view for no reason. The read connection must be
    closed before call_llm() runs, and a second, fresh connection
    must be opened for the write.
    """
    t0 = datetime.datetime(2026, 8, 1, 10, 0, 0)
    newest = t0 + datetime.timedelta(days=1)
    db = _RelationshipDb(
        memories=[_memory(7, 'a new memory', newest)],
        relationship={
            'summary': '', 'updated_through_created_at': t0,
        },
        now_ts=newest + datetime.timedelta(hours=1),
    )
    state = {}

    def _fake_llm(client, prompt, *args, **kwargs):
        state['closed_at_llm_time'] = db.closed
        state['queries_at_llm_time'] = [
            q for q, _ in db.executed
        ]
        return json.dumps({'message': 'They trust each other.'})

    with (
        patch.object(
            chatter_memory, 'get_db_connection', return_value=db,
        ) as get_conn,
        patch.object(
            chatter_memory, 'get_llm_client',
            return_value=object(),
        ),
        patch.object(
            chatter_memory, 'call_llm', side_effect=_fake_llm,
        ),
    ):
        chatter_memory._maybe_update_relationship(
            {}, 1283000001, 1283000099,
        )

    assert state['closed_at_llm_time'] is True
    # Nothing written yet at LLM time -- the read phase only read.
    assert all(
        q.startswith('SELECT')
        for q in state['queries_at_llm_time']
    )
    # A second, fresh connection is opened for the write phase.
    assert get_conn.call_count == 2
    # And the write still actually landed on the (fake, shared)
    # connection returned by the second call.
    assert db.relationship['updated_through_created_at'] == newest
    assert db.commits == 1


def test_relationship_write_does_not_clobber_a_concurrent_rewind():
    """The race between the two fixes on this branch.

    _maybe_update_relationship() (relationship_executor) reads
    the watermark at T1 and then spends multiple seconds in an
    LLM call. Meanwhile _condense_low_value_memories()
    (condensation_executor -- _condensing_pairs excludes only a
    second CONDENSATION pass, not this one) commits
    _rewind_relationship_watermark() for the SAME pair, moving
    the watermark BACK to T0 so a freshly written digest gets
    re-folded.

    The relationship pass then wakes up holding a stale, higher
    watermark. If it writes that unconditionally, the rewind is
    undone and the digest's content is stranded forever -- its
    source rows are already deleted. So the write must lose the
    race, not win it.
    """
    t0 = datetime.datetime(2026, 8, 1, 10, 0, 0)   # rewind target
    t1 = t0 + datetime.timedelta(hours=2)          # read at
    t2 = t0 + datetime.timedelta(hours=5)          # would advance to
    db = _RelationshipDb(
        memories=[_memory(7, 'a new memory', t2)],
        relationship={
            'summary': 'Cordial.',
            'updated_through_created_at': t1,
        },
        now_ts=t2 + datetime.timedelta(hours=1),
    )

    def _rewind_during_the_llm_call(*args, **kwargs):
        # Condensation commits its rewind while we are blocked
        # on the model.
        db.relationship['updated_through_created_at'] = t0
        return json.dumps({'message': 'They trust each other.'})

    with (
        patch.object(
            chatter_memory, 'get_db_connection', return_value=db,
        ),
        patch.object(
            chatter_memory, 'get_llm_client', return_value=object(),
        ),
        patch.object(
            chatter_memory, 'call_llm',
            side_effect=_rewind_during_the_llm_call,
        ),
    ):
        chatter_memory._maybe_update_relationship(
            {}, 1283000001, 1283000099,
        )

    # The rewind stands. Not t2 (stale advance), and not
    # GREATEST(t0, t2) either -- that guard would be just as
    # wrong here, since the rewind is *meant* to go backward.
    assert db.relationship['updated_through_created_at'] == t0
    # The summary is still real LLM work, and re-folding
    # already-summarized material is harmless, so it is kept.
    assert db.relationship['summary'] == 'They trust each other.'
    # And the pass must not have fallen back to a blind upsert.
    assert not [
        q for q, _ in db.executed
        if q.startswith("INSERT INTO llm_bot_relationships")
    ]


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


# ============================================================
# Full-fidelity condensation prompt (sanitize truncation bug)
# ============================================================

def test_sanitize_defaults_to_the_short_prompt_cap():
    text = 'x' * 400
    out = chatter_memory.sanitize_memory_for_prompt(text)
    assert len(out) == chatter_memory.DEFAULT_PROMPT_MEMORY_CHARS
    assert out.endswith('...')


def test_sanitize_honours_an_explicit_max_chars():
    text = 'x' * 400
    out = chatter_memory.sanitize_memory_for_prompt(
        text, max_chars=chatter_memory.MEMORY_TEXT_MAX_CHARS,
    )
    # Well under the raised ceiling -> returned whole.
    assert out == text
    assert '...' not in out


def test_sanitize_still_strips_and_normalizes_with_max_chars():
    out = chatter_memory.sanitize_memory_for_prompt(
        "a\x00b\n  c", max_chars=500,
    )
    assert out == 'ab c'


def test_condensation_prompt_shows_the_whole_stored_memory():
    """The blocker: memories are stored up to
    MEMORY_TEXT_MAX_CHARS, the prompt truncated at 200, and
    _condense_low_value_memories() then DELETED the untruncated
    originals -- so everything past char 200 of a long memory was
    destroyed without the model ever seeing it.
    """
    long_tail = 'TAIL-MARKER-THAT-MUST-SURVIVE'
    long_memory = (
        'We fought through Blackrock Depths together. '
        + ('filler words here. ' * 20)
        + long_tail
    )
    assert len(long_memory) > 200
    assert len(long_memory) <= chatter_memory.MEMORY_TEXT_MAX_CHARS
    prompt = chatter_memory._build_condensation_prompt(
        [_row(1, long_memory, 2), _row(2, 'Short one.', 3)],
        max_digests=1,
    )
    assert long_tail in prompt
    assert '...' not in prompt.split('Memories:')[1][:len(long_memory) + 50]


def test_relationship_prompt_shows_the_whole_stored_memory():
    """Same truncation call in _maybe_update_relationship().
    Lower severity (no deletion), but the watermark advances past
    every row folded in, so a truncated tail is never summarized.
    """
    t0 = datetime.datetime(2026, 8, 1, 10, 0, 0)
    tail = 'RELATIONSHIP-TAIL-MARKER'
    long_memory = (
        'A long shared history. ' + ('and more. ' * 30) + tail
    )
    assert len(long_memory) > 200
    db = _RelationshipDb(
        memories=[_memory(1, long_memory, t0 + datetime.timedelta(
            days=1,
        ))],
        relationship={
            'summary': '', 'updated_through_created_at': t0,
        },
        now_ts=t0 + datetime.timedelta(days=2),
    )
    seen = {}
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
            side_effect=lambda client, prompt, *a, **k: (
                seen.setdefault('prompt', prompt),
                json.dumps({'message': 'They trust each other.'}),
            )[1],
        ),
    ):
        chatter_memory._maybe_update_relationship(
            {}, 1283000001, 1283000099,
        )
    assert tail in seen['prompt']


# ============================================================
# MaxDigests is clamped against MinCandidates so a pass can
# never net-grow the memory pool past MaxPerBotPlayer
# ============================================================

def _max_digests_in_prompt(**overrides):
    seen = {}
    rows = [_row(i, f'memory {i}', 2) for i in range(1, 7)]
    db = _CondenseDb(candidate_rows=rows)
    with (
        patch.object(
            chatter_memory, 'get_db_connection', return_value=db,
        ),
        patch.object(
            chatter_memory, 'get_llm_client', return_value=object(),
        ),
        patch.object(
            chatter_memory, 'call_llm',
            side_effect=lambda client, prompt, *a, **k: (
                seen.setdefault('prompt', prompt),
                _ONE_DIGEST,
            )[1],
        ),
    ):
        chatter_memory._condense_low_value_memories(
            _base_config(**overrides),
            bot_guid=1283000001, player_guid=1283000099,
        )
    prompt = seen['prompt']
    marker = 'Fold them into 1-'
    start = prompt.index(marker) + len(marker)
    return int(prompt[start:prompt.index(' ', start)])


def test_max_digests_clamped_below_min_candidates():
    # MaxDigests 5 with MinCandidates 4 would let a pass swap 4
    # rows for 5 -- a net gain. Clamp to MinCandidates - 1.
    assert _max_digests_in_prompt(**{
        'LLMChatter.Memory.Condensation.MinCandidates': 4,
        'LLMChatter.Memory.Condensation.MaxDigests': 5,
    }) == 3


def test_max_digests_equal_to_min_candidates_is_clamped():
    # Equal is still not a reduction: 4 rows in, 4 digests out.
    assert _max_digests_in_prompt(**{
        'LLMChatter.Memory.Condensation.MinCandidates': 4,
        'LLMChatter.Memory.Condensation.MaxDigests': 4,
    }) == 3


def test_max_digests_left_alone_when_already_safe():
    assert _max_digests_in_prompt(**{
        'LLMChatter.Memory.Condensation.MinCandidates': 5,
        'LLMChatter.Memory.Condensation.MaxDigests': 2,
    }) == 2


def test_non_positive_max_digests_falls_back_to_default():
    assert _max_digests_in_prompt(**{
        'LLMChatter.Memory.Condensation.MinCandidates': 6,
        'LLMChatter.Memory.Condensation.MaxDigests': 0,
    }) == chatter_memory.DEFAULT_CONDENSATION_MAX_DIGESTS


def test_non_positive_min_candidates_falls_back_to_default():
    # MinCandidates 0/1 would make "condensation" a 1-row rewrite
    # and leave MaxDigests unclamped.
    assert _max_digests_in_prompt(**{
        'LLMChatter.Memory.Condensation.MinCandidates': 1,
        'LLMChatter.Memory.Condensation.MaxDigests': 9,
    }) == chatter_memory.DEFAULT_CONDENSATION_MIN_CANDIDATES - 1


# ============================================================
# No open transaction across the LLM call
# ============================================================

def test_read_connection_is_closed_before_the_llm_call():
    """With autocommit off, holding the candidate SELECT's
    connection open across the LLM round-trip keeps its read view
    (and InnoDB purge) pinned for seconds.
    """
    rows = [_row(i, f'memory {i}', 2) for i in range(1, 4)]
    db = _CondenseDb(candidate_rows=rows)
    state = {}

    def _fake_llm(client, prompt, *args, **kwargs):
        state['closed_at_llm_time'] = db.closed
        state['queries_at_llm_time'] = [q for q, _ in db.executed]
        return _ONE_DIGEST

    with (
        patch.object(
            chatter_memory, 'get_db_connection', return_value=db,
        ) as get_conn,
        patch.object(
            chatter_memory, 'get_llm_client', return_value=object(),
        ),
        patch.object(
            chatter_memory, 'call_llm', side_effect=_fake_llm,
        ),
    ):
        result = chatter_memory._condense_low_value_memories(
            _base_config(), bot_guid=1283000001,
            player_guid=1283000099,
        )

    assert result is True
    assert state['closed_at_llm_time'] is True
    # Nothing written yet at LLM time -- the read phase only read.
    assert all(
        q.startswith('SELECT')
        for q in state['queries_at_llm_time']
    )
    # A second, fresh connection is opened for the write phase.
    assert get_conn.call_count == 2


# ============================================================
# Condensation must not strand memories behind the relationship
# watermark
# ============================================================

def _rewind_update(db):
    return next(
        (
            (q, p) for q, p in db.executed
            if q.startswith("UPDATE llm_bot_relationships")
        ),
        None,
    )


def _condense_with_timestamps(created_ats, rowcount):
    rows = [
        _row(i + 1, f'memory {i}', 2, created_at=ca)
        for i, ca in enumerate(created_ats)
    ]
    db = _CondenseDb(
        candidate_rows=rows,
        relationship_update_rowcount=rowcount,
    )
    with (
        patch.object(
            chatter_memory, 'get_db_connection', return_value=db,
        ),
        patch.object(
            chatter_memory, 'get_llm_client', return_value=object(),
        ),
        patch.object(
            chatter_memory, 'call_llm', return_value=_ONE_DIGEST,
        ),
    ):
        result = chatter_memory._condense_low_value_memories(
            _base_config(), bot_guid=1283000001,
            player_guid=1283000099,
        )
    return db, result


def test_condensation_rewinds_the_relationship_watermark():
    """The blocker: a digest inherits its OLDEST source's
    created_at, but the relationship pass selects with
    `created_at > watermark`. Folding a source NEWER than the
    watermark into an older-stamped digest hides that content
    from relationship tracking permanently -- the sources are
    deleted and the digest sorts behind the watermark forever.
    """
    oldest = datetime.datetime(2026, 8, 1, 10, 0, 0)
    newest = datetime.datetime(2026, 8, 3, 10, 0, 0)
    db, result = _condense_with_timestamps(
        [oldest, datetime.datetime(2026, 8, 2, 10, 0, 0), newest],
        rowcount=1,
    )
    assert result is True
    update = _rewind_update(db)
    assert update is not None, [q for q, _ in db.executed]
    query, params = update
    # Rewound to one second before the digest's created_at, so the
    # next relationship pass re-folds the digest.
    assert params[0] == oldest - datetime.timedelta(seconds=1)
    assert params[1] == 1283000001
    assert params[2] == 1283000099
    # Only fires when a source really outran the watermark...
    assert params[3] == newest
    # ...and only ever rewinds, never advances.
    assert params[4] == oldest - datetime.timedelta(seconds=1)
    assert 'updated_through_created_at < %s' in query
    assert 'updated_through_created_at > %s' in query


def test_rewind_shares_the_condensation_transaction():
    """A rewind committed separately from the insert+delete could
    be lost (or applied alone) on a crash between them."""
    oldest = datetime.datetime(2026, 8, 1, 10, 0, 0)
    db, result = _condense_with_timestamps(
        [oldest, datetime.datetime(2026, 8, 3, 10, 0, 0)],
        rowcount=1,
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
    update_idx = next(
        i for i, q in enumerate(queries)
        if q.startswith("UPDATE llm_bot_relationships")
    )
    assert insert_idx < delete_idx < update_idx


def test_rewind_uses_update_so_it_never_creates_a_relationship():
    """A pair with no relationship row must stay without one --
    the relationship pass creates it on its own terms."""
    oldest = datetime.datetime(2026, 8, 1, 10, 0, 0)
    db, result = _condense_with_timestamps(
        [oldest, datetime.datetime(2026, 8, 3, 10, 0, 0)],
        rowcount=0,
    )
    assert result is True
    update = _rewind_update(db)
    assert update is not None
    assert update[0].startswith("UPDATE llm_bot_relationships")
    assert 'INSERT' not in update[0]
    assert not [
        q for q, _ in db.executed
        if q.startswith("INSERT INTO llm_bot_relationships")
    ]


def test_rewind_skipped_without_source_timestamps():
    # Rows with no created_at at all (a caller/fake that never
    # selected the column) -- _row() substitutes a default, so
    # build them by hand.
    rows = [
        {
            'id': i, 'group_id': 41, 'memory': f'memory {i}',
            'importance_score': 2, 'effective_score': 2,
            'condensation_generation': 0,
        }
        for i in (1, 2)
    ]
    db = _CondenseDb(
        candidate_rows=rows, relationship_update_rowcount=1,
    )
    with (
        patch.object(
            chatter_memory, 'get_db_connection', return_value=db,
        ),
        patch.object(
            chatter_memory, 'get_llm_client', return_value=object(),
        ),
        patch.object(
            chatter_memory, 'call_llm', return_value=_ONE_DIGEST,
        ),
    ):
        chatter_memory._condense_low_value_memories(
            _base_config(), bot_guid=1283000001,
            player_guid=1283000099,
        )
    assert _rewind_update(db) is None


if __name__ == '__main__':
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith('test_') and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"\n{len(tests)} tests passed")
