#!/usr/bin/env python3
"""Eviction must remove the least valuable memory, not the most valuable.

This guards a bug that shipped unnoticed: the eviction query filtered
`used = 1` and then ordered by effective_score ASC. Because `used` is set
only by the recall path -- which selects ORDER BY effective_score DESC --
`used = 1` is by construction the pool's *highest*-value subset. Eviction
was therefore deleting the weakest of the best while never-recalled
low-value rows survived, and condensation digests (always inserted used=1
with importance capped below the protect floor) were reliably first in line,
so the feature destroyed its own output.

The assertions are on the SQL the code issues rather than on rows, because
these statements use MySQL-only syntax (DELETE ... ORDER BY ... LIMIT) that
no in-process database will execute faithfully. Asserting the query shape is
what actually pins the bug.

Run directly from the module root:
  python tools/tests/test_memory_eviction.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chatter_memory  # noqa: E402


class _RecordingCursor:
    """Captures executed SQL; reports one row deleted."""

    def __init__(self):
        self.statements = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def close(self):
        pass


class _RecordingConn:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def _run_eviction(config=None):
    cursor, conn = _RecordingCursor(), _RecordingConn()
    chatter_memory._evict_one_used(cursor, conn, 1, 2, config)
    assert cursor.statements, "eviction issued no SQL at all"
    return cursor.statements


def _normalise(sql):
    return re.sub(r"\s+", " ", sql).strip()


def test_eviction_does_not_filter_on_used():
    """The exact regression: `used = 1` must not appear as a filter.

    `used = 1` marks the memories recall considered most valuable.
    Restricting deletion to them inverts this function's purpose.
    """
    for sql, _ in _run_eviction():
        normalised = _normalise(sql)
        where = normalised.split("ORDER BY")[0]
        assert not re.search(r"\bused\s*=\s*1\b", where), (
            "eviction restricts deletion to used=1 rows. Those are the "
            "rows recall ranked highest, so this deletes the most "
            "valuable memories and spares the least valuable ones:\n"
            + normalised
        )


def test_eviction_orders_by_ascending_score():
    """Least valuable first, and the ordering must be score-led."""
    sql, _ = _run_eviction()[0]
    normalised = _normalise(sql)
    assert "ORDER BY" in normalised, "eviction deletes without an ORDER BY"
    order_clause = normalised.split("ORDER BY", 1)[1]
    assert " ASC" in order_clause, (
        "eviction must order ascending so the lowest-value row goes "
        "first: " + order_clause
    )
    assert "LIMIT 1" in normalised, "eviction must delete exactly one row"


def test_eviction_never_touches_the_single_best_memory():
    """The top-scoring memory for a pair is always excluded."""
    sql, _ = _run_eviction()[0]
    normalised = _normalise(sql).lower()
    # The exclusion is expressed as: id != (SELECT id ... ORDER BY score
    # DESC LIMIT 1) -- i.e. "everything except the single best row".
    has_exclusion = (
        re.search(r"id\s*!=\s*\(\s*select", normalised)
        or "not in" in normalised
        or "not exists" in normalised
    )
    assert has_exclusion, (
        "eviction has no top-row exclusion, so a bot can forget the most "
        "valuable thing it knows about a player:\n" + normalised
    )
    assert "desc limit 1" in normalised, (
        "the excluded row must be selected by DESCENDING score (the best "
        "one); excluding an ascending pick would protect the worst row"
    )


def test_ties_prefer_evicting_an_already_recalled_row():
    """Among equally-scored rows, one that has surfaced already goes
    before one that has never had the chance."""
    sql, _ = _run_eviction()[0]
    order_clause = _normalise(sql).split("ORDER BY", 1)[1]
    assert re.search(r"used\s+DESC", order_clause), (
        "expected `used DESC` as a tie-break so unread memories are kept "
        "in preference to already-recalled ones: " + order_clause
    )


def test_a_single_statement_covers_the_whole_pool():
    """No used-only pass followed by a fallback: one ordered statement
    over every row, so the ordering cannot be subverted by the filter."""
    statements = _run_eviction()
    assert len(statements) == 1, (
        "expected one DELETE covering the whole pool, got %d statements; "
        "a filtered first pass is how the original bug worked"
        % len(statements)
    )


def main() -> int:
    tests = [
        test_eviction_does_not_filter_on_used,
        test_eviction_orders_by_ascending_score,
        test_eviction_never_touches_the_single_best_memory,
        test_ties_prefer_evicting_an_already_recalled_row,
        test_a_single_statement_covers_the_whole_pool,
    ]
    for test in tests:
        test()
        print("PASS: %s" % test.__name__)
    print("\n%d tests passed" % len(tests))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
