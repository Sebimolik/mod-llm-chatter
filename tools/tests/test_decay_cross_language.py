#!/usr/bin/env python3
"""Cross-language decay-formula drift guard.

chatter_memory._effective_score_sql() and its hand-written
mirror in src/LLMChatterCommand.cpp must stay in sync. This
test reads the C++ source directly and asserts that (a) the
hardcoded C++ defaults match the Python constants, and (b)
the two decay expressions are character-for-character the
same SQL once the C++ concatenation is evaluated with the
same tunables as the Python builder.

(b) is deliberately an exact comparison of the extracted
expression rather than a grep for fragments anywhere in the
.cpp: a changed floor (GREATEST(1 -> GREATEST(2)), divisor
(decayDays -> decayDays * 2) or unit (DAY -> HOUR) has to
fail here, which a whole-file substring check cannot do.

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


# Deliberately implausible tunables: they make the two
# expressions comparable while guaranteeing the numbers come
# from the config plumbing and not from a coincidence with a
# hardcoded default.
PROBE_MAX_IMPORTANCE = 4242
PROBE_DECAY_DAYS = 7331

# C++ locals feeding the mirrored expression, mapped to the
# probe value the Python builder is handed for each.
CPP_DECAY_VARS = {
    "decayMaxImportance": PROBE_MAX_IMPORTANCE,
    "decayDays": PROBE_DECAY_DAYS,
}

_EXPR_RE = re.compile(
    r"std::string\s+effectiveScoreExpr\s*=\s*(.*?);",
    re.S,
)
_PIECE_RE = re.compile(
    r'"((?:[^"\\]|\\.)*)"'
    r"|std::to_string\(([^()]*)\)"
)


def _normalize(sql):
    """Collapse formatting-only differences (the two sources
    wrap the same SQL differently) without touching
    structure."""
    return " ".join(sql.split())


def _cpp_decay_expression(vars_):
    """Evaluate the C++ effectiveScoreExpr concatenation.

    Only string literals, std::to_string(<local>) and '+'
    may appear: anything else means the mirror grew logic
    this guard cannot reason about, and that is a failure,
    not something to skip over.
    """
    text = CPP_PATH.read_text(encoding="utf-8")
    match = _EXPR_RE.search(text)
    assert match, "effectiveScoreExpr not found in C++ mirror"
    body = match.group(1)

    pieces = []
    leftover = list(body)
    for piece in _PIECE_RE.finditer(body):
        literal, var = piece.group(1), piece.group(2)
        if literal is not None:
            pieces.append(
                literal.encode().decode("unicode_escape")
            )
        else:
            # A bare tunable only: `decayDays * 2` or any
            # other inline arithmetic is real drift, and
            # must be reported as such rather than quietly
            # compared as if it were the plain variable.
            term = var.strip()
            assert term in vars_, (
                f"unrecognized C++ decay term {term!r}"
            )
            pieces.append(str(vars_[term]))
        leftover[piece.start():piece.end()] = (
            " " * (piece.end() - piece.start())
        )
    assert pieces, "effectiveScoreExpr is empty"
    assert not set("".join(leftover)) - set(" +\n\t\r"), (
        "unsupported C++ expression syntax: " + body
    )
    return _normalize("".join(pieces))


def _parse_decay_expression(sql):
    """Pull the decay knobs back out of the built SQL so a
    mismatch reports which part drifted."""
    match = re.fullmatch(
        r"CASE WHEN importance_score (?P<cmp>\S+)"
        r" (?P<threshold>\d+) THEN"
        r" GREATEST\((?P<floor>\d+), importance_score -"
        r" TIMESTAMPDIFF\((?P<unit>\w+), created_at,"
        r" NOW\(\)\) / (?P<divisor>\d+)\)"
        r" ELSE importance_score END",
        sql,
    )
    assert match, "unrecognized decay expression: " + sql
    return match.groupdict()


def test_cpp_formula_mirrors_python_formula():
    cpp_sql = _cpp_decay_expression(CPP_DECAY_VARS)
    py_sql = _normalize(chatter_memory._effective_score_sql({
        "LLMChatter.Memory.DecayMaxImportance":
            PROBE_MAX_IMPORTANCE,
        "LLMChatter.Memory.DecayDays": PROBE_DECAY_DAYS,
    }))

    # Structural read-out first: a drifted floor, divisor,
    # comparison or time unit names itself in the failure.
    cpp_parts = _parse_decay_expression(cpp_sql)
    py_parts = _parse_decay_expression(py_sql)
    assert cpp_parts == py_parts, (cpp_parts, py_parts)

    # Both tunables really reached the expression.
    assert cpp_parts["threshold"] == str(PROBE_MAX_IMPORTANCE)
    assert cpp_parts["divisor"] == str(PROBE_DECAY_DAYS)

    # And nothing else about the expression differs either.
    assert cpp_sql == py_sql, (cpp_sql, py_sql)


def main() -> int:
    test_cpp_defaults_match_python_constants()
    test_cpp_formula_mirrors_python_formula()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
