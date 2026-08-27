#!/usr/bin/env python3
"""Focused checks for activate_orphaned_memories() -- crash/restart
recovery for llm_bot_memories rows left `active = 0`.

This is the function a skeptical review caught hardcoding the
per-pair cap (`max_per=30` regardless of the configured
LLMChatter.Memory.MaxPerBotPlayer): raising that config would have
silently deleted real memories on the next bridge start, above the
newly-raised cap, until this was fixed. These tests exist to make
sure that regression can't come back unnoticed.

Runs against a private, disposable mysqld instance (own datadir,
own unix socket, no networking, torn down at the end) -- never the
live acore_characters database. See ~/mod-llm-chatter-state.md
("mysqld is at /usr/sbin/mysqld, not on PATH... needs a private
instance with its own datadir"). No real bot/player guids are
touched; synthetic ids are used purely by convention since the
whole database is throwaway.

Run directly from the module root:
  python tools/tests/test_orphan_memory_recovery.py
"""

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import mysql.connector  # noqa: E402

import chatter_memory  # noqa: E402

MYSQLD = "/usr/sbin/mysqld"

# Copied verbatim from data/sql/characters/base/00000000_llm_chatter_tables.sql
# (the two tables activate_orphaned_memories() touches). If that base
# file's definition of either table changes, update this copy too --
# it exists to run the real SQL (GROUP BY / ORDER BY / DELETE), not to
# be a schema of record.
SCHEMA_SQL = """
CREATE TABLE `llm_group_bot_traits` (
    `id` INT UNSIGNED NOT NULL AUTO_INCREMENT,
    `group_id` INT UNSIGNED NOT NULL,
    `bot_guid` INT UNSIGNED NOT NULL,
    `bot_name` VARCHAR(64) NOT NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_group_bot` (`group_id`, `bot_guid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `llm_bot_memories` (
    `id`            INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    `bot_guid`      INT UNSIGNED NOT NULL,
    `player_guid`   INT UNSIGNED NOT NULL,
    `group_id`      INT UNSIGNED NOT NULL,
    `memory_type`   ENUM(
        'ambient', 'boss_kill', 'wipe', 'rare_kill',
        'dungeon', 'party_member', 'player_message',
        'first_meeting', 'quest_complete', 'achievement',
        'level_up', 'bg_win', 'bg_loss',
        'discovery', 'pvp_kill', 'gear_change', 'mount_change',
        'condensed'
    ) NOT NULL,
    `memory`        TEXT         NOT NULL,
    `importance_score` TINYINT UNSIGNED NOT NULL DEFAULT 5,
    `condensation_generation` TINYINT UNSIGNED NOT NULL DEFAULT 0,
    `mood`          VARCHAR(32)  NOT NULL DEFAULT 'neutral',
    `active`        TINYINT(1)   NOT NULL DEFAULT 0,
    `used`          TINYINT(1)   NOT NULL DEFAULT 0,
    `session_start` DOUBLE       NOT NULL,
    `created_at`    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_bot_player`        (`bot_guid`, `player_guid`),
    INDEX `idx_bot_player_active` (`bot_guid`, `player_guid`, `active`),
    INDEX `idx_group`             (`group_id`, `active`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


class ScratchMySQL:
    """A throwaway mysqld: own datadir/socket, no networking,
    destroyed on close(). The acore MySQL user has no CREATE
    DATABASE grant against the live server, so this is a private
    instance instead, per the state doc.
    """

    def __init__(self):
        self.datadir = tempfile.mkdtemp(prefix="llmc_orphan_test_")
        self.socket_path = str(Path(self.datadir) / "mysqld.sock")
        self._proc = None

    def start(self):
        subprocess.run(
            [
                MYSQLD, "--no-defaults",
                f"--datadir={self.datadir}",
                "--initialize-insecure",
            ],
            check=True, capture_output=True,
        )
        self._proc = subprocess.Popen(
            [
                MYSQLD, "--no-defaults",
                f"--datadir={self.datadir}",
                f"--socket={self.socket_path}",
                "--skip-networking",
                f"--pid-file={self.datadir}/mysqld.pid",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 30
        last_error = None
        while time.time() < deadline:
            if Path(self.socket_path).exists():
                try:
                    conn = mysql.connector.connect(
                        unix_socket=self.socket_path, user="root",
                    )
                    conn.close()
                    return
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
            time.sleep(0.2)
        self._proc.kill()
        self._proc.wait(timeout=15)
        self._proc = None
        raise RuntimeError(
            f"scratch mysqld never became ready: {last_error}"
        )

    def connect(self):
        conn = mysql.connector.connect(
            unix_socket=self.socket_path, user="root",
        )
        conn.database = "llmc_test"
        return conn

    def load_schema(self):
        conn = mysql.connector.connect(
            unix_socket=self.socket_path, user="root",
        )
        cursor = conn.cursor()
        cursor.execute("CREATE DATABASE llmc_test")
        conn.database = "llmc_test"
        for statement in SCHEMA_SQL.split(";"):
            statement = statement.strip()
            if statement:
                cursor.execute(statement)
        conn.commit()
        cursor.close()
        conn.close()

    def close(self):
        if self._proc is not None:
            try:
                subprocess.run(
                    [
                        "mysqladmin",
                        f"--socket={self.socket_path}",
                        "-uroot", "shutdown",
                    ],
                    check=False, capture_output=True, timeout=15,
                )
                self._proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                self._proc.kill()
            self._proc = None
        shutil.rmtree(self.datadir, ignore_errors=True)


def _insert_memory(
    conn, group_id, bot_guid, player_guid,
    active, session_start, created_at_offset=0,
    importance=5, memory_type='ambient',
):
    """created_at_offset seconds are added to NOW() so distinct
    rows for the same pair can carry distinct timestamps."""
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO llm_bot_memories"
        " (bot_guid, player_guid, group_id, memory_type, memory,"
        "  importance_score, mood, active, used, session_start,"
        "  created_at)"
        " VALUES (%s, %s, %s, %s, 'test memory', %s, 'neutral',"
        "  %s, 0, %s, NOW() + INTERVAL %s SECOND)",
        (
            bot_guid, player_guid, group_id, memory_type,
            importance, active, session_start, created_at_offset,
        ),
    )
    conn.commit()


def _active_rows(conn, bot_guid, player_guid):
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, active, importance_score FROM llm_bot_memories"
        " WHERE bot_guid = %s AND player_guid = %s"
        " ORDER BY id",
        (bot_guid, player_guid),
    )
    return cursor.fetchall()


def test_promotes_orphaned_rows_from_a_long_dead_session(db):
    conn = db.connect()
    now = time.time()
    # session_start 2 hours before its last row's created_at ->
    # well past a 30-minute session_minutes threshold.
    _insert_memory(
        conn, group_id=101, bot_guid=999999101, player_guid=999999901,
        active=0, session_start=now - 7200,
    )
    chatter_memory.activate_orphaned_memories(
        conn, session_minutes=30, max_per=30,
    )
    rows = _active_rows(conn, 999999101, 999999901)
    assert len(rows) == 1
    assert rows[0]['active'] == 1
    conn.close()


def test_discards_orphaned_rows_from_a_short_session(db):
    conn = db.connect()
    now = time.time()
    # session_start only 60s before created_at -> well under a
    # 30-minute threshold, so this looks abandoned mid-conversation.
    _insert_memory(
        conn, group_id=102, bot_guid=999999102, player_guid=999999902,
        active=0, session_start=now - 60,
    )
    chatter_memory.activate_orphaned_memories(
        conn, session_minutes=30, max_per=30,
    )
    rows = _active_rows(conn, 999999102, 999999902)
    assert rows == []
    conn.close()


def test_live_groups_are_left_untouched(db):
    conn = db.connect()
    now = time.time()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO llm_group_bot_traits"
        " (group_id, bot_guid, bot_name) VALUES (%s, %s, 'Bot')",
        (103, 999999103),
    )
    conn.commit()
    _insert_memory(
        conn, group_id=103, bot_guid=999999103, player_guid=999999903,
        active=0, session_start=now - 7200,
    )
    chatter_memory.activate_orphaned_memories(
        conn, session_minutes=30, max_per=30,
    )
    rows = _active_rows(conn, 999999103, 999999903)
    assert len(rows) == 1
    # Still inactive: this group is live, rehydration owns it, not
    # startup recovery.
    assert rows[0]['active'] == 0
    conn.close()


def test_promotion_respects_a_custom_max_per_cap(db):
    """The regression this function shipped once: cap enforcement
    after a bulk promotion was hardcoded to 30 regardless of the
    max_per argument, so raising LLMChatter.Memory.MaxPerBotPlayer
    above what recovery assumed would silently delete real memories
    on the next bridge start. Uses max_per=3 (nowhere near 30) so a
    hardcoded 30 would make this test fail.
    """
    conn = db.connect()
    now = time.time()
    bot_guid, player_guid = 999999104, 999999904
    # Two already-active rows (not part of this recovery pass).
    _insert_memory(
        conn, group_id=104, bot_guid=bot_guid, player_guid=player_guid,
        active=1, session_start=now, importance=6,
        created_at_offset=-50,
    )
    _insert_memory(
        conn, group_id=104, bot_guid=bot_guid, player_guid=player_guid,
        active=1, session_start=now, importance=8,
        created_at_offset=-40,
    )
    # Three orphaned rows from a long-dead session, about to be
    # promoted to active -- pushing the pair from 2 to 5 active rows.
    for i, importance in enumerate((1, 4, 9)):
        _insert_memory(
            conn, group_id=104, bot_guid=bot_guid,
            player_guid=player_guid, active=0,
            session_start=now - 7200, importance=importance,
            created_at_offset=-30 + i,
        )
    chatter_memory.activate_orphaned_memories(
        conn, session_minutes=30, max_per=3,
    )
    rows = _active_rows(conn, bot_guid, player_guid)
    active = [r for r in rows if r['active'] == 1]
    assert len(active) == 3, active
    # The lowest-importance rows (1, 4) are the ones trimmed; the
    # highest-importance three (6, 8, 9) survive.
    kept_importances = sorted(r['importance_score'] for r in active)
    assert kept_importances == [6, 8, 9], kept_importances
    conn.close()


def test_cap_is_not_enforced_on_pairs_that_were_not_promoted(db):
    """A pair already sitting above max_per, but with no orphaned
    rows to promote for it, must be left alone -- this recovery
    pass only trims pairs it just bulk-promoted into, it is not a
    general-purpose cap sweep."""
    conn = db.connect()
    now = time.time()
    bot_guid, player_guid = 999999105, 999999905
    for i in range(5):
        _insert_memory(
            conn, group_id=105, bot_guid=bot_guid,
            player_guid=player_guid, active=1, session_start=now,
            importance=5, created_at_offset=i,
        )
    chatter_memory.activate_orphaned_memories(
        conn, session_minutes=30, max_per=3,
    )
    rows = _active_rows(conn, bot_guid, player_guid)
    assert len(rows) == 5
    assert all(r['active'] == 1 for r in rows)
    conn.close()


def test_default_max_per_is_thirty_when_omitted(db):
    """Sanity check for the documented default: omitting max_per
    entirely (as some call sites do) should behave like passing 30,
    not like passing 0 or leaving the cap unenforced. The regression
    itself (a *hardcoded* 30 regardless of the argument) is proven
    by test_promotion_respects_a_custom_max_per_cap above, which
    uses a cap far below 30."""
    conn = db.connect()
    now = time.time()
    bot_guid, player_guid = 999999106, 999999906
    for i, importance in enumerate(range(1, 6)):
        _insert_memory(
            conn, group_id=106, bot_guid=bot_guid,
            player_guid=player_guid, active=0,
            session_start=now - 7200, importance=importance,
            created_at_offset=i,
        )
    # Default max_per (30): all 5 promoted rows survive.
    chatter_memory.activate_orphaned_memories(
        conn, session_minutes=30,
    )
    rows = _active_rows(conn, bot_guid, player_guid)
    assert len(rows) == 5
    assert all(r['active'] == 1 for r in rows)
    conn.close()


def test_zero_max_per_deletes_nothing(db):
    """The blocker: max_per was never clamped, so
    `excess = ids[:len(ids) - max_per]` with max_per=0 became
    `ids[:len(ids)]` -- every active memory the pair had, deleted
    at bridge startup, silently. A misconfigured
    LLMChatter.Memory.MaxPerBotPlayer = 0 must be refused, not
    obeyed.
    """
    conn = db.connect()
    now = time.time()
    bot_guid, player_guid = 999999107, 999999907
    # Real-shaped pool: two already-active rows plus three
    # orphans about to be promoted, mixed importances.
    for i, importance in enumerate((5, 9)):
        _insert_memory(
            conn, group_id=107, bot_guid=bot_guid,
            player_guid=player_guid, active=1, session_start=now,
            importance=importance, created_at_offset=-60 + i,
        )
    for i, importance in enumerate((2, 4, 7)):
        _insert_memory(
            conn, group_id=107, bot_guid=bot_guid,
            player_guid=player_guid, active=0,
            session_start=now - 7200, importance=importance,
            created_at_offset=-30 + i,
        )
    chatter_memory.activate_orphaned_memories(
        conn, session_minutes=30, max_per=0,
    )
    rows = _active_rows(conn, bot_guid, player_guid)
    # All five survive, all promoted -- nothing deleted.
    assert len(rows) == 5, rows
    assert all(r['active'] == 1 for r in rows), rows
    assert sorted(
        r['importance_score'] for r in rows
    ) == [2, 4, 5, 7, 9]
    conn.close()


def test_negative_max_per_deletes_nothing(db):
    """Same guard, negative side: -1 would have made the slice
    `ids[:len(ids) + 1]`, i.e. still everything."""
    conn = db.connect()
    now = time.time()
    bot_guid, player_guid = 999999108, 999999908
    for i, importance in enumerate((3, 6, 8)):
        _insert_memory(
            conn, group_id=108, bot_guid=bot_guid,
            player_guid=player_guid, active=0,
            session_start=now - 7200, importance=importance,
            created_at_offset=i,
        )
    chatter_memory.activate_orphaned_memories(
        conn, session_minutes=30, max_per=-1,
    )
    rows = _active_rows(conn, bot_guid, player_guid)
    assert len(rows) == 3, rows
    assert all(r['active'] == 1 for r in rows), rows
    conn.close()


def test_trim_never_deletes_the_pairs_top_row(db):
    """Every other deletion path in chatter_memory.py protects a
    pair's single highest-effective-score row via
    _top_row_exclusion_sql(). This one must too: with max_per=1
    the trim has to leave exactly the top row, not an arbitrary
    one.
    """
    conn = db.connect()
    now = time.time()
    bot_guid, player_guid = 999999109, 999999909
    for i, importance in enumerate((2, 10, 4, 6)):
        _insert_memory(
            conn, group_id=109, bot_guid=bot_guid,
            player_guid=player_guid, active=0,
            session_start=now - 7200, importance=importance,
            created_at_offset=i,
        )
    chatter_memory.activate_orphaned_memories(
        conn, session_minutes=30, max_per=1,
    )
    rows = _active_rows(conn, bot_guid, player_guid)
    assert len(rows) == 1, rows
    assert rows[0]['importance_score'] == 10, rows
    conn.close()


def test_trim_uses_effective_score_not_raw_importance(db):
    """The trim must rank by the decay-aware effective score
    (_effective_score_sql()), the same expression eviction and
    condensation-candidate selection use -- not raw
    importance_score.

    Rows at or below DecayMaxImportance (3) lose ~1 point per
    DecayDays (30). A 90-day-old importance-3 row therefore has
    effective score 1, BELOW a fresh importance-2 row. Ranking by
    raw importance would drop the fresh 2 and keep the stale 3;
    ranking by effective score does the opposite.
    """
    conn = db.connect()
    now = time.time()
    bot_guid, player_guid = 999999110, 999999910
    ninety_days = -90 * 24 * 3600
    _insert_memory(
        conn, group_id=110, bot_guid=bot_guid,
        player_guid=player_guid, active=0,
        session_start=now - 7200, importance=3,
        created_at_offset=ninety_days,
    )
    _insert_memory(
        conn, group_id=110, bot_guid=bot_guid,
        player_guid=player_guid, active=0,
        session_start=now - 7200, importance=2,
        created_at_offset=-10,
    )
    # Protected top row, so the trim has exactly one row to drop.
    _insert_memory(
        conn, group_id=110, bot_guid=bot_guid,
        player_guid=player_guid, active=0,
        session_start=now - 7200, importance=9,
        created_at_offset=-5,
    )
    chatter_memory.activate_orphaned_memories(
        conn, session_minutes=30, max_per=2,
    )
    rows = _active_rows(conn, bot_guid, player_guid)
    kept = sorted(r['importance_score'] for r in rows)
    assert kept == [2, 9], kept
    conn.close()


def main() -> int:
    db = ScratchMySQL()
    db.start()
    try:
        db.load_schema()
        tests = [
            test_promotes_orphaned_rows_from_a_long_dead_session,
            test_discards_orphaned_rows_from_a_short_session,
            test_live_groups_are_left_untouched,
            test_promotion_respects_a_custom_max_per_cap,
            test_cap_is_not_enforced_on_pairs_that_were_not_promoted,
            test_default_max_per_is_thirty_when_omitted,
            test_zero_max_per_deletes_nothing,
            test_negative_max_per_deletes_nothing,
            test_trim_never_deletes_the_pairs_top_row,
            test_trim_uses_effective_score_not_raw_importance,
        ]
        for test in tests:
            test(db)
        print("OK")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
