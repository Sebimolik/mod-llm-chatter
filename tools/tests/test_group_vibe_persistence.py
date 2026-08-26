#!/usr/bin/env python3
"""Focused checks for llm_group_vibe persistence.

Covers the DB helpers (upsert/get/delete), the lazy-expiry
and DB-fallback branches of get_session_vibe(), the
compare-and-delete race guard, and the unconditional write
from _ensure_cap_and_insert().

Everything runs against an in-memory fake of the
llm_group_vibe table -- no real database, no real memory or
group rows are ever touched.

Run directly from the module root:
  python tools/tests/test_group_vibe_persistence.py
"""

import importlib
import logging
import re
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

import chatter_db  # noqa: E402
import chatter_memory  # noqa: E402

# Synthetic ids only; nothing here reaches a real database.
GROUP = 999999101
BOT = 999999201
PLAYER = 999999301

CONFIG = {'LLMChatter.GroupChatter.VibeDurationSeconds': 600}

_INSERT_RE = re.compile(
    r"INSERT INTO llm_group_vibe\s*\(([^)]*)\)"
    r"\s*VALUES\s*\(([^)]*)\)",
    re.I,
)
_SELECT_RE = re.compile(
    r"SELECT\s+(.*?)\s+FROM llm_group_vibe", re.I
)
_DELETE_RE = re.compile(
    r"DELETE FROM llm_group_vibe\s+WHERE\s+(.*)", re.I
)


class _VibeCursor:
    """Executes the three llm_group_vibe statements against
    a dict standing in for the table.

    The INSERT column list is parsed out of the SQL rather
    than assumed, so a column/parameter mismatch (or a
    resurrected `mood` column) shows up as a test failure.
    """

    def __init__(self, conn):
        self._conn = conn
        self._result = None
        self.rowcount = -1

    def execute(self, query, params=None):
        flat = ' '.join(query.split())
        params = tuple(params or ())
        self._conn.executed.append((flat, params))
        table = self._conn.table

        insert = _INSERT_RE.search(flat)
        if insert:
            cols = [
                c.strip().strip('`')
                for c in insert.group(1).split(',')
            ]
            vals = [
                v.strip() for v in insert.group(2).split(',')
            ]
            assert len(cols) == len(vals), flat
            row = {}
            idx = 0
            for col, val in zip(cols, vals):
                if val == '%s':
                    assert idx < len(params), flat
                    row[col] = params[idx]
                    idx += 1
                else:
                    row[col] = val.upper()
            assert idx == len(params), (flat, params)
            for col in cols:
                assert col in _COLUMNS, (col, flat)
            gid = row.pop('group_id')
            if gid in table:
                table[gid].update(row)
            else:
                table[gid] = row
            self.rowcount = 1
            return

        select = _SELECT_RE.search(flat)
        if select:
            cols = [
                c.strip() for c in select.group(1).split(',')
            ]
            for col in cols:
                assert col in _COLUMNS, (col, flat)
            row = table.get(params[0])
            self._result = (
                None if row is None
                else tuple(row[c] for c in cols)
            )
            return

        delete = _DELETE_RE.search(flat)
        if delete:
            where = delete.group(1)
            assert 'group_id = %s' in where, flat
            gid = params[0]
            row = table.get(gid)
            if row is None:
                self.rowcount = 0
                return
            if 'set_at = %s' in where:
                # The compare-and-delete race guard: only
                # remove the row we actually observed.
                if row.get('set_at') != params[1]:
                    self.rowcount = 0
                    return
            del table[gid]
            self.rowcount = 1
            return

        raise AssertionError("unexpected statement: " + flat)

    def fetchone(self):
        return self._result

    def fetchall(self):
        return [] if self._result is None else [self._result]

    def close(self):
        pass


_COLUMNS = {
    'group_id', 'vibe', 'importance', 'set_at', 'updated_at',
}


class _VibeConn:
    def __init__(self, table):
        self.table = table
        self.executed = []
        self.commits = 0
        self.closed = 0

    def cursor(self, dictionary=False):
        return _VibeCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed += 1


class _Connector:
    """Stands in for get_db_connection(), counting how many
    connections the code under test actually opens."""

    def __init__(self, table):
        self.table = table
        self.opened = []

    def __call__(self, config=None):
        conn = _VibeConn(self.table)
        self.opened.append(conn)
        return conn


def _fresh():
    table = {}
    return table, _Connector(table)


# ---------------------------------------------------------
# DB helpers
# ---------------------------------------------------------

def test_upsert_then_read_round_trip():
    table, connector = _fresh()
    with patch.object(
        chatter_db, 'get_db_connection', connector
    ), patch.object(chatter_db.time, 'time', lambda: 1000.0):
        chatter_db.upsert_group_vibe(
            CONFIG, GROUP, 'triumphant', 8,
        )
        row = chatter_db.get_group_vibe(CONFIG, GROUP)
    assert row == ('triumphant', 1000)
    assert table[GROUP]['importance'] == 8
    # Both helpers owned their connection and closed it.
    assert len(connector.opened) == 2
    assert all(c.closed == 1 for c in connector.opened)


def test_get_group_vibe_missing_row_returns_none():
    table, connector = _fresh()
    with patch.object(
        chatter_db, 'get_db_connection', connector
    ):
        assert chatter_db.get_group_vibe(
            CONFIG, GROUP
        ) is None


def test_upsert_overwrites_the_existing_row():
    table, connector = _fresh()
    clock = [1000.0]
    with patch.object(
        chatter_db, 'get_db_connection', connector
    ), patch.object(
        chatter_db.time, 'time', lambda: clock[0]
    ):
        chatter_db.upsert_group_vibe(
            CONFIG, GROUP, 'grim', 6,
        )
        clock[0] = 1500.0
        chatter_db.upsert_group_vibe(
            CONFIG, GROUP, 'triumphant', 9,
        )
        row = chatter_db.get_group_vibe(CONFIG, GROUP)
    # ON DUPLICATE KEY UPDATE: one row, refreshed in place.
    assert list(table) == [GROUP]
    assert row == ('triumphant', 1500)
    assert table[GROUP]['importance'] == 9


def test_upsert_writes_no_mood_column():
    table, connector = _fresh()
    with patch.object(
        chatter_db, 'get_db_connection', connector
    ):
        chatter_db.upsert_group_vibe(
            CONFIG, GROUP, 'wary', 5,
        )
    assert 'mood' not in table[GROUP]
    statement = connector.opened[0].executed[0][0].lower()
    assert 'mood' not in statement


def test_upsert_reuses_a_caller_owned_connection():
    table, connector = _fresh()
    conn = _VibeConn(table)
    with patch.object(
        chatter_db, 'get_db_connection', connector
    ):
        chatter_db.upsert_group_vibe(
            CONFIG, GROUP, 'wary', 5, conn=conn,
        )
    # No second connect, and the caller's connection is left
    # open for the caller to close.
    assert connector.opened == []
    assert conn.closed == 0
    assert conn.commits == 1
    assert table[GROUP]['vibe'] == 'wary'


def test_upsert_clamps_importance_and_skips_bad_group_ids():
    table, connector = _fresh()
    with patch.object(
        chatter_db, 'get_db_connection', connector
    ):
        chatter_db.upsert_group_vibe(
            CONFIG, GROUP, 'wary', 9999,
        )
        chatter_db.upsert_group_vibe(
            CONFIG, -1, 'wary', 5,
        )
        chatter_db.upsert_group_vibe(
            CONFIG, 0x100000000, 'wary', 5,
        )
    assert table[GROUP]['importance'] == 255
    assert list(table) == [GROUP]


def test_delete_guard_refuses_when_set_at_changed():
    table, connector = _fresh()
    with patch.object(
        chatter_db, 'get_db_connection', connector
    ), patch.object(chatter_db.time, 'time', lambda: 2000.0):
        chatter_db.upsert_group_vibe(
            CONFIG, GROUP, 'triumphant', 8,
        )
        # A concurrent writer refreshed the row after we
        # read set_at=1000: the stale delete must not fire.
        chatter_db.delete_group_vibe(CONFIG, GROUP, 1000)
        row = chatter_db.get_group_vibe(CONFIG, GROUP)
    assert row == ('triumphant', 2000)


def test_delete_removes_the_row_it_observed():
    table, connector = _fresh()
    with patch.object(
        chatter_db, 'get_db_connection', connector
    ), patch.object(chatter_db.time, 'time', lambda: 2000.0):
        chatter_db.upsert_group_vibe(
            CONFIG, GROUP, 'triumphant', 8,
        )
        chatter_db.delete_group_vibe(CONFIG, GROUP, 2000)
        assert chatter_db.get_group_vibe(
            CONFIG, GROUP
        ) is None
    assert table == {}


def test_delete_reuses_a_caller_owned_connection():
    table, connector = _fresh()
    conn = _VibeConn(table)
    table[GROUP] = {
        'vibe': 'wary', 'importance': 5,
        'set_at': 100, 'updated_at': 'NOW()',
    }
    with patch.object(
        chatter_db, 'get_db_connection', connector
    ):
        chatter_db.delete_group_vibe(
            CONFIG, GROUP, 100, conn=conn,
        )
    assert connector.opened == []
    assert conn.closed == 0
    assert table == {}


# ---------------------------------------------------------
# get_session_vibe(): in-memory fast path, DB fallback,
# lazy expiry
# ---------------------------------------------------------

def _clear_sessions():
    chatter_memory._active_sessions.pop(GROUP, None)


def test_live_in_memory_vibe_skips_the_database():
    table, connector = _fresh()
    _clear_sessions()
    chatter_memory._active_sessions[GROUP] = {
        'vibe': 'grimly_amused', 'vibe_set_at': 4900.0,
    }
    try:
        with patch.object(
            chatter_memory, 'get_db_connection', connector
        ), patch.object(
            chatter_memory.time, 'time', lambda: 5000.0
        ):
            out = chatter_memory.get_session_vibe(
                GROUP, CONFIG,
            )
    finally:
        _clear_sessions()
    assert out == 'grimly amused'
    assert connector.opened == []


def test_db_fallback_reseeds_the_in_memory_session():
    table, connector = _fresh()
    table[GROUP] = {
        'vibe': 'triumphant', 'importance': 8,
        'set_at': 4800, 'updated_at': 'NOW()',
    }
    _clear_sessions()
    # A session exists but lost its vibe (post-CLEANUP wipe).
    chatter_memory._active_sessions[GROUP] = {}
    try:
        with patch.object(
            chatter_memory, 'get_db_connection', connector
        ), patch.object(
            chatter_memory.time, 'time', lambda: 5000.0
        ):
            out = chatter_memory.get_session_vibe(
                GROUP, CONFIG,
            )
            session = chatter_memory._active_sessions[GROUP]
            assert out == 'triumphant'
            assert session['vibe'] == 'triumphant'
            assert session['vibe_set_at'] == 4800
            # Re-seeded: the next read is served in-memory.
            connector.opened.clear()
            assert chatter_memory.get_session_vibe(
                GROUP, CONFIG,
            ) == 'triumphant'
            assert connector.opened == []
    finally:
        _clear_sessions()
    # Row survives a successful read.
    assert GROUP in table


def test_db_fallback_works_without_any_session():
    table, connector = _fresh()
    table[GROUP] = {
        'vibe': 'wary', 'importance': 7,
        'set_at': 4900, 'updated_at': 'NOW()',
    }
    _clear_sessions()
    with patch.object(
        chatter_memory, 'get_db_connection', connector
    ), patch.object(
        chatter_memory.time, 'time', lambda: 5000.0
    ):
        out = chatter_memory.get_session_vibe(GROUP, CONFIG)
    assert out == 'wary'
    assert GROUP not in chatter_memory._active_sessions


def test_expired_row_returns_none_and_self_cleans():
    table, connector = _fresh()
    table[GROUP] = {
        'vibe': 'triumphant', 'importance': 8,
        'set_at': 1000, 'updated_at': 'NOW()',
    }
    _clear_sessions()
    chatter_memory._active_sessions[GROUP] = {
        'vibe': 'triumphant', 'vibe_set_at': 1000.0,
    }
    try:
        with patch.object(
            chatter_memory, 'get_db_connection', connector
        ), patch.object(
            chatter_memory.time, 'time', lambda: 5000.0
        ):
            out = chatter_memory.get_session_vibe(
                GROUP, CONFIG,
            )
            session = chatter_memory._active_sessions[GROUP]
            assert 'vibe' not in session
            assert 'vibe_set_at' not in session
    finally:
        _clear_sessions()
    assert out is None
    # Lazily deleted, and on a single shared connection.
    assert table == {}
    assert len(connector.opened) == 1


def test_expiry_is_anchored_to_the_original_set_at():
    """A restart must not extend a vibe's lifetime: the DB
    row's own set_at decides, not the moment it is read."""
    table, connector = _fresh()
    table[GROUP] = {
        'vibe': 'wary', 'importance': 7,
        'set_at': 4390, 'updated_at': 'NOW()',
    }
    _clear_sessions()
    with patch.object(
        chatter_memory, 'get_db_connection', connector
    ), patch.object(
        chatter_memory.time, 'time', lambda: 5000.0
    ):
        # 610s old against a 600s duration -> expired.
        assert chatter_memory.get_session_vibe(
            GROUP, CONFIG,
        ) is None
    assert table == {}


def test_missing_row_clears_a_stale_session_vibe():
    table, connector = _fresh()
    _clear_sessions()
    chatter_memory._active_sessions[GROUP] = {
        'vibe': 'wary', 'vibe_set_at': 1000.0,
    }
    try:
        with patch.object(
            chatter_memory, 'get_db_connection', connector
        ), patch.object(
            chatter_memory.time, 'time', lambda: 5000.0
        ):
            out = chatter_memory.get_session_vibe(
                GROUP, CONFIG,
            )
            session = chatter_memory._active_sessions[GROUP]
            assert 'vibe' not in session
    finally:
        _clear_sessions()
    assert out is None


# ---------------------------------------------------------
# _ensure_cap_and_insert(): the write path
# ---------------------------------------------------------

class _MemoryCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0

    def execute(self, query, params=None):
        self._conn.executed.append(
            (' '.join(query.split()), tuple(params or ()))
        )

    def fetchone(self):
        return (0,)

    def fetchall(self):
        return []

    def close(self):
        pass


class _MemoryConn:
    def __init__(self):
        self.executed = []
        self.commits = 0
        self.closed = 0

    def cursor(self, dictionary=False):
        return _MemoryCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed += 1


def _insert(conn, calls, importance, config=None):
    cfg = {'LLMChatter.GroupChatter'
           '.VibeImportanceThreshold': 5}
    cfg.update(config or {})

    def _record(config_, group_id, vibe, imp, conn=None):
        calls.append((group_id, vibe, imp, conn))

    with patch.object(
        chatter_memory, 'upsert_group_vibe', _record
    ), patch.object(
        chatter_memory, '_maybe_trigger_condensation',
        lambda *a, **k: None,
    ):
        return chatter_memory._ensure_cap_and_insert(
            conn, cfg, BOT, PLAYER, GROUP,
            'boss_kill', 'we felled it', 'triumphant',
            None, 1000, 1, 30, importance=importance,
        )


def test_vibe_is_persisted_without_an_in_memory_session():
    """The 2c fix: the read path falls back to the DB
    unconditionally, so the write path must not be gated on
    a live in-memory session."""
    _clear_sessions()
    conn = _MemoryConn()
    calls = []
    assert _insert(conn, calls, 8) is True
    assert calls == [(GROUP, 'triumphant', 8, conn)]
    assert GROUP not in chatter_memory._active_sessions


def test_vibe_updates_the_session_when_one_exists():
    _clear_sessions()
    chatter_memory._active_sessions[GROUP] = {}
    conn = _MemoryConn()
    calls = []
    try:
        _insert(conn, calls, 8)
        session = chatter_memory._active_sessions[GROUP]
        assert session['vibe'] == 'triumphant'
        assert session['vibe_set_at'] > 0
    finally:
        _clear_sessions()
    assert calls == [(GROUP, 'triumphant', 8, conn)]


def test_unimportant_memory_writes_no_vibe():
    _clear_sessions()
    conn = _MemoryConn()
    calls = []
    assert _insert(conn, calls, 4) is True
    assert calls == []


def test_vibe_write_failure_never_breaks_the_insert():
    _clear_sessions()
    conn = _MemoryConn()

    def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    patches = (
        patch.object(
            chatter_memory, 'upsert_group_vibe', _boom,
        ),
        patch.object(
            chatter_memory, '_maybe_trigger_condensation',
            lambda *a, **k: None,
        ),
    )
    # The failure is logged with a traceback by design;
    # keep it out of the test output.
    logging.disable(logging.CRITICAL)
    try:
        with patches[0], patches[1]:
            ok = chatter_memory._ensure_cap_and_insert(
                conn, {'LLMChatter.GroupChatter'
                       '.VibeImportanceThreshold': 5},
                BOT, PLAYER, GROUP, 'boss_kill',
                'we felled it', 'triumphant', None,
                1000, 1, 30, importance=9,
            )
    finally:
        logging.disable(logging.NOTSET)
    assert ok is True
    assert any(
        q.startswith('INSERT INTO llm_bot_memories')
        for q, _ in conn.executed
    )


def main() -> int:
    tests = [
        test_upsert_then_read_round_trip,
        test_get_group_vibe_missing_row_returns_none,
        test_upsert_overwrites_the_existing_row,
        test_upsert_writes_no_mood_column,
        test_upsert_reuses_a_caller_owned_connection,
        test_upsert_clamps_importance_and_skips_bad_group_ids,
        test_delete_guard_refuses_when_set_at_changed,
        test_delete_removes_the_row_it_observed,
        test_delete_reuses_a_caller_owned_connection,
        test_live_in_memory_vibe_skips_the_database,
        test_db_fallback_reseeds_the_in_memory_session,
        test_db_fallback_works_without_any_session,
        test_expired_row_returns_none_and_self_cleans,
        test_expiry_is_anchored_to_the_original_set_at,
        test_missing_row_clears_a_stale_session_vibe,
        test_vibe_is_persisted_without_an_in_memory_session,
        test_vibe_updates_the_session_when_one_exists,
        test_unimportant_memory_writes_no_vibe,
        test_vibe_write_failure_never_breaks_the_insert,
    ]
    for test in tests:
        test()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
