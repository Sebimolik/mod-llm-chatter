#!/usr/bin/env python3
"""Focused Unicode cleanup regression checks.

Run directly from the module root:
  python tools/tests/test_text_cleanup.py
"""

import sys
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from chatter_text import (  # noqa: E402
    cleanup_message,
    extract_json_object,
)


def test_hangul_and_cjk_text_are_preserved():
    message = "좋아, 이제 출발하자. 准备好了吗?"
    assert cleanup_message(message) == message


def test_actual_emoji_ranges_are_removed():
    message = "A\U000024C2B\U0001F201C\U0001F600D"
    assert cleanup_message(message) == "ABCD"


def test_nested_json_is_extracted_from_surrounding_prose():
    """A chatty model must not defeat the embedded-JSON fallback.

    The fallback used to be a regex whose character class could not cross
    a nested object, so any payload shaped {"key": [{...}]} -- which is
    the condensation schema -- could never be recovered from prose. That
    is also the call routed to the cheap model by default, i.e. the one
    most likely to wrap its answer in commentary.
    """
    raw = (
        'Sure! Here are the digests:\n'
        '{"digests": [{"memory": "a", "importance": 4}]}\n'
        'Let me know if you want changes.'
    )
    data = extract_json_object(raw, required_key='digests')
    assert data is not None, 'nested payload was not recovered from prose'
    assert data['digests'][0]['memory'] == 'a'


def test_braces_inside_strings_do_not_break_extraction():
    raw = 'noise {"digests": [{"memory": "a } b {"}]} trailing'
    data = extract_json_object(raw, required_key='digests')
    assert data is not None
    assert data['digests'][0]['memory'] == 'a } b {'


def test_last_object_wins_when_the_model_revises_itself():
    raw = ('{"digests": [{"memory": "first"}]} '
           'on reflection: {"digests": [{"memory": "second"}]}')
    data = extract_json_object(raw, required_key='digests')
    assert data['digests'][0]['memory'] == 'second'


def main() -> int:
    test_hangul_and_cjk_text_are_preserved()
    test_actual_emoji_ranges_are_removed()
    test_nested_json_is_extracted_from_surrounding_prose()
    test_braces_inside_strings_do_not_break_extraction()
    test_last_object_wins_when_the_model_revises_itself()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
