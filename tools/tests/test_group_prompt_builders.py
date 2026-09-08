#!/usr/bin/env python3
"""Every group prompt builder must actually build, in both chat modes.

Background: the merge that brought normal chat mode in unioned two import
lists and dropped build_bot_identity_from_dict, while the gear- and
mount-change builders still called it. Both raised NameError the first time
a bot noticed the player's new gear or new mount, and the suite stayed
green the whole time because nothing ever called those two builders.

This encodes the constraint rather than that one bug: every build_*_prompt
in chatter_group_prompts is invoked in both modes and must return a
non-empty string. A builder referencing a name it never imported, or
drifting out of step with a helper's signature, fails here.

SAMPLE_ARGS is deliberately a ratchet. A builder whose required argument is
missing from the table fails with a message asking for a sample value, so a
newly added builder cannot quietly escape coverage.

Run directly from the module root:
  python tools/tests/test_group_prompt_builders.py
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chatter_group_prompts as builders  # noqa: E402

MODES = ('roleplay', 'normal')


class _EmptyCursor:
    """Answers every query with no rows."""

    def execute(self, *args, **kwargs):
        return None

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def close(self):
        return None


class _StubDb:
    """Enough of a connection for builders that look up dungeon data."""

    def cursor(self, *args, **kwargs):
        return _EmptyCursor()


BOT = {
    'name': 'Thrallmar', 'race': 'Orc', 'class': 'Warrior',
    'level': 70, 'gender': 'male',
}
TRAITS = ['gruff', 'loyal']

# One sample value per required argument name. Names are shared across
# builders, so this stays much smaller than the builder count.
SAMPLE_ARGS = {
    'db': _StubDb(),
    'bot': BOT,
    'reactor': BOT,
    'bots': [BOT, dict(BOT, name='Sylvara')],
    'traits': TRAITS,
    'reactor_traits': TRAITS,
    'traits_map': {'Thrallmar': TRAITS, 'Sylvara': TRAITS},
    'mode': 'roleplay',
    'mood': 'cheerful',
    'bot_name': 'Thrallmar',
    'new_bot_name': 'Sylvara',
    'new_bot_names': ['Sylvara', 'Korgath'],
    'race': 'Orc', 'race_name': 'Orc',
    'class_name': 'Warrior', 'level': 70,
    'player_name': 'Hero', 'player_class': 'Mage',
    'player_race': 'Human', 'player_gender': 'female',
    'player_level': 70, 'player_message': 'ready when you are',
    'achiever_name': 'Hero', 'achiever_names': ['Hero', 'Sylvara'],
    'achievement_name': 'The Immortal',
    'acceptor_name': 'Hero', 'completer_name': 'Hero',
    'quest_name': 'The Lost Caravan', 'quest_names': ['The Lost Caravan'],
    'quest_level': 68,
    'creature_name': 'Gruul the Dragonkiller',
    'killer_name': 'Gruul the Dragonkiller',
    'dead_name': 'Sylvara', 'target_name': 'Sylvara',
    'aggro_target': 'Sylvara', 'caster_name': 'Sylvara',
    'leveler_name': 'Hero', 'new_level': 71,
    'wearer_name': 'Hero', 'rider_name': 'Hero',
    'item_name': 'Warglaive of Azzinoth', 'item_quality': 4,
    'mount_name': 'Swift Nether Drake',
    'spell_name': 'Fireball', 'spell_category': 'offensive',
    'state_type': 'idle',
    'zone_name': 'Nagrand', 'zone_id': 3518,
    'subzone_name': 'Garadar', 'map_name': 'Karazhan', 'map_id': 532,
    'objects': [{'name': 'Rusted Cage', 'type': 'Chest',
                 'is_creature': False, 'sub_name': ''}],
    'is_bot': False, 'is_boss': True, 'is_rare': False, 'is_raid': True,
    'in_city': False, 'in_dungeon': True,
}


def _prompt_builders():
    found = [
        (name, fn) for name, fn in vars(builders).items()
        if name.startswith('build_') and name.endswith('prompt')
        and inspect.isfunction(fn)
        and fn.__module__ == builders.__name__
    ]
    assert found, 'no prompt builders discovered'
    return sorted(found)


def _required_args(fn):
    return [
        p.name for p in inspect.signature(fn).parameters.values()
        if p.default is inspect._empty
        and p.kind in (p.POSITIONAL_OR_KEYWORD, p.POSITIONAL_ONLY)
    ]


def test_every_builder_has_sample_arguments():
    """The ratchet: no builder may go uncovered for want of a sample."""
    missing = {}
    for name, fn in _prompt_builders():
        unknown = [a for a in _required_args(fn) if a not in SAMPLE_ARGS]
        if unknown:
            missing[name] = unknown
    assert not missing, (
        'add a sample value to SAMPLE_ARGS for: %r' % missing)


def test_every_builder_builds_in_both_modes():
    """Each builder returns real prompt text rather than raising."""
    for name, fn in _prompt_builders():
        for mode in MODES:
            args = dict(SAMPLE_ARGS, mode=mode)
            kwargs = {a: args[a] for a in _required_args(fn)}
            try:
                result = fn(**kwargs)
            except Exception as exc:
                raise AssertionError(
                    '%s raised in %s mode: %s: %s'
                    % (name, mode, type(exc).__name__, exc))
            assert isinstance(result, str), (
                '%s returned %s, not str' % (name, type(result).__name__))
            assert result.strip(), '%s returned empty text' % name


def test_gear_and_mount_builders_are_covered():
    """The two builders the import drop broke, named explicitly.

    The generic sweep above already calls them, but naming them keeps the
    regression visible if the discovery logic is ever narrowed.
    """
    names = {name for name, _ in _prompt_builders()}
    assert 'build_gear_change_reaction_prompt' in names
    assert 'build_mount_change_reaction_prompt' in names


def main() -> int:
    test_every_builder_has_sample_arguments()
    test_every_builder_builds_in_both_modes()
    test_gear_and_mount_builders_are_covered()
    print('OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
