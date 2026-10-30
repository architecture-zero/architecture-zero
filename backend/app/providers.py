import os
import json
import logging
from urllib.parse import urlparse

import requests as req

from app import model_catalog

logger = logging.getLogger(__name__)

# Multi-provider support - enable any combination via ENABLE_* flags.
# Backward compat: if PROVIDER is set (and ENABLE_* are not), derive from it.
_legacy        = os.getenv("PROVIDER", "ollama").lower()
ENABLE_OLLAMA     = os.getenv("ENABLE_OLLAMA",     "true"  if _legacy == "ollama"    else "false").lower() == "true"
ENABLE_ANTHROPIC  = os.getenv("ENABLE_ANTHROPIC",  "true"  if _legacy == "anthropic" else "false").lower() == "true"
ENABLE_OPENAI     = os.getenv("ENABLE_OPENAI",     "true"  if _legacy == "openai"    else "false").lower() == "true"

OLLAMA_BASE          = os.getenv("OLLAMA_BASE", "http://host.docker.internal:11434")
# Context window for every Ollama request - see _ollama_options (Ollama's own
# default is ~4k and it truncates the SYSTEM PROMPT away in silence). 0 = leave
# it to Ollama.
OLLAMA_NUM_CTX       = int(os.getenv("OLLAMA_NUM_CTX", "32768"))
# Service-token headers for an Ollama base behind Cloudflare Access (a remote
# GPU box exposed through a tunnel). Empty = no extra headers.
CF_ACCESS_CLIENT_ID  = os.getenv("CF_ACCESS_CLIENT_ID", "")
CF_ACCESS_CLIENT_SECRET = os.getenv("CF_ACCESS_CLIENT_SECRET", "")
OPENAI_KEY    = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE   = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_VER = "2023-06-01"

# The line a lane answers with when the model returns NOTHING - no text and no
# tool call. The newer models can end a stream with a refusal stop (the
# provider's own classifier) and no content at all; read as an answer, that is
# silence: a deployed instance answered a jailbreak question with its sources and [DONE]
# and zero output tokens (2026-10-22, found by its eval's security cohort and
# reproduced live). One sentence, true for a refusal and for an empty
# completion alike, so no lane ever serves an empty turn.
EMPTY_TURN_TEXT = "I can't answer that."


def _get_runtime(db_key: str, env_key: str, default: str = "") -> str:
    """Read from config DB; fall back to env var; then to default. Called
    per-request so values update without restart."""
    try:
        from app.config import get_config, is_secret_config_key, decrypt_secret
        val = get_config(db_key, "")
        if val:
            # Secret rows are Fernet at rest; legacy plaintext passes through.
            return decrypt_secret(val) if is_secret_config_key(db_key) else val
    except Exception:
        pass
    return os.getenv(env_key, default)


def _get_runtime_bool(db_key: str, env_key: str, env_default: str = "false") -> bool:
    try:
        from app.config import get_config
        val = get_config(db_key, "")
        if val:
            return val.lower() == "true"
    except Exception:
        pass
    return os.getenv(env_key, env_default).lower() == "true"


# -- OpenAI-compatible provider registry -------------------------------------
# Every entry speaks the OpenAI chat/completions dialect, so ONE adapter (the
# _openai_* functions below) serves them all - adding a provider is a registry
# entry plus a key, never a new adapter. Per-provider config follows one
# convention:
#   key:  config db `<name>_api_key`   / env `<NAME>_API_KEY`
#   base: config db `<name>_base_url`  / env `<NAME>_BASE_URL`  (override only)
# A provider with no key configured is dormant: visible to get_provider_config
# as unconfigured, absent from the model picker, and any call to it fails with
# the provider's own auth error.
#
# Routing: `prefixes` maps bare model names to a provider. Providers whose
# model names have no unique prefix (Groq serves llama/qwen names that would
# fall through to Ollama) are reached with an explicit "provider:model" id,
# e.g. "groq:llama-3.3-70b-versatile" - see _resolve_model.
OPENAI_COMPAT: dict[str, dict] = {
    "openai":   {"label": "OpenAI",   "base": "https://api.openai.com/v1",
                 "prefixes": ("gpt-", "o1-", "o3-", "o4-")},
    "gemini":   {"label": "Gemini",   "base": "https://generativelanguage.googleapis.com/v1beta/openai",
                 "prefixes": ("gemini-",)},
    "mistral":  {"label": "Mistral",  "base": "https://api.mistral.ai/v1",
                 "prefixes": ("mistral-", "magistral-", "codestral-", "ministral-",
                              "pixtral-", "open-mistral", "open-mixtral")},
    "groq":     {"label": "Groq",     "base": "https://api.groq.com/openai/v1",
                 "prefixes": ()},
    "xai":      {"label": "xAI",      "base": "https://api.x.ai/v1",
                 "prefixes": ("grok-",)},
    "deepseek": {"label": "DeepSeek", "base": "https://api.deepseek.com",
                 "prefixes": ("deepseek-",)},
}


def _resolve_model(model: str) -> tuple[str, str]:
    """Resolve a picker model id to (provider, bare_model_id).

    A COLON marks an Ollama tag ("qwen3:8b", "mistral:7b", "deepseek-r1:7b",
    "gpt-oss:20b") unless the name is an explicit "provider:model" namespace:
    "anthropic:" and "ollama:" always, a registry provider's name only when
    the rest is one of THAT provider's own ids - it starts with the provider's
    routing prefixes, or anything for a provider with none (groq). The picker
    lists local models by their Ollama tag and every vendor id bare or
    groq-namespaced, and no vendor id carries a colon, so a vendor's name or
    prefix inside a tagged local name must never send it off the box. Until
    2026-09-30 "mistral:7b" went to api.mistral.ai as model "7b", and
    "deepseek-r1:7b" to DeepSeek, wherever that vendor was keyed - the prompt
    and its retrieved passages with it (fixed 2026-09-30). Names without
    a colon route by the Anthropic / registry prefixes; Ollama is the
    fallback.

    A Claude family alias ("claude-opus-latest") is resolved FIRST, to the
    newest model of that family the key can use (model_catalog.resolve), so
    every call through here sends a concrete id; anything else is unchanged
    by that step."""
    model = model_catalog.resolve(model)
    if ":" in model:
        head, tail = model.split(":", 1)
        if head in ("anthropic", "ollama"):
            return head, tail
        entry = OPENAI_COMPAT.get(head)
        if entry is not None and (not entry["prefixes"]
                                  or tail.startswith(entry["prefixes"])):
            return head, tail
        return "ollama", model
    if model.startswith("claude-"):
        return "anthropic", model
    for name, entry in OPENAI_COMPAT.items():
        if entry["prefixes"] and model.startswith(entry["prefixes"]):
            return name, model
    return "ollama", model


def _provider_for_model(model: str) -> str:
    """Provider name only - kept for callers that don't need the bare id."""
    return _resolve_model(model)[0]


def _compat_base(provider: str) -> str:
    """Base URL for an OpenAI-compatible provider (runtime-overridable)."""
    entry = OPENAI_COMPAT[provider]
    base = _get_runtime(f"{provider}_base_url", f"{provider.upper()}_BASE_URL",
                        entry["base"])
    return base.rstrip("/")


# THE CAP'S FIELD FOLLOWS THE HOST (2026-10-29). OpenAI's reasoning models
# (the o-series, gpt-5) refuse a request that carries max_tokens and require
# max_completion_tokens, which OpenAI's current chat models also accept; every
# other host that speaks the dialect (Groq, Mistral, xAI, DeepSeek, Gemini's
# compatible endpoint, a self-hosted server) takes max_tokens. Every request on
# this path that sends a cap names its field through _cap_field, decided by the
# base URL the request goes to rather than the provider's name, so the openai
# entry pointed at another compatible server sends that server's field.
_OPENAI_API_HOST = "api.openai.com"


def _cap_field(base_url: str) -> str:
    """The reply cap's field for a request to `base_url`: max_completion_tokens
    on OpenAI's own API, max_tokens on every other host."""
    host = (urlparse(base_url or "").hostname or "").lower()
    return "max_completion_tokens" if host == _OPENAI_API_HOST else "max_tokens"


def _compat_headers(provider: str) -> dict:
    """Bearer-auth headers for an OpenAI-compatible provider."""
    key = _get_runtime(f"{provider}_api_key", f"{provider.upper()}_API_KEY", "")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def compat_key_configured(provider: str) -> bool:
    return bool(_get_runtime(f"{provider}_api_key", f"{provider.upper()}_API_KEY", ""))


# -- Ollama -------------------------------------------------------------------

def _ollama_headers() -> dict:
    """CF-Access service token headers when configured, empty dict otherwise."""
    cid = CF_ACCESS_CLIENT_ID or os.getenv("CF_ACCESS_CLIENT_ID", "")
    sec = CF_ACCESS_CLIENT_SECRET or os.getenv("CF_ACCESS_CLIENT_SECRET", "")
    if cid and sec:
        return {"CF-Access-Client-Id": cid, "CF-Access-Client-Secret": sec}
    return {}


def _ollama_message_events(msg: dict):
    """Normalise one Ollama message (a stream chunk's or a full response's)
    into the shared event shape. Tool calls arrive WHOLE with parsed
    arguments - Ollama never fragments them like OpenAI does, so no
    accumulator is needed."""
    token = msg.get("content", "")
    if token:
        yield {"type": "text", "text": token}
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        yield {"type": "tool_call", "id": tc.get("id", ""), "name": fn.get("name", ""), "args": args}


def _ollama_options(options: dict | None = None) -> dict | None:
    """Merge the configured context window into every Ollama request.

    Ollama does NOT use the model's advertised context length - it applies its
    own small default (~4k) unless num_ctx is passed, and it truncates
    SILENTLY from the FRONT. That front is the system prompt: the safety and
    grounding rules live there, so an over-long turn quietly becomes an
    unguarded, rule-free model - no error, no warning, a plausible answer.
    Every backend Ollama request MUST come through here.

    Tune per GPU with OLLAMA_NUM_CTX (VRAM holds the KV cache); 0 restores
    Ollama's default.
    """
    merged = dict(options or {})
    if OLLAMA_NUM_CTX > 0:
        merged.setdefault("num_ctx", OLLAMA_NUM_CTX)
    return merged or None


def bounded_window_tokens(model: str) -> int | None:
    """The context window, in tokens, that a turn sent to `model` must fit
    where this code sets it - else None (2026-10-29). The Ollama lane runs
    with num_ctx (OLLAMA_NUM_CTX, merged by _ollama_options) and cuts an
    over-long prompt from the front, where the system prompt is; the chat
    trims its history to this figure. OLLAMA_NUM_CTX=0 hands the window to
    Ollama's own default, a figure this code does not know, so it is None.
    A vendor model's window is the one its vendor's /models list declares
    (model_catalog.listed_window, 2026-10-29): Groq lists 8192-token models
    and Mistral its legacy open-* ids at 32768, under the chat's input bound,
    and such a vendor refused a long conversation with an error. A vendor
    model whose list gives no window is None, as every cloud lane was: an
    over-long request there is refused with an error, not cut."""
    if _provider_for_model(model) == "ollama":
        return OLLAMA_NUM_CTX if OLLAMA_NUM_CTX > 0 else None
    return model_catalog.listed_window(model)


def _ollama_chat_nonstream(messages: list, model: str, tools: list | None = None,
                           options: dict | None = None) -> dict:
    """Single buffered /api/chat call. FALLBACK ONLY: a non-streaming request
    sends zero response bytes until generation finishes, and a proxied/
    tunneled base will kill any origin silent past its idle timeout
    (Cloudflare: 524 at ~100s). Only reached when an older Ollama (<0.8)
    rejects streaming with tools."""
    base = _get_runtime("ollama_base_url", "OLLAMA_BASE", OLLAMA_BASE)
    payload: dict = {"model": model, "messages": messages, "stream": False}
    opts = _ollama_options(options)
    if opts:
        payload["options"] = opts
    if tools:
        payload["tools"] = tools
    resp = req.post(f"{base}/api/chat", json=payload, headers=_ollama_headers(), timeout=300)
    resp.raise_for_status()
    data = resp.json()
    model_catalog.note_if_cut(data.get("done_reason"), model, (opts or {}).get("num_predict"))
    return data


def _ollama_stream_events(messages: list, model: str, tools: list | None = None,
                          options: dict | None = None):
    """Ollama as normalised events - streaming WITH tools in the payload.

    Streaming here is load-bearing, not cosmetic: when OLLAMA_BASE points at a
    remote GPU box through a tunnel, tokens flowing keep the connection alive
    for the whole generation; a buffered call times out at the proxy. Ollama
    >= 0.8 streams tool calls, so the old buffered-when-tools degradation is
    gone. A server that still rejects stream+tools (HTTP 400) gets one
    buffered fallback call - fine on a LAN base, still timeout-prone through a
    tunnel, so keep the GPU box's Ollama current.
    """
    base = _get_runtime("ollama_base_url", "OLLAMA_BASE", OLLAMA_BASE)
    payload: dict = {"model": model, "messages": messages, "stream": True}
    opts = _ollama_options(options)
    if opts:
        payload["options"] = opts
    if tools:
        payload["tools"] = tools
    try:
        resp = req.post(f"{base}/api/chat", json=payload, headers=_ollama_headers(),
                        stream=True, timeout=300)
        resp.raise_for_status()
    except req.exceptions.HTTPError:
        resp.close()  # the 4xx stream would otherwise pin its connection
        if tools and resp.status_code == 400:
            data = _ollama_chat_nonstream(messages, model, tools, options)
            yield from _ollama_message_events(data.get("message", {}))
            return
        raise
    done_reason = None
    with resp:
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except Exception:
                continue
            # Ollama reports failures as {"error": ...} lines on a 200 stream
            # (proxy timeouts, model load failures). Reading only .message
            # drops them silently and the turn comes back empty - fail loud
            # instead.
            if chunk.get("error"):
                raise ValueError(
                    f"Ollama stream error: {str(chunk['error'])[:200]}")
            if chunk.get("done"):
                done_reason = chunk.get("done_reason") or done_reason
            yield from _ollama_message_events(chunk.get("message", {}))
    # The cap's cut on record, as the Anthropic streamer records it: Ollama's
    # last line says done_reason "length" when num_predict ended the answer.
    model_catalog.note_if_cut(done_reason, model, (opts or {}).get("num_predict"))


def _ollama_tool_call(messages: list, model: str, tools: list,
                      max_tokens: int | None = None) -> dict:
    """Buffered-INTERFACE call for non-chat callers (history summary, eval
    runner). Streams under the hood and assembles the full message - the
    interface stayed, the timeout-prone transport didn't. The caller's cap
    rides as num_predict; none given keeps this door's 16384."""
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    cap = 16384 if max_tokens is None else max_tokens
    for event in _ollama_stream_events(messages, model, tools or None,
                                       options={"num_predict": cap}):
        if event["type"] == "text":
            text_parts.append(event["text"])
        elif event["type"] == "tool_call":
            tool_calls.append({
                "id": event.get("id", ""),
                "type": "function",
                "function": {"name": event.get("name", ""), "arguments": event.get("args", {})},
            })
    return {"message": {"content": "".join(text_parts), "tool_calls": tool_calls}}


# -- OpenAI-compatible (OpenAI, Gemini, Mistral, Groq, xAI, DeepSeek) --------

def _openai_tool_call(messages: list, model: str, tools: list,
                      provider: str = "openai", max_tokens: int | None = None) -> dict:
    base = _compat_base(provider)
    payload: dict = {"model": model, "messages": messages, "tools": tools}
    # THE CALLER'S CAP, WHEN IT GIVES ONE (2026-10-29): this door sent none,
    # so a history summary on this path ran to the model's own limit. The
    # field is the host's (_cap_field). A call with no cap - the eval
    # runner's tool rounds - sends exactly what it sent before.
    if max_tokens is not None:
        payload[_cap_field(base)] = max_tokens
    resp = req.post(f"{base}/chat/completions",
                    headers=_compat_headers(provider),
                    json=payload,
                    timeout=120)
    resp.raise_for_status()
    choice = resp.json()["choices"][0]
    model_catalog.note_if_cut(choice.get("finish_reason"), model, max_tokens)
    # Normalise to Ollama-style {message: {content, tool_calls}}
    return {"message": choice["message"]}


# -- Anthropic ----------------------------------------------------------------

def _to_anthropic_tools(tools: list) -> list:
    """Convert OpenAI-format tool list to Anthropic format."""
    result = []
    for t in tools:
        fn = t.get("function", t)
        result.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return result


def _to_anthropic_messages(messages: list) -> list:
    """Convert OpenAI-format messages (including tool roles) to Anthropic
    format."""
    result = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            i += 1
            continue

        if role == "user":
            if result and result[-1]["role"] == "user":
                prev = result[-1]["content"]
                if isinstance(prev, str):
                    result[-1]["content"] = prev + "\n" + content
                else:
                    # List content = a tool_results user message. A plain user
                    # message following it (an empty-answer retry nudge) must
                    # join as a text block AFTER the tool_result blocks -
                    # silently dropping it makes the retry a byte-identical
                    # replay.
                    prev.append({"type": "text", "text": content})
            else:
                result.append({"role": "user", "content": content})
            i += 1

        elif role == "assistant":
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                blocks = []
                if content:
                    blocks.append({"type": "text", "text": content})
                tool_use_ids = []
                for j, tc in enumerate(tool_calls):
                    fn = tc.get("function", {})
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    tid = tc.get("id") or f"toolu_{i:04d}_{j:02d}"
                    tool_use_ids.append(tid)
                    blocks.append({
                        "type": "tool_use",
                        "id": tid,
                        "name": fn.get("name", ""),
                        "input": args,
                    })
                result.append({"role": "assistant", "content": blocks})
                i += 1
                # Consume following tool result messages positionally
                tool_results = []
                for tid in tool_use_ids:
                    if i < len(messages) and messages[i].get("role") == "tool":
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tid,
                            "content": messages[i].get("content", ""),
                        })
                        i += 1
                if tool_results:
                    result.append({"role": "user", "content": tool_results})
            else:
                if content:
                    if result and result[-1]["role"] == "assistant":
                        prev = result[-1]["content"]
                        if isinstance(prev, str):
                            result[-1]["content"] = prev + "\n" + content
                    else:
                        result.append({"role": "assistant", "content": content})
                i += 1

        else:
            i += 1

    while result and result[0]["role"] != "user":
        result.pop(0)
    return result


def _anthropic_headers() -> dict:
    key = _get_runtime("anthropic_api_key", "ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    return {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VER,
        "Content-Type": "application/json",
    }


def _anthropic_system_blocks(system_prompt: str, messages: list) -> list | None:
    """Build the Messages-API `system` field as content blocks with a
    prompt-cache breakpoint (`cache_control: ephemeral`) on the FIRST block.

    Anthropic caching is a prefix match over tools -> system -> messages, so
    the breakpoint on block 1 caches the tool definitions plus the stable
    system core server-side (~90% off on re-reads, 5-min sliding TTL).
    Variable content - conditional suffixes, mid-history summaries - lands in
    a second, uncached block, so toggling it never busts the cached core.

    Callers pass the STABLE core via `system_prompt` and may also carry the
    full prompt (core + suffixes) as a system-role message. A system message
    that startswith the core is split: the core stays in block 1, the
    remainder becomes the uncached tail. The same rule prevents a caller
    passing the SAME string both ways from having it joined twice into
    `system`, doubling those tokens on every call.

    Prefixes under the model's minimum cacheable length are silently not
    cached by the API - the marker is harmless.
    """
    parts: list[str] = []
    if system_prompt:
        parts.append(system_prompt)
    for m in messages:
        if m.get("role") != "system":
            continue
        content = m.get("content", "")
        if not content:
            continue
        if system_prompt and content.startswith(system_prompt):
            content = content[len(system_prompt):]  # core already sits in block 1
            if not content:
                continue
        parts.append(content)
    if not parts:
        return None
    blocks: list[dict] = [{"type": "text", "text": parts[0],
                           "cache_control": {"type": "ephemeral"}}]
    if len(parts) > 1:
        blocks.append({"type": "text", "text": "\n\n".join(parts[1:])})
    return blocks


def _log_anthropic_usage(usage: dict, model: str):
    """Caching is FAIL-SILENT - off looks identical to on except in billing -
    so the positive signal lives in the logs: cache_read_input_tokens > 0 on a
    repeat call is the proof it's working; stuck at 0 means a silent
    invalidator (or a below-minimum prefix). Grep: 'anthropic_usage'."""
    if not usage:
        return
    logger.info(
        "anthropic_usage model=%s input=%s cache_read=%s cache_creation=%s",
        model, usage.get("input_tokens"),
        usage.get("cache_read_input_tokens"),
        usage.get("cache_creation_input_tokens"))


def _anthropic_stream_events(messages: list, model: str, system_prompt: str = "",
                             max_tokens: int = 1024, tools: list | None = None):
    """Event-dict Anthropic streamer (wired in via stream_chat_events).

    Streams the Messages API, yielding normalised event dicts so the caller
    can tell text apart from tool calls:

        {"type": "text", "text": "..."}                       # stream to the user
        {"type": "tool_call", "id", "name", "args": {...}}    # run the tool, then loop

    Tool-call arguments arrive as a STREAM of JSON fragments
    (input_json_delta), so we accumulate them per content-block and only
    json.loads once the block closes (content_block_stop).
    """
    merged = _to_anthropic_messages(messages)
    system_blocks = _anthropic_system_blocks(system_prompt, messages)

    payload: dict = {
        "model": model,
        "messages": merged,
        # The caller's cap holds the answer; a model that thinks first gets
        # room for its thinking on top (model_catalog.output_room).
        "max_tokens": model_catalog.output_room(model, max_tokens),
        "stream": True,
    }
    if system_blocks:
        payload["system"] = system_blocks
    if tools:
        payload["tools"] = _to_anthropic_tools(tools)

    # index -> {"id", "name", "args_buf"} - one entry per in-flight tool_use block
    tool_blocks: dict[int, dict] = {}
    stop_reason = None

    with req.post("https://api.anthropic.com/v1/messages",
                  headers=_anthropic_headers(), json=payload, stream=True, timeout=300) as resp:
        if not resp.ok:
            # Bounded as the stream's error branches below are (2026-10-29):
            # the vendor's whole body went into the exception, and so the
            # log; its error JSON leads with the type, so 200 keeps it.
            raise ValueError(f"Anthropic {resp.status_code}: {resp.text[:200]}")
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode() if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            try:
                chunk = json.loads(line[5:].strip())
            except Exception:
                continue

            ctype = chunk.get("type")

            # Mid-stream API errors (overloaded_error etc.) arrive as SSE
            # events on a 200 stream. Unhandled they are silently SKIPPED:
            # the round yields zero events and an agentic loop reads that as
            # "final answer", serving an empty turn. Fail loud; the caller
            # surfaces it.
            if ctype == "error":
                err = chunk.get("error") or {}
                raise ValueError(
                    f"Anthropic stream error: {err.get('type', '?')}: "
                    f"{str(err.get('message', ''))[:200]}")

            if ctype == "message_start":
                _log_anthropic_usage(
                    chunk.get("message", {}).get("usage") or {}, model)

            # A block begins - note it if it's a tool call, so we can collect
            # its args.
            elif ctype == "content_block_start":
                block = chunk.get("content_block", {})
                if block.get("type") == "tool_use":
                    tool_blocks[chunk.get("index")] = {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "args_buf": "",
                    }

            # A piece of a block arrives - text streams out now; tool args
            # accumulate.
            elif ctype == "content_block_delta":
                delta = chunk.get("delta", {})
                dtype = delta.get("type")
                if dtype == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        yield {"type": "text", "text": text}
                elif dtype == "input_json_delta":
                    tb = tool_blocks.get(chunk.get("index"))
                    if tb is not None:
                        tb["args_buf"] += delta.get("partial_json", "")

            # A block ends - if it was a tool call, the buffer is now complete
            # JSON.
            elif ctype == "content_block_stop":
                tb = tool_blocks.pop(chunk.get("index"), None)
                if tb is not None:
                    try:
                        args = json.loads(tb["args_buf"]) if tb["args_buf"] else {}
                    except Exception:
                        args = {}
                    yield {"type": "tool_call", "id": tb["id"], "name": tb["name"], "args": args}

            elif ctype == "message_delta":
                stop_reason = (chunk.get("delta") or {}).get("stop_reason") or stop_reason

    model_catalog.note_if_cut(stop_reason, model, payload["max_tokens"])
    if stop_reason == "refusal":
        # The provider's classifier ended the turn; usually with no content at
        # all. stream_chat_events answers an empty turn with EMPTY_TURN_TEXT;
        # this line is the operator's record of why.
        logger.warning("%s ended the stream with a refusal stop", model)


def _anthropic_tool_call(messages: list, model: str, tools: list,
                         max_tokens: int | None = None) -> dict:
    """Non-streaming Anthropic call for the agentic tool loop. The caller's
    cap, with the thinking room output_room adds; none given keeps 8192."""
    anthropic_messages = _to_anthropic_messages(messages)
    if not anthropic_messages:
        return {"message": {"content": "", "tool_calls": []}}

    system_blocks = _anthropic_system_blocks("", messages)

    payload: dict = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": model_catalog.output_room(
            model, 8192 if max_tokens is None else max_tokens),
    }
    if tools:
        payload["tools"] = _to_anthropic_tools(tools)
    if system_blocks:
        payload["system"] = system_blocks

    resp = req.post(
        "https://api.anthropic.com/v1/messages",
        headers=_anthropic_headers(),
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    _log_anthropic_usage(data.get("usage") or {}, model)
    model_catalog.note_if_cut(data.get("stop_reason"), model, payload["max_tokens"])

    text_parts = []
    tool_calls = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": block.get("input", {}),
                },
            })

    if not text_parts and not tool_calls:
        # The same empty turn the streaming lane guards against, on the
        # non-streaming door: a refusal stop or an empty completion must not
        # read as a blank answer.
        logger.warning("%s returned an empty completion (stop_reason=%s) - answering with the empty-turn line",
                       model, data.get("stop_reason"))
        text_parts.append(EMPTY_TURN_TEXT)

    return {"message": {"content": "".join(text_parts), "tool_calls": tool_calls}}


def _openai_stream_events(messages: list, model: str, tools: list | None = None,
                          max_tokens: int = 1024, provider: str = "openai"):
    """OpenAI-dialect streaming as normalised events (text + tool_call), for
    every registry provider - `provider` picks the base URL + key.

    Tool-call arguments arrive as JSON-string fragments under
    delta.tool_calls, keyed by index - accumulate per index, parse once the
    stream ends.
    """
    base = _compat_base(provider)
    # The cap's field by the host (_cap_field, 2026-10-29): OpenAI's own API
    # takes max_completion_tokens, every other compatible host max_tokens.
    payload: dict = {"model": model, "messages": messages, "stream": True,
                     _cap_field(base): max_tokens}
    if tools:
        payload["tools"] = tools

    tool_accum: dict[int, dict] = {}  # index -> {"id", "name", "args_buf"}
    finish_reason = None
    with req.post(f"{base}/chat/completions",
                  headers=_compat_headers(provider), json=payload, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode() if isinstance(raw, bytes) else raw
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except Exception:
                continue
            # Same fail-loud rule as the Anthropic/Ollama streamers: an
            # in-stream error object must not read as a clean empty answer.
            if chunk.get("error"):
                err = chunk["error"]
                msg = err.get("message") if isinstance(err, dict) else err
                raise ValueError(
                    f"{provider} stream error: {str(msg)[:200]}")
            choices = chunk.get("choices") or []
            if not choices:
                continue
            finish_reason = choices[0].get("finish_reason") or finish_reason
            delta = choices[0].get("delta", {})
            text = delta.get("content") or ""
            if text:
                yield {"type": "text", "text": text}
            for tc in delta.get("tool_calls") or []:
                slot = tool_accum.setdefault(tc.get("index", 0), {"id": "", "name": "", "args_buf": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args_buf"] += fn["arguments"]

    # The cap's cut on record, as the Anthropic streamer records it: the
    # dialect's finish_reason says "length" when max_tokens ended the answer.
    model_catalog.note_if_cut(finish_reason, model, max_tokens)
    for idx in sorted(tool_accum):
        slot = tool_accum[idx]
        if not slot["name"]:
            continue
        try:
            args = json.loads(slot["args_buf"]) if slot["args_buf"] else {}
        except Exception:
            args = {}
        yield {"type": "tool_call", "id": slot["id"], "name": slot["name"], "args": args}


# -- Public API ---------------------------------------------------------------

def stream_chat_events(messages: list, model: str, tools: list | None = None,
                       system_prompt: str = "", max_tokens: int = 1024):
    """Yield normalised events from the active provider:

        {"type": "text", "text": "..."}                       # stream to the user
        {"type": "tool_call", "id", "name", "args": {...}}    # run the tool, then loop

    One shape for all providers (adapter layer) - the caller doesn't care
    which provider produced it.
    """
    provider, bare = _resolve_model(model)
    if provider == "anthropic":
        events = _anthropic_stream_events(messages, bare, system_prompt, max_tokens, tools or None)
    elif provider == "ollama":
        # The caller's cap rides as num_predict (2026-10-29): the Anthropic
        # lane above and the OpenAI-shaped one below already honour
        # max_tokens, and this branch dropped it - the chat's response_tokens
        # never reached Ollama, so a local reply ran to the model's own stop,
        # past the reply room the history trim keeps for it in num_ctx. Same
        # shape as _ollama_tool_call's options: num_ctx still merges in
        # through _ollama_options, and note_if_cut records a local cut as it
        # does a cloud one. A cap of 0 or less is no cap: num_predict is left
        # out rather than sent as a figure Ollama gives its own meaning.
        options = ({"num_predict": max_tokens}
                   if isinstance(max_tokens, int) and max_tokens > 0 else None)
        events = _ollama_stream_events(messages, bare, tools or None, options=options)
    else:
        events = _openai_stream_events(messages, bare, tools or None, max_tokens,
                                       provider=provider)
    # One exit for every provider, so the guard sits here: a turn that yields
    # no event at all - no text, no tool call - is never an answer. A refusal
    # stop (the provider's classifier), an empty completion, a reply that was
    # all thinking: each used to reach the caller as silence, and the chat lane
    # served it as such. The visitor gets one plain sentence instead.
    yielded = False
    for event in events:
        yielded = True
        yield event
    if not yielded:
        logger.warning("%s returned an empty turn (no text, no tool call) - answering with the empty-turn line",
                       model)
        yield {"type": "text", "text": EMPTY_TURN_TEXT}


def stream_chat(messages: list, model: str, tools: list | None = None,
                system_prompt: str = "", max_tokens: int = 1024):
    """Yield text tokens only - back-compat wrapper over stream_chat_events
    for callers that don't handle tool calls (summaries and other text-only
    paths)."""
    for event in stream_chat_events(messages, model, tools=tools,
                                    system_prompt=system_prompt, max_tokens=max_tokens):
        if event.get("type") == "text":
            yield event["text"]


def non_stream_tool_call(messages: list, model: str, tools: list,
                         max_tokens: int | None = None) -> dict:
    """Non-streaming call for the agentic loop. Returns Ollama-normalised
    dict.

    `max_tokens` caps the reply on every lane (2026-10-29): the history
    summarizer passes its own bound. None keeps each door's own figure as
    before - no cap on the OpenAI-style door, 16384 on Ollama, 8192 with
    thinking room on Anthropic - which is what the eval runner's calls send."""
    provider, bare = _resolve_model(model)
    if provider == "anthropic":
        # Its door answers an empty completion itself, naming the stop reason.
        return _anthropic_tool_call(messages, bare, tools, max_tokens=max_tokens)
    elif provider == "ollama":
        data = _ollama_tool_call(messages, bare, tools, max_tokens=max_tokens)
    else:
        data = _openai_tool_call(messages, bare, tools, provider=provider,
                                 max_tokens=max_tokens)
    # The empty turn on the other two doors (2026-10-23): a completion with no
    # text and no tool call reached the caller as a blank answer, the same
    # silence the streaming exit and the Anthropic door already answer.
    msg = data.get("message") or {}
    if not (msg.get("content") or "") and not msg.get("tool_calls"):
        logger.warning("%s returned an empty completion - answering with the empty-turn line", model)
        data = {**data, "message": {**msg, "content": EMPTY_TURN_TEXT, "tool_calls": []}}
    return data


def supports_tools(model: str = "") -> bool:
    provider = _provider_for_model(model)
    return provider in ("ollama", "anthropic") or provider in OPENAI_COMPAT


def get_provider_config() -> dict:
    ollama_en    = _get_runtime_bool("provider_ollama_enabled",    "ENABLE_OLLAMA",    "true" if ENABLE_OLLAMA    else "false")
    anthropic_en = _get_runtime_bool("provider_anthropic_enabled", "ENABLE_ANTHROPIC", "true" if ENABLE_ANTHROPIC else "false")
    openai_en    = _get_runtime_bool("provider_openai_enabled",    "ENABLE_OPENAI",    "true" if ENABLE_OPENAI    else "false")
    enabled = []
    if ollama_en:    enabled.append("ollama")
    if anthropic_en: enabled.append("anthropic")
    if openai_en:    enabled.append("openai")
    cfg = {
        "provider":             enabled[0] if len(enabled) == 1 else "multi",
        "enabled_providers":    enabled,
        "anthropic_configured": bool(_get_runtime("anthropic_api_key", "ANTHROPIC_API_KEY", ANTHROPIC_KEY)),
    }
    # Registry providers are dormant-until-keyed: configured == usable.
    for name in OPENAI_COMPAT:
        cfg[f"{name}_configured"] = compat_key_configured(name)
    return cfg


def offered_providers() -> set[str]:
    """The providers this instance OFFERS - ONE predicate behind the model
    picker (/api/models) and the chat route's dispatch gate (ruled 2026-09-21),
    so the two cannot disagree: a model the picker shows is dispatchable, and a
    model the picker would not show is refused before its name prefix routes it
    to a provider the operator turned off. Ollama by its runtime toggle;
    Anthropic and OpenAI by their toggle OR a configured key (the registry's
    dormant-until-keyed rule); registry providers by a key alone."""
    cfg = get_provider_config()
    offered = set(cfg["enabled_providers"])
    if cfg["anthropic_configured"]:
        offered.add("anthropic")
    for name in OPENAI_COMPAT:
        if compat_key_configured(name):
            offered.add(name)
    return offered
