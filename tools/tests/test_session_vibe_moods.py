#!/usr/bin/env python3
"""Focused session-vibe mood-bias checks.

Run directly from the module root:
  python tools/tests/test_session_vibe_moods.py
"""

import collections
import importlib
import random
import sys
import types
from pathlib import Path


def _ensure_module(name: str) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    return mod


def _install_non_strict_stubs() -> None:
    """Install minimal stubs for optional provider/database deps."""
    for mod_name in ("anthropic", "openai"):
        try:
            importlib.import_module(mod_name)
        except ModuleNotFoundError:
            mod = _ensure_module(mod_name)
            if mod_name == "anthropic":
                setattr(mod, "Anthropic", type("Anthropic", (), {}))
            else:
                setattr(mod, "OpenAI", type("OpenAI", (), {}))

    try:
        importlib.import_module("mysql.connector")
    except ModuleNotFoundError:
        mysql_mod = _ensure_module("mysql")
        connector_mod = _ensure_module("mysql.connector")
        setattr(mysql_mod, "connector", connector_mod)


TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
_install_non_strict_stubs()

import chatter_constants  # noqa: E402
import chatter_group  # noqa: E402
import chatter_prompts  # noqa: E402

BOTS = [
    {
        'guid': 999999001, 'name': 'Testbotone',
        'class': 'Warrior', 'race': 'Orc', 'level': 40,
        'gender': 'male', 'role': 'tank',
    },
    {
        'guid': 999999002, 'name': 'Testbottwo',
        'class': 'Priest', 'race': 'Troll', 'level': 40,
        'gender': 'female', 'role': 'healer',
    },
]
TRAITS = {'Testbotone': ['gruff'], 'Testbottwo': ['calm']}


def test_family_moods_exist_in_the_real_pools():
    pools = {
        'normal': chatter_constants.MOODS,
        'roleplay': chatter_constants.RP_MOODS,
    }
    for mode, pool in pools.items():
        by_family = chatter_constants.VIBE_FAMILY_MOODS[mode]
        assert set(by_family) == set(
            chatter_constants.VIBE_MOOD_FAMILIES.values()
        )
        for moods in by_family.values():
            for mood in moods:
                assert mood in pool, (mode, mood)


def test_unknown_and_empty_vibes_do_not_bias():
    assert chatter_prompts.get_vibe_mood_pool(None) is None
    assert chatter_prompts.get_vibe_mood_pool("") is None
    assert chatter_prompts.get_vibe_mood_pool("bewildered") is None


def test_underscored_memory_mood_is_normalized():
    assert chatter_prompts.get_vibe_mood_pool(
        "grimly_amused", "roleplay"
    ) == chatter_prompts.get_vibe_mood_pool(
        "grimly amused", "roleplay"
    )


def test_first_message_always_matches_the_vibe():
    pool = chatter_prompts.get_vibe_mood_pool("humbled")
    random.seed(11)
    for _ in range(200):
        seq = chatter_prompts.generate_conversation_mood_sequence(
            4, 'normal', session_vibe='humbled',
        )
        assert len(seq) == 4
        assert seq[0] in pool


def test_bias_decays_across_the_exchange():
    pool = chatter_prompts.get_vibe_mood_pool("triumphant")
    random.seed(12)
    hits = [0, 0, 0, 0]
    runs = 1500
    for _ in range(runs):
        seq = chatter_prompts.generate_conversation_mood_sequence(
            4, 'normal', session_vibe='triumphant',
        )
        for i, mood in enumerate(seq):
            if mood in pool:
                hits[i] += 1
    rates = [hit / runs for hit in hits]
    assert rates[0] == 1.0
    # Strictly loosening grip, but never back to pure chance
    # inside a short exchange.
    assert rates[0] > rates[1] > rates[2] > rates[3]
    assert rates[3] > len(pool) / len(chatter_constants.MOODS)


def test_no_vibe_keeps_the_full_random_pool():
    random.seed(13)
    seen = collections.Counter()
    for _ in range(4000):
        seen.update(
            chatter_prompts.generate_conversation_mood_sequence(
                4, 'normal',
            )
        )
    assert len(seen) == len(chatter_constants.MOODS)


def test_vibe_reaches_the_idle_conversation_prompt():
    random.seed(14)
    prompt = chatter_group.build_idle_conversation_prompt(
        BOTS, TRAITS, 'normal', 'the road ahead',
        members=['Testbotone', 'Testbottwo'],
        zone_id=1, map_id=0, session_vibe='humbled',
    )
    assert "Overall tone: humbled" in prompt
    pool = chatter_prompts.get_vibe_mood_pool("humbled")
    first_line = next(
        line for line in prompt.splitlines()
        if line.strip().startswith("Message 1 ")
    )
    assert any(f"mood={mood}" in first_line for mood in pool)


def test_no_vibe_prompt_uses_a_random_tone():
    random.seed(15)
    prompt = chatter_group.build_idle_conversation_prompt(
        BOTS, TRAITS, 'normal', 'the road ahead',
        members=['Testbotone', 'Testbottwo'],
        zone_id=1, map_id=0, session_vibe=None,
    )
    tone_line = next(
        line for line in prompt.splitlines()
        if line.startswith("Overall tone: ")
    )
    tone = tone_line[len("Overall tone: "):]
    assert tone in chatter_constants.TONES


def main() -> int:
    tests = [
        test_family_moods_exist_in_the_real_pools,
        test_unknown_and_empty_vibes_do_not_bias,
        test_underscored_memory_mood_is_normalized,
        test_first_message_always_matches_the_vibe,
        test_bias_decays_across_the_exchange,
        test_no_vibe_keeps_the_full_random_pool,
        test_vibe_reaches_the_idle_conversation_prompt,
        test_no_vibe_prompt_uses_a_random_tone,
    ]
    for test in tests:
        test()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
