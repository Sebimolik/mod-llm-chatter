#!/usr/bin/env python3
"""Focused checks for provider token-usage capture.

Covers both halves of the contract: usage that the
provider actually reports gets logged verbatim, and usage
that is absent stays absent (never estimated, never zero-
filled).

Run directly from the module root:
  python tools/tests/test_token_usage_logging.py
"""

import importlib
import json
import sys
import types
from pathlib import Path


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


TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
_install_non_strict_stubs()

import chatter_llm  # noqa: E402
import chatter_log_viewer  # noqa: E402
import chatter_request_logger  # noqa: E402


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _openai_response(content, usage=None):
    """Minimal stand-in for an OpenAI-compatible reply."""
    message = _Obj(content=content, tool_calls=None)
    choice = _Obj(message=message, finish_reason='stop')
    return _Obj(choices=[choice], usage=usage)


class _FakeCompletions:
    def __init__(self, response):
        self._response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.chat = _Obj(
            completions=_FakeCompletions(response)
        )


_CONFIG = {
    'LLMChatter.Provider': 'openrouter',
    'LLMChatter.Model': 'deepseek-chat',
    'LLMChatter.MaxTokens': '120',
    'LLMChatter.Temperature': '0.85',
}


def _capture_entries(tmp_path, fn):
    """Run fn() with the request logger writing to
    tmp_path, then return the parsed JSONL rows."""
    chatter_request_logger.init_request_logger({
        'LLMChatter.RequestLog.Enable': '1',
        'LLMChatter.RequestLog.Path': str(tmp_path),
        'LLMChatter.RequestLog.MaxSizeMB': '50',
    })
    try:
        fn()
    finally:
        chatter_request_logger._enabled = False
    rows = []
    with open(tmp_path, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _tmp_log(name):
    import tempfile
    return Path(tempfile.mkdtemp()) / name


def test_openai_usage_is_logged_verbatim():
    usage = _Obj(
        prompt_tokens=1234,
        completion_tokens=56,
        total_tokens=1290,
    )
    client = _FakeClient(
        _openai_response('a line', usage)
    )
    path = _tmp_log('usage.jsonl')
    rows = _capture_entries(
        path,
        lambda: chatter_llm.call_llm(
            client, 'hello', _CONFIG,
            label='memory_generation',
        ),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row['prompt_tokens'] == 1234
    assert row['completion_tokens'] == 56
    assert row['total_tokens'] == 1290
    assert row['label'] == 'memory_generation'


def test_missing_usage_logs_no_token_fields():
    client = _FakeClient(
        _openai_response('a line', None)
    )
    path = _tmp_log('nousage.jsonl')
    rows = _capture_entries(
        path,
        lambda: chatter_llm.call_llm(
            client, 'hello', _CONFIG, label='precache',
        ),
    )
    assert len(rows) == 1
    row = rows[0]
    # Absent means absent -- not 0, not an estimate.
    assert 'prompt_tokens' not in row
    assert 'completion_tokens' not in row
    assert 'total_tokens' not in row
    # The pre-existing schema is still intact.
    assert row['label'] == 'precache'
    assert row['response'] == 'a line'


def test_failed_call_logs_no_token_fields():
    class _Boom:
        def create(self, **kwargs):
            raise RuntimeError('nope')

    client = _Obj(chat=_Obj(completions=_Boom()))
    path = _tmp_log('boom.jsonl')
    rows = _capture_entries(
        path,
        lambda: chatter_llm.call_llm(
            client, 'hello', _CONFIG, label='precache',
        ),
    )
    assert len(rows) == 1
    assert 'total_tokens' not in rows[0]
    assert rows[0]['response'] is None


def test_anthropic_usage_shape_is_normalized():
    usage = _Obj(input_tokens=10, output_tokens=4)
    response = _Obj(
        content=[_Obj(text=' hi ')], usage=usage,
    )

    class _Messages:
        def create(self, **kwargs):
            return response

    client = _Obj(messages=_Messages())
    path = _tmp_log('anthropic.jsonl')
    cfg = dict(_CONFIG)
    cfg['LLMChatter.Provider'] = 'anthropic'
    rows = _capture_entries(
        path,
        lambda: chatter_llm.call_llm(
            client, 'hello', cfg, label='memory_generation',
        ),
    )
    row = rows[0]
    assert row['prompt_tokens'] == 10
    assert row['completion_tokens'] == 4
    # Anthropic reports no total; it is summed, not guessed.
    assert row['total_tokens'] == 14


def test_extract_usage_ignores_junk():
    assert chatter_llm._extract_usage(None) is None
    assert chatter_llm._extract_usage(_Obj()) is None
    assert chatter_llm._extract_usage(
        _Obj(usage=_Obj(prompt_tokens='lots'))
    ) is None
    assert chatter_llm._extract_usage(
        {'usage': {'total_tokens': 7}}
    ) == {'total_tokens': 7}


def test_token_breakdown_mixes_old_and_new_rows():
    entries = [
        # Old-format row: no token fields at all.
        {'label': 'precache', 'duration_ms': 10},
        {
            'label': 'precache', 'duration_ms': 10,
            'prompt_tokens': 100,
            'completion_tokens': 10,
            'total_tokens': 110,
        },
        {
            'label': 'memory_generation',
            'duration_ms': 10,
            'prompt_tokens': 50,
            'completion_tokens': 5,
            'total_tokens': 55,
        },
    ]
    labels, totals = chatter_log_viewer._token_breakdown(
        entries
    )
    assert labels['precache']['calls'] == 2
    assert labels['precache']['measured'] == 1
    assert labels['precache']['total_tokens'] == 110
    assert labels['memory_generation']['calls'] == 1
    assert totals['calls'] == 3
    assert totals['measured'] == 2
    assert totals['total_tokens'] == 165
    assert totals['prompt_tokens'] == 150


if __name__ == '__main__':
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith('test_') and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"\n{len(tests)} tests passed")
