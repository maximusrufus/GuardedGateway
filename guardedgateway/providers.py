"""Upstream provider clients. All network I/O goes through a module-level
`httpx.AsyncClient` so tests can substitute an `httpx.MockTransport` (or
`respx`) and never touch a real network — see tests/conftest.py.

Not copied from another repo — small, gateway-specific adapters translating
OpenAI-wire-format chat requests into each provider's native call shape.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

#: overridable per-process for tests: set to an httpx.AsyncClient built on a
#: MockTransport (or respx) before any provider call.
_client: httpx.AsyncClient | None = None


def set_client(client: httpx.AsyncClient) -> None:
    global _client
    _client = client


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=60.0)
    return _client


@dataclass
class CompletionResult:
    text: str
    prompt_tokens: int
    completion_tokens: int


class UpstreamError(RuntimeError):
    """Any failure calling an upstream provider — triggers the circuit breaker."""


async def call_fake(model: str, messages: list[dict], max_tokens: int) -> CompletionResult:
    """Built-in provider requiring no network call at all — always available,
    always free, used for local testing and as the ultimate fallback."""
    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    text = f"[fake/{model}] echo: {last_user}"
    prompt_tokens = sum(len(m.get("content", "")) for m in messages) // 4
    completion_tokens = len(text) // 4
    return CompletionResult(
        text=text, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
    )


async def call_ollama(
    model: str, messages: list[dict], max_tokens: int, base_url: str
) -> CompletionResult:
    client = get_client()
    try:
        resp = await client.post(
            f"{base_url.rstrip('/')}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {"num_predict": max_tokens},
            },
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        raise UpstreamError(f"ollama call failed: {e}") from e
    text = data.get("message", {}).get("content", "")
    return CompletionResult(
        text=text,
        prompt_tokens=int(data.get("prompt_eval_count") or 0),
        completion_tokens=int(data.get("eval_count") or 0),
    )


async def call_openai(
    model: str, messages: list[dict], max_tokens: int, api_key: str
) -> CompletionResult:
    client = get_client()
    try:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens},
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        raise UpstreamError(f"openai call failed: {e}") from e
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return CompletionResult(
        text=text,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
    )


async def call_anthropic(
    model: str, messages: list[dict], max_tokens: int, api_key: str
) -> CompletionResult:
    client = get_client()
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    user_messages = [m for m in messages if m["role"] != "system"]
    try:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            json={
                "model": model,
                "system": system,
                "messages": user_messages,
                "max_tokens": max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        raise UpstreamError(f"anthropic call failed: {e}") from e
    text = "".join(block.get("text", "") for block in data.get("content", []))
    usage = data.get("usage", {})
    return CompletionResult(
        text=text,
        prompt_tokens=int(usage.get("input_tokens") or 0),
        completion_tokens=int(usage.get("output_tokens") or 0),
    )


async def call_gemini(
    model: str, messages: list[dict], max_tokens: int, api_key: str
) -> CompletionResult:
    client = get_client()
    contents = [
        {"role": "user" if m["role"] != "assistant" else "model", "parts": [{"text": m["content"]}]}
        for m in messages
        if m["role"] != "system"
    ]
    try:
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": api_key},
            json={"contents": contents, "generationConfig": {"maxOutputTokens": max_tokens}},
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        raise UpstreamError(f"gemini call failed: {e}") from e
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    usage = data.get("usageMetadata", {})
    return CompletionResult(
        text=text,
        prompt_tokens=int(usage.get("promptTokenCount") or 0),
        completion_tokens=int(usage.get("candidatesTokenCount") or 0),
    )


async def dispatch(
    provider: str,
    model_name: str,
    messages: list[dict],
    max_tokens: int,
    *,
    api_key: str | None = None,
    ollama_base_url: str = "http://localhost:11434",
) -> CompletionResult:
    """Route to the right provider client. `model_name` is the bare model
    name (no 'provider/' prefix — that's stripped by the caller)."""
    if provider == "fake":
        return await call_fake(model_name, messages, max_tokens)
    if provider == "ollama":
        return await call_ollama(model_name, messages, max_tokens, ollama_base_url)
    if provider == "openai":
        if not api_key:
            raise UpstreamError("openai requires a BYOK provider key configured for this tenant")
        return await call_openai(model_name, messages, max_tokens, api_key)
    if provider == "anthropic":
        if not api_key:
            raise UpstreamError("anthropic requires a BYOK provider key configured for this tenant")
        return await call_anthropic(model_name, messages, max_tokens, api_key)
    if provider == "gemini":
        if not api_key:
            raise UpstreamError("gemini requires a BYOK provider key configured for this tenant")
        return await call_gemini(model_name, messages, max_tokens, api_key)
    raise ValueError(f"unknown provider {provider!r}")
