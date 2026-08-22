#!/usr/bin/env python3
"""Cross-language decay-formula drift guard.

chatter_memory._effective_score_sql() and its hand-written
mirror in src/LLMChatterCommand.cpp must stay in sync. This
test reads the C++ source directly and asserts that (a) the
hardcoded C++ defaults match the Python constants, and (b)
the formula shape (CASE ... GREATEST(1, ...) with
TIMESTAMPDIFF(DAY, ...) / decayDays) is present in both, so
a change in one place without the other fails loudly.

Run directly from the module root:
  python tools/tests/test_decay_cross_language.py
"""

import re
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import chatter_memory  # noqa: E402

CPP_PATH = (
    Path(__file__).resolve().parents[2]
    / "src" / "LLMChatterCommand.cpp"
)


def _cpp_decay_defaults():
    """Return (max_importance, decay_days) hardcoded in C++."""
    text = CPP_PATH.read_text(encoding="utf-8")
    max_imp = re.search(
        r'"LLMChatter\.Memory\.DecayMaxImportance",\s*(\d+)',
        text,
    )
    days = re.search(
        r'"LLMChatter\.Memory\.DecayDays",\s*(\d+)',
        text,
    )
    assert max_imp, "DecayMaxImportance default not found in C++"
    assert days, "DecayDays default not found in C++"
    return int(max_imp.group(1)), int(days.group(1))


def test_cpp_defaults_match_python_constants():
    cpp_max, cpp_days = _cpp_decay_defaults()
    assert cpp_max == chatter_memory.DEFAULT_DECAY_MAX_IMPORTANCE
    assert cpp_days == chatter_memory.DEFAULT_DECAY_DAYS


def test_cpp_formula_mirrors_python_formula():
    cpp_text = CPP_PATH.read_text(encoding="utf-8")
    for token in (
        "CASE WHEN importance_score",
        "GREATEST(1",
        "TIMESTAMPDIFF(DAY",
        "ELSE importance_score END",
    ):
        assert token in cpp_text, f"missing {token!r} in C++ mirror"

    py_sql = chatter_memory._effective_score_sql({})
    for token in (
        "CASE WHEN importance_score",
        "GREATEST(1",
        "TIMESTAMPDIFF(DAY",
        "ELSE importance_score END",
    ):
        assert token in py_sql, f"missing {token!r} in Python SQL"


def main() -> int:
    test_cpp_defaults_match_python_constants()
    test_cpp_formula_mirrors_python_formula()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
