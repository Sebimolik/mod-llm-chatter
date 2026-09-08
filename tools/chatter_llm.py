"""LLM call-layer helpers extracted from chatter_shared (N14)."""

import logging
import threading
import time
from typing import Any, Optional

from chatter_constants import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_GOOGLE_MODEL,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    GOOGLE_OPENAI_BASE_URL,
    OPENROUTER_BASE_URL,
)

logger = logging.getLogger(__name__)


def _split_prompt(prompt):
    """Extract system/user parts from a prompt.

    Returns (system_msg, user_msg). system_msg is
    None for plain str prompts.
    """
    from chatter_shared import PromptParts
    if isinstance(prompt, PromptParts) and prompt.system_prompt:
        return prompt.system_prompt, prompt.user_prompt
    return None, str(prompt)


def _build_chat_messages(sys_msg, user_content):
    """Build OpenAI-style messages list with optional
    system message."""
    messages = []
    if sys_msg:
        messages.append({
            "role": "system",
            "content": sys_msg,
        })
    messages.append({
        "role": "user",
        "content": user_content,
    })
    return messages


def _build_anthropic_request_kwargs(
    model, max_tokens, temperature, sys_msg, user_msg
):
    """Build Anthropic SDK v1-compatible request arguments."""
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": user_msg,
        }],
        "extra_body": {
            "temperature": temperature,
        },
    }
    if sys_msg:
        kwargs["system"] = sys_msg
    return kwargs


def _openrouter_headers(config):
    """Build optional OpenRouter app-attribution headers."""
    headers = {}
    referer = str(config.get(
        'LLMChatter.OpenRouter.HttpReferer', ''
    )).strip()
    title = str(config.get(
        'LLMChatter.OpenRouter.Title', ''
    )).strip()
    if referer:
        headers['HTTP-Referer'] = referer
    if title:
        headers['X-OpenRouter-Title'] = title
    return headers or None


def _openrouter_reasoning_effort(config):
    """Return the configured OpenRouter reasoning effort."""
    effort = str(config.get(
        'LLMChatter.OpenRouter.ReasoningEffort', ''
    )).strip()
    return effort or None


def _openrouter_reasoning_enabled(config):
    """Return whether OpenRouter reasoning needs extra output budget."""
    effort = _openrouter_reasoning_effort(config)
    return bool(effort) and effort.lower() != 'none'


def _apply_openrouter_options(kwargs, config):
    """Attach opt-in OpenRouter reasoning request options."""
    effort = _openrouter_reasoning_effort(config)
    if not effort:
        return

    # Unlike Google, "none" is sent explicitly so hybrid models are
    # forced into their non-reasoning mode instead of using defaults.
    # Normalize only the "none" sentinel; other values keep their
    # configured case since supported effort spellings vary by model.
    if effort.lower() == 'none':
        effort = 'none'
    reasoning = {'effort': effort}
    if str(config.get(
        'LLMChatter.OpenRouter.ReasoningExclude', '0'
    )).strip() == '1':
        reasoning['exclude'] = True
    kwargs['extra_body'] = {'reasoning': reasoning}


def _ollama_user_msg(user_msg, config):
    """Apply Ollama-specific transforms to user msg
    (e.g. /no_think prefix)."""
    disable_thinking = (
        config.get(
            'LLMChatter.Ollama.DisableThinking',
            '1',
        ) == '1'
    )
    if disable_thinking:
        return "/no_think " + user_msg
    return user_msg


def _google_reasoning_effort(config):
    """Return Gemini OpenAI-compatible reasoning effort."""
    if _google_thinking_config(config):
        return None
    effort = str(config.get(
        'LLMChatter.Google.ReasoningEffort', 'minimal'
    )).strip().lower()
    if not effort or effort in ('0', 'none', 'off', 'disabled'):
        return None
    return effort


def _google_thinking_config(config):
    """Return Gemini thinking_config for OpenAI compatibility."""
    raw_budget = str(config.get(
        'LLMChatter.Google.ThinkingBudget', ''
    )).strip()
    if not raw_budget:
        return None
    try:
        return {'thinking_budget': int(raw_budget)}
    except (TypeError, ValueError):
        logger.warning(
            "Invalid LLMChatter.Google.ThinkingBudget=%r",
            raw_budget,
        )
        return None


def _apply_google_options(kwargs, config):
    """Attach Gemini-specific OpenAI compatibility options."""
    thinking_config = _google_thinking_config(config)
    if thinking_config:
        kwargs['extra_body'] = {
            'extra_body': {
                'google': {
                    'thinking_config': thinking_config,
                },
            },
        }
        return

    effort = _google_reasoning_effort(config)
    if effort:
        kwargs['reasoning_effort'] = effort


def _effective_max_tokens(provider, config, max_tokens):
    """Adjust provider-specific output budget."""
    if provider == 'google':
        config_key = 'LLMChatter.Google.MaxTokensMultiplier'
        default_multiplier = 2
    elif (
        provider == 'openrouter'
        and _openrouter_reasoning_enabled(config)
    ):
        config_key = (
            'LLMChatter.OpenRouter.MaxTokensMultiplier'
        )
        default_multiplier = 1
    else:
        return max_tokens

    try:
        multiplier = float(config.get(
            config_key, default_multiplier
        ))
    except (TypeError, ValueError):
        multiplier = float(default_multiplier)
    multiplier = max(1.0, min(multiplier, 8.0))
    return int(max_tokens * multiplier)


def _extract_chat_content(response, label=''):
    """Extract text from an OpenAI-compatible chat response."""
    choice = response.choices[0]
    message = choice.message
    content = getattr(message, 'content', None)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                text = part.get('text')
            else:
                text = getattr(part, 'text', None)
            if text:
                parts.append(text)
        if parts:
            return ''.join(parts).strip()

    finish_reason = getattr(choice, 'finish_reason', None)
    tool_calls = getattr(message, 'tool_calls', None)
    logger.warning(
        "LLM returned no text content (%s): "
        "finish_reason=%s tool_calls=%s",
        label, finish_reason, bool(tool_calls),
    )
    return None


def _usage_int(usage, *names):
    """Return the first int-valued attribute/key found.

    Providers hand back either a pydantic-ish object or a
    plain dict, so both are probed. Anything non-int (or
    missing) yields None -- never a guess.
    """
    for name in names:
        value = None
        if isinstance(usage, dict):
            value = usage.get(name)
        else:
            value = getattr(usage, name, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def _extract_usage(response):
    """Pull REAL token counts off a provider response.

    Handles the OpenAI-compatible shape (usage.
    prompt_tokens / completion_tokens / total_tokens --
    used by OpenAI, OpenRouter, DeepSeek, Google's
    OpenAI-compat endpoint and Ollama) and Anthropic's
    (usage.input_tokens / output_tokens, which carries no
    total, so it is summed only when both halves are
    present).

    Returns None when the provider reported nothing, so the
    logger can omit the fields entirely rather than write
    fabricated zeros. Never raises: usage accounting must
    not be able to break an otherwise good LLM call.
    """
    try:
        usage = getattr(response, 'usage', None)
        if usage is None and isinstance(response, dict):
            usage = response.get('usage')
        if usage is None:
            return None
        prompt_tokens = _usage_int(
            usage, 'prompt_tokens', 'input_tokens'
        )
        completion_tokens = _usage_int(
            usage, 'completion_tokens', 'output_tokens'
        )
        total_tokens = _usage_int(usage, 'total_tokens')
        if (
            total_tokens is None
            and prompt_tokens is not None
            and completion_tokens is not None
        ):
            total_tokens = prompt_tokens + completion_tokens
        out = {}
        if prompt_tokens is not None:
            out['prompt_tokens'] = prompt_tokens
        if completion_tokens is not None:
            out['completion_tokens'] = completion_tokens
        if total_tokens is not None:
            out['total_tokens'] = total_tokens
        return out or None
    except Exception:
        return None


def resolve_model(model_name: str) -> str:
    """Resolve friendly model aliases to provider model IDs."""
    normalized = (model_name or '').strip()
    aliases = {
        'haiku': DEFAULT_ANTHROPIC_MODEL,
        'gpt4o-mini': DEFAULT_OPENAI_MODEL,
        'gpt-4o-mini': DEFAULT_OPENAI_MODEL,
        'openrouter-auto': 'openrouter/auto',
        'google-2.5-flash': 'gemini-2.5-flash',
        'google2.5-flash': 'gemini-2.5-flash',
        'gemini-2.5-flash': 'gemini-2.5-flash',
        'google-3.1-flash-lite': 'gemini-3.1-flash-lite',
        'google3.1-flash-lite': 'gemini-3.1-flash-lite',
        'gemini-3.1-flash-lite': 'gemini-3.1-flash-lite',
        'google-3-flash': 'gemini-3-flash-preview',
        'google3-flash': 'gemini-3-flash-preview',
        'gemini-3-flash': 'gemini-3-flash-preview',
        'gemini-3-flash-preview': 'gemini-3-flash-preview',
    }
    return aliases.get(normalized.lower(), normalized)


_main_client = None
_main_client_provider = None
_main_client_lock = threading.Lock()


def _client_timeout(config):
    """Request timeout, in seconds, for every provider client.

    Both SDKs default to ~600s with retries. Internal work runs on
    single-worker executors, so an unbounded call does not just delay
    itself -- it blocks every queued condensation or relationship
    update behind it. Bounding this trades a rare slow success for a
    predictable failure the next pass picks up again.
    """
    raw = str(
        config.get('LLMChatter.RequestTimeoutSeconds', '') or ''
    ).strip()
    if not raw:
        return DEFAULT_REQUEST_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "LLMChatter.RequestTimeoutSeconds=%r is not a number;"
            " using %ss", raw, DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )
        return DEFAULT_REQUEST_TIMEOUT_SECONDS
    if value <= 0:
        # 0/negative means "no timeout"; honour it, but say so, since
        # it reinstates the stall this default exists to prevent.
        logger.warning(
            "LLMChatter.RequestTimeoutSeconds=%s disables the request"
            " timeout; a wedged provider call will block internal"
            " work indefinitely.", value,
        )
        return None
    return value


def get_llm_client(config):
    """Get or create the main LLM client.

    Thread-safe, lazily initialised, cached by
    provider. Returns the client object suitable
    for passing to call_llm().
    """
    global _main_client, _main_client_provider

    provider = config.get(
        'LLMChatter.Provider', 'anthropic'
    ).lower()

    with _main_client_lock:
        if (
            _main_client is not None
            and _main_client_provider == provider
        ):
            return _main_client

        if provider == 'ollama':
            import openai
            base_url = config.get(
                'LLMChatter.Ollama.BaseUrl',
                'http://localhost:11434',
            )
            _main_client = openai.OpenAI(
                base_url=(
                    f"{base_url.rstrip('/')}/v1"
                ),
                api_key="ollama",
                timeout=_client_timeout(config),
            )
        elif provider == 'openai':
            import openai
            _main_client = openai.OpenAI(
                api_key=config.get(
                    'LLMChatter.OpenAI.ApiKey', ''
                ),
                timeout=_client_timeout(config),
            )
        elif provider == 'google':
            import openai
            _main_client = openai.OpenAI(
                api_key=config.get(
                    'LLMChatter.Google.ApiKey', ''
                ),
                base_url=config.get(
                    'LLMChatter.Google.BaseUrl',
                    GOOGLE_OPENAI_BASE_URL,
                ),
                timeout=_client_timeout(config),
            )
        elif provider == 'openrouter':
            import openai
            kwargs = {
                'api_key': config.get(
                    'LLMChatter.OpenRouter.ApiKey', ''
                ),
                'base_url': config.get(
                    'LLMChatter.OpenRouter.BaseUrl',
                    OPENROUTER_BASE_URL,
                ),
            }
            kwargs['timeout'] = _client_timeout(config)
            headers = _openrouter_headers(config)
            if headers:
                kwargs['default_headers'] = headers
            _main_client = openai.OpenAI(**kwargs)
        else:
            import anthropic
            _main_client = anthropic.Anthropic(
                api_key=config.get(
                    'LLMChatter.Anthropic.ApiKey',
                    '',
                ),
                timeout=_client_timeout(config),
            )

        _main_client_provider = provider
        return _main_client


def call_llm(
    client: Any,
    prompt: str,
    config: dict,
    max_tokens_override: int = None,
    context: str = '',
    *,
    label: str = '',
    metadata: dict = None,
    use_quick_model: bool = False,
) -> str:
    """Call LLM API.

    Supports Anthropic, OpenAI, Google, OpenRouter, and Ollama.

    use_quick_model: route this call to the cheap
    QuickAnalyze provider/model (see
    _resolve_quick_target()) instead of LLMChatter.Model.
    Intended for text a player never reads verbatim --
    stored memories, digests, dispositions. Falls back to
    the main client/model transparently when QuickAnalyze
    is not configured, so callers can pass it
    unconditionally. Everything else (retry-free error
    handling, token accounting, request logging, the
    max_tokens/temperature budget) is shared with a normal
    call.
    """
    provider = config.get(
        'LLMChatter.Provider', 'anthropic'
    ).lower()
    default_model = DEFAULT_ANTHROPIC_MODEL
    if provider == 'openai':
        default_model = DEFAULT_OPENAI_MODEL
    elif provider == 'google':
        default_model = DEFAULT_GOOGLE_MODEL
    elif provider == 'openrouter':
        default_model = DEFAULT_OPENROUTER_MODEL
    model = config.get(
        'LLMChatter.Model', default_model
    )
    model = resolve_model(model)
    if use_quick_model:
        client, provider, model = (
            _resolve_quick_target(client, config)
        )
    if max_tokens_override is not None:
        max_tokens = max_tokens_override
    else:
        max_tokens = int(
            config.get('LLMChatter.MaxTokens', 200)
        )
    request_max_tokens = _effective_max_tokens(
        provider, config, max_tokens
    )
    temperature = float(
        config.get('LLMChatter.Temperature', 0.85)
    )

    t0 = time.monotonic()
    result = None
    usage = None
    sys_msg, user_msg = _split_prompt(prompt)
    sent_user_msg = user_msg  # tracks actual payload
    try:
        if provider == 'ollama':
            sent_user_msg = _ollama_user_msg(
                user_msg, config
            )
            context_size = int(
                config.get(
                    'LLMChatter.Ollama'
                    '.ContextSize', 2048
                )
            )
            response = client.chat.completions.create(
                model=model,
                max_tokens=request_max_tokens,
                temperature=temperature,
                messages=_build_chat_messages(
                    sys_msg, sent_user_msg
                ),
                extra_body={
                    "options": {
                        "num_ctx": context_size
                    }
                }
            )
            result = _extract_chat_content(
                response, label
            )
            usage = _extract_usage(response)
        elif provider in ('openai', 'google', 'openrouter'):
            kwargs = {
                'model': model,
                'max_tokens': request_max_tokens,
                'temperature': temperature,
                'messages': _build_chat_messages(
                    sys_msg, user_msg
                ),
            }
            if provider == 'google':
                _apply_google_options(kwargs, config)
            elif provider == 'openrouter':
                _apply_openrouter_options(kwargs, config)
            response = client.chat.completions.create(
                **kwargs
            )
            result = _extract_chat_content(
                response, label
            )
            usage = _extract_usage(response)
        else:
            # Anthropic (default)
            kwargs = _build_anthropic_request_kwargs(
                model,
                request_max_tokens,
                temperature,
                sys_msg,
                user_msg,
            )
            response = client.messages.create(
                **kwargs
            )
            usage = _extract_usage(response)
            result = response.content[0].text.strip()
    except Exception as exc:
        logger.error(
            "LLM call failed (%s): %s", label, exc
        )
        result = None
    finally:
        duration_ms = int(
            (time.monotonic() - t0) * 1000
        )
        try:
            from chatter_request_logger import (
                log_request,
            )
            log_request(
                label, sent_user_msg, result,
                model, provider, duration_ms,
                metadata=metadata,
                system_prompt=sys_msg,
                usage=usage,
            )
        except Exception:
            pass
    return result


# Cached client for quick analyze when provider
# differs from main provider
_quick_analyze_client = None
_quick_analyze_provider = None
_quick_analyze_lock = threading.Lock()


def _get_quick_analyze_client(config):
    """Get or create the LLM client for quick
    analyze calls. Returns (client, provider).

    If QuickAnalyze.Provider matches the main
    provider (or is empty), returns None so the
    caller uses the main client.

    Thread-safe: lazy init protected by lock.
    """
    global _quick_analyze_client
    global _quick_analyze_provider

    qa_provider = config.get(
        'LLMChatter.QuickAnalyze.Provider', ''
    ).strip().lower()
    main_provider = config.get(
        'LLMChatter.Provider', 'anthropic'
    ).lower()

    # Empty = use main provider
    if not qa_provider or qa_provider == main_provider:
        return None, main_provider

    with _quick_analyze_lock:
        # Return cached client if already created
        if (
            _quick_analyze_client is not None
            and _quick_analyze_provider == qa_provider
        ):
            return _quick_analyze_client, qa_provider

        # Create new client for the quick analyze
        # provider
        if qa_provider == 'ollama':
            import openai
            base_url = config.get(
                'LLMChatter.Ollama.BaseUrl',
                'http://localhost:11434'
            )
            ollama_api_url = (
                f"{base_url.rstrip('/')}/v1"
            )
            _quick_analyze_client = openai.OpenAI(
                base_url=ollama_api_url,
                api_key="ollama",
                timeout=_client_timeout(config),
            )
        elif qa_provider == 'openai':
            import openai
            api_key = config.get(
                'LLMChatter.OpenAI.ApiKey', ''
            )
            if not api_key:
                return None, main_provider
            _quick_analyze_client = openai.OpenAI(
                api_key=api_key,
                timeout=_client_timeout(config),
            )
        elif qa_provider == 'google':
            import openai
            api_key = config.get(
                'LLMChatter.Google.ApiKey', ''
            )
            if not api_key:
                return None, main_provider
            _quick_analyze_client = openai.OpenAI(
                api_key=api_key,
                base_url=config.get(
                    'LLMChatter.Google.BaseUrl',
                    GOOGLE_OPENAI_BASE_URL,
                ),
                timeout=_client_timeout(config),
            )
        elif qa_provider == 'openrouter':
            import openai
            api_key = config.get(
                'LLMChatter.OpenRouter.ApiKey', ''
            )
            if not api_key:
                return None, main_provider
            kwargs = {
                'api_key': api_key,
                'base_url': config.get(
                    'LLMChatter.OpenRouter.BaseUrl',
                    OPENROUTER_BASE_URL,
                ),
            }
            headers = _openrouter_headers(config)
            if headers:
                kwargs['default_headers'] = headers
            kwargs['timeout'] = _client_timeout(config)
            _quick_analyze_client = openai.OpenAI(**kwargs)
        elif qa_provider == 'anthropic':
            import anthropic
            api_key = config.get(
                'LLMChatter.Anthropic.ApiKey', ''
            )
            if not api_key:
                return None, main_provider
            _quick_analyze_client = anthropic.Anthropic(
                api_key=api_key,
                timeout=_client_timeout(config),
            )
        else:
            return None, main_provider

        _quick_analyze_provider = qa_provider
        return _quick_analyze_client, qa_provider


# Per-provider default model, used only when a separate
# QuickAnalyze provider is configured without naming a model.
_PROVIDER_DEFAULT_MODELS = {
    'anthropic': DEFAULT_ANTHROPIC_MODEL,
    'openai': DEFAULT_OPENAI_MODEL,
    'google': DEFAULT_GOOGLE_MODEL,
    'openrouter': DEFAULT_OPENROUTER_MODEL,
}


def _resolve_quick_target(client, config):
    """Resolve the client/provider/model for cheap
    "internal" LLM work (QuickAnalyze).

    Returns (client, provider, model). When QuickAnalyze is
    not configured on this server the fallback is fully
    transparent on EVERY provider: the caller's own client,
    the main provider, and LLMChatter.Model itself. An
    unconfigured server behaves exactly as if the call had
    gone through the main path, which is what the config
    documentation promises.

    A provider default is used only when the admin has
    deliberately pointed QuickAnalyze at a separate provider
    and left its model unset -- there is no "main model" for
    that provider to inherit.

    Previously Anthropic and OpenAI substituted a hardcoded
    cheaper model here regardless of LLMChatter.Model, which
    silently moved every internal call (and, since the memory
    subsystem routes through this, all memory bookkeeping)
    off the admin's chosen model.

    Shared by quick_llm_analyze() and by
    call_llm(use_quick_model=True) so there is exactly one
    definition of "the cheap model".
    """
    qa_client, provider = (
        _get_quick_analyze_client(config)
    )
    if qa_client is not None:
        active_client = qa_client
        using_quick_provider = True
    else:
        active_client = client
        using_quick_provider = False

    qa_model = config.get(
        'LLMChatter.QuickAnalyze.Model', ''
    ).strip()

    provider_default = _PROVIDER_DEFAULT_MODELS.get(
        provider, DEFAULT_ANTHROPIC_MODEL
    )

    if qa_model:
        # Explicitly configured: always wins.
        model = qa_model
    elif using_quick_provider:
        # A separate quick provider was configured but given no
        # model, so there is no main model to inherit -- fall back
        # to that provider's own default.
        model = provider_default
    else:
        # No QuickAnalyze configuration at all. Stay on the admin's
        # model so this is a genuine no-op, on every provider.
        model = config.get(
            'LLMChatter.Model', provider_default
        )
    return active_client, provider, resolve_model(model)


def quick_llm_analyze(
    client: Any,
    config: dict,
    prompt: str,
    max_tokens: int = 50,
    *,
    label: str = '',
    metadata: dict = None,
) -> Optional[str]:
    """Fast LLM call for pre-processing analysis.

    Uses the configured QuickAnalyze provider/model,
    or defaults to the fastest model on the main
    provider (Haiku for Anthropic, gpt-4o-mini for
    OpenAI, Gemini Flash for Google, OpenRouter's
    configured model, main model for Ollama).

    Useful for tasks like:
    - Determining which bot a player is addressing
    - Classifying message intent or sentiment
    - Summarizing context before a full prompt

    Returns raw text response, or None on error.
    """
    active_client, provider, model = (
        _resolve_quick_target(client, config)
    )

    t0 = time.monotonic()
    result = None
    usage = None
    sys_msg, user_msg = _split_prompt(prompt)
    sent_user_msg = user_msg
    try:
        if provider == 'ollama':
            sent_user_msg = _ollama_user_msg(
                user_msg, config
            )
            context_size = int(config.get(
                'LLMChatter.Ollama.ContextSize',
                2048
            ))
            response = (
                active_client
                .chat.completions.create(
                model=model,
                max_tokens=_effective_max_tokens(
                    provider, config, max_tokens
                ),
                temperature=0.1,
                    messages=_build_chat_messages(
                        sys_msg, sent_user_msg
                    ),
                    extra_body={
                        "options": {
                            "num_ctx": context_size
                        }
                    }
                )
            )
            result = _extract_chat_content(
                response, label
            )
            usage = _extract_usage(response)
        elif provider in ('openai', 'google', 'openrouter'):
            kwargs = {
                'model': model,
                'max_tokens': _effective_max_tokens(
                    provider, config, max_tokens
                ),
                'temperature': 0.1,
                'messages': _build_chat_messages(
                    sys_msg, user_msg
                ),
            }
            if provider == 'google':
                _apply_google_options(kwargs, config)
            elif provider == 'openrouter':
                _apply_openrouter_options(kwargs, config)
            response = (
                active_client
                .chat.completions.create(
                    **kwargs
                )
            )
            result = _extract_chat_content(
                response, label
            )
            usage = _extract_usage(response)
        else:
            kwargs = _build_anthropic_request_kwargs(
                model,
                max_tokens,
                0.1,
                sys_msg,
                user_msg,
            )
            response = (
                active_client.messages.create(
                    **kwargs
                )
            )
            usage = _extract_usage(response)
            result = response.content[0].text.strip()
    except Exception as exc:
        logger.error(
            "LLM call failed (%s): %s", label, exc
        )
        result = None
    finally:
        duration_ms = int(
            (time.monotonic() - t0) * 1000
        )
        try:
            from chatter_request_logger import (
                log_request,
            )
            log_request(
                label, sent_user_msg, result,
                model, provider, duration_ms,
                metadata=metadata,
                system_prompt=sys_msg,
                usage=usage,
            )
        except Exception:
            pass
    return result
