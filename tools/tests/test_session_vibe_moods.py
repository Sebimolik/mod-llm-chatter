#!/usr/bin/env python3
"""Focused session-vibe prompt checks.

Run directly from the module root:
  python tools/tests/test_session_vibe_moods.py
"""

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


def test_vibe_reaches_the_idle_conversation_prompt():
    """The vibe now replaces the bare tone word with a
    sentence naming what caused it."""
    random.seed(14)
    prompt = chatter_group.build_idle_conversation_prompt(
        BOTS, TRAITS, 'normal', 'the road ahead',
        members=['Testbotone', 'Testbottwo'],
        zone_id=1, map_id=0, session_vibe='humbled',
        session_vibe_source='wipe',
    )
    assert (
        "The group is still humbled after being cut down"
        " to the last of them."
        in prompt
    )
    assert "Overall tone:" not in prompt
    # The per-message sequence sets length only: moods belong to
    # each speaker's persona, which is built from what actually
    # happened to that bot.
    first_line = next(
        line for line in prompt.splitlines()
        if line.strip().startswith("Message 1 ")
    )
    assert "mood=" not in first_line
    assert "length=" in first_line


def test_vibe_without_a_source_still_replaces_the_tone():
    """Legacy row (no source_type): still a sentence, just
    without a named cause."""
    random.seed(14)
    prompt = chatter_group.build_idle_conversation_prompt(
        BOTS, TRAITS, 'normal', 'the road ahead',
        members=['Testbotone', 'Testbottwo'],
        zone_id=1, map_id=0, session_vibe='humbled',
    )
    assert "mood right now is humbled" in prompt
    assert "Overall tone:" not in prompt


def test_no_vibe_prompt_still_gives_every_speaker_a_tone():
    """Without a vibe the persona cast supplies the tones.

    There is deliberately no single "Overall tone" line any
    more: each speaker carries its own tone from its persona,
    so one shared tone would contradict them.
    """
    random.seed(15)
    prompt = chatter_group.build_idle_conversation_prompt(
        BOTS, TRAITS, 'normal', 'the road ahead',
        members=['Testbotone', 'Testbottwo'],
        zone_id=1, map_id=0, session_vibe=None,
    )
    assert "Overall tone:" not in prompt

    toned = [
        line for line in prompt.splitlines()
        if "tone: " in line and "—" in line
    ]
    assert len(toned) >= 2, (
        "expected a tone per speaker in the cast, got %r" % toned
    )
    for line in toned:
        tone = line.split("tone: ", 1)[1].split(";")[0].strip()
        # Persona tones are composed phrases seeded from the
        # bot's identity, not bare TONES entries, so the shape
        # is what matters: a non-empty descriptive tone.
        assert tone and not tone.endswith(":"), (
            "%r is not a usable tone" % tone
        )


def main() -> int:
    tests = [
        test_vibe_reaches_the_idle_conversation_prompt,
        test_vibe_without_a_source_still_replaces_the_tone,
        test_no_vibe_prompt_still_gives_every_speaker_a_tone,
    ]
    for test in tests:
        test()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
