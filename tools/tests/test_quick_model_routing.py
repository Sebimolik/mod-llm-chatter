#!/usr/bin/env python3
"""Focused checks for cheap-model routing of internal
memory-side LLM work.

Memory text, digests and relationship summaries are never
shown to a player verbatim, so they run on
LLMChatter.QuickAnalyze.Model when one is configured and
fall back to LLMChatter.Model when one is not.

Run directly from the module root:
  python tools/tests/test_quick_model_routing.py
"""

import importlib
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

import chatter_llm  # noqa: E402
import chatter_memory  # noqa: E402


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        message = _Obj(content='ok', tool_calls=None)
        return _Obj(
            choices=[
                _Obj(message=message,
                     finish_reason='stop')
            ],
            usage=None,
        )


class _FakeClient:
    def __init__(self):
        self.completions = _FakeCompletions()
        self.chat = _Obj(completions=self.completions)


_BASE_CONFIG = {
    'LLMChatter.Provider': 'openrouter',
    'LLMChatter.Model': 'deepseek-chat',
    'LLMChatter.MaxTokens': '120',
    'LLMChatter.Temperature': '0.85',
}


def test_quick_model_used_when_configured():
    cfg = dict(_BASE_CONFIG)
    cfg['LLMChatter.QuickAnalyze.Model'] = 'cheap-model'
    client = _FakeClient()
    chatter_llm.call_llm(
        client, 'hi', cfg, label='memory_generation',
        use_quick_model=True,
    )
    assert (
        client.completions.kwargs['model']
        == 'cheap-model'
    )


def test_falls_back_to_main_model_when_unconfigured():
    cfg = dict(_BASE_CONFIG)
    client = _FakeClient()
    chatter_llm.call_llm(
        client, 'hi', cfg, label='memory_generation',
        use_quick_model=True,
    )
    # No QuickAnalyze config at all -> identical to a
    # normal call, same client, same model.
    assert (
        client.completions.kwargs['model']
        == 'deepseek-chat'
    )


def test_main_path_ignores_quick_model():
    cfg = dict(_BASE_CONFIG)
    cfg['LLMChatter.QuickAnalyze.Model'] = 'cheap-model'
    client = _FakeClient()
    chatter_llm.call_llm(
        client, 'hi', cfg, label='group_player_msg',
    )
    # Player-visible chatter must never be downgraded.
    assert (
        client.completions.kwargs['model']
        == 'deepseek-chat'
    )


def test_quick_llm_analyze_still_uses_quick_model():
    cfg = dict(_BASE_CONFIG)
    cfg['LLMChatter.QuickAnalyze.Model'] = 'cheap-model'
    client = _FakeClient()
    chatter_llm.quick_llm_analyze(
        client, cfg, 'hi', label='find_addressed_bot',
    )
    assert (
        client.completions.kwargs['model']
        == 'cheap-model'
    )


def test_memory_quick_model_toggle():
    assert chatter_memory._memory_uses_quick_model({})
    assert chatter_memory._memory_uses_quick_model(
        {'LLMChatter.Memory.UseQuickModel': '1'}
    )
    assert not chatter_memory._memory_uses_quick_model(
        {'LLMChatter.Memory.UseQuickModel': '0'}
    )


def _capture_call_llm():
    """Patch chatter_memory.call_llm, returning the recorded
    kwargs list."""
    recorded = []

    def _fake(client, prompt, config, **kwargs):
        recorded.append(kwargs)
        return None

    return recorded, _fake


def test_memory_generation_routes_to_quick_model():
    recorded, fake = _capture_call_llm()
    with patch.object(
        chatter_memory, 'call_llm', fake
    ), patch.object(
        chatter_memory, 'get_llm_client',
        lambda cfg: _FakeClient()
    ):
        chatter_memory._call_llm_for_memory(
            dict(_BASE_CONFIG),
            bot_name='Grimtusk', bot_class='Warrior',
            bot_race='Orc', bot_gender='male',
            player_name='Aeryn',
            memory_type='ambient',
        )
    assert len(recorded) == 1
    assert recorded[0]['label'] == 'memory_generation'
    assert recorded[0]['use_quick_model'] is True


def test_memory_generation_respects_opt_out():
    recorded, fake = _capture_call_llm()
    cfg = dict(_BASE_CONFIG)
    cfg['LLMChatter.Memory.UseQuickModel'] = '0'
    with patch.object(
        chatter_memory, 'call_llm', fake
    ), patch.object(
        chatter_memory, 'get_llm_client',
        lambda c: _FakeClient()
    ):
        chatter_memory._call_llm_for_memory(
            cfg, bot_name='Grimtusk',
            memory_type='ambient',
        )
    assert recorded[0]['use_quick_model'] is False


if __name__ == '__main__':
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith('test_') and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"\n{len(tests)} tests passed")
