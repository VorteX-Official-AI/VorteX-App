"""Streaming clients for OpenAI-compatible, Anthropic and Google Gemini APIs."""

import asyncio
import json
import time
from typing import AsyncIterator

import httpx

EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "max": "high"}
# Thinking models spend part of this budget on reasoning before writing the answer,
# so the ceilings are generous — truncation is retried with a bigger budget below.
EFFORT_MAX_TOKENS = {"low": 4096, "medium": 8192, "high": 12288, "max": 16384}
RETRY_FLOOR = 16384

THINK_PROMPT = (
    "Think this through carefully before answering. Work step by step, check your reasoning, "
    "and only then write the final answer. Keep the final answer clean and well organised."
)

BLOCK_REASONS = {
    "SAFETY",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "RECITATION",
    "SPII",
    "IMAGE_SAFETY",
}

REQUEST_TIMEOUT = httpx.Timeout(300.0, connect=20.0)

PROVIDER_DEFAULT_BASE = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "cursor": "https://api.cursor.com",
}

PROVIDER_LABELS = {
    "openai": "OpenAI-compatible",
    "anthropic": "Anthropic Claude",
    "gemini": "Google Gemini",
    "cursor": "Cursor Cloud Agent",
}

CURSOR_ALIASES = {"cursor", "cursor-agent", "cursor-cloud", "composer"}

ANTHROPIC_ALIASES = {"anthropic", "claude", "anthropic-claude"}
GEMINI_ALIASES = {"gemini", "google", "google-gemini", "google-ai"}
OPENAI_ALIASES = {
    "openai",
    "chatgpt",
    "gpt",
    "openai-compatible",
    "compatible",
    "deepseek",
    "groq",
    "xai",
    "grok",
    "mistral",
    "openrouter",
    "together",
    "perplexity",
    "fireworks",
    "azure",
    "ollama",
    "lmstudio",
}


def normalize_provider(value: str | None) -> str:
    """Map vendor aliases (deepseek, claude, google…) onto a transport type."""
    name = (value or "openai").strip().lower()
    if name in CURSOR_ALIASES:
        return "cursor"
    if name in ANTHROPIC_ALIASES:
        return "anthropic"
    if name in GEMINI_ALIASES:
        return "gemini"
    if name in OPENAI_ALIASES:
        return "openai"
    return "openai"


def provider_of(model: dict) -> str:
    return normalize_provider(model.get("provider"))


def _base(model: dict) -> str:
    return (model.get("base_url") or "").strip().rstrip("/")


def openai_base(model: dict) -> str:
    return _base(model) or PROVIDER_DEFAULT_BASE["openai"]


def anthropic_base(model: dict) -> str:
    base = _base(model) or PROVIDER_DEFAULT_BASE["anthropic"]
    if not base.rstrip("/").endswith(("/v1", "/v2")):
        base = base + "/v1"
    return base


def cursor_base(model: dict) -> str:
    base = _base(model) or PROVIDER_DEFAULT_BASE["cursor"]
    if base.rstrip("/").endswith("/v1"):
        base = base.rstrip("/")[: -len("/v1")]
    return base


def gemini_base(model: dict) -> str:
    base = _base(model) or PROVIDER_DEFAULT_BASE["gemini"]
    if not base.rstrip("/").endswith(("/v1beta", "/v1alpha", "/v1")):
        base = base + "/v1beta"
    return base


def completion_url(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if not base:
        base = PROVIDER_DEFAULT_BASE["openai"]
    return f"{base}/chat/completions"


def max_tokens_for(effort: str) -> int:
    return EFFORT_MAX_TOKENS.get(effort or "medium", EFFORT_MAX_TOKENS["medium"])


def estimate_tokens(messages: list[dict], reply: str = "") -> int:
    chars = sum(len(str(m.get("content", ""))) for m in messages) + len(reply)
    return max(1, chars // 4)


def normalize_content(content) -> list[dict]:
    """Return a message body as blocks: {"type":"text"} or {"type":"image"}."""
    if isinstance(content, list):
        blocks: list[dict] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                blocks.append({"type": "text", "text": str(block["text"])})
            elif block.get("type") == "image" and block.get("data"):
                blocks.append(
                    {
                        "type": "image",
                        "mime": block.get("mime") or "image/png",
                        "data": block["data"],
                    }
                )
        return blocks
    text = str(content or "")
    return [{"type": "text", "text": text}] if text.strip() else []


def merge_turns(messages: list[dict]) -> list[dict]:
    """Collapse consecutive same-role turns and drop a leading assistant turn.

    Gemini and Claude expect user/model messages to alternate; a failed reply can
    leave two user turns in a row, which makes the next request return nothing.
    Contents come back as block lists so text and images survive the merge.
    """
    merged: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        blocks = normalize_content(message.get("content"))
        if not blocks:
            continue
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"].extend(blocks)
        else:
            merged.append({"role": role, "content": blocks})
    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    return merged


def _openai_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            parts: list[dict] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append({"type": "text", "text": block.get("text", "")})
                elif block.get("type") == "image" and block.get("data"):
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{block.get('mime') or 'image/png'};base64,{block['data']}"
                            },
                        }
                    )
            content = parts
        out.append({"role": message.get("role"), "content": content})
    return out


def _error_text(status: int, body: str) -> str:
    text = (body or "").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return f"{status}: {text[:400] or 'provider error'}"
    if isinstance(parsed, list) and parsed:
        parsed = parsed[0]
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("status") or error.get("code")
            if message:
                return f"{status}: {str(message)[:400]}"
        if isinstance(error, str):
            return f"{status}: {error[:400]}"
        if parsed.get("message"):
            return f"{status}: {str(parsed['message'])[:400]}"
    return f"{status}: {text[:400]}"


# ---------------------------------------------------------------- OpenAI


def _openai_payload(model: dict, messages: list[dict], effort: str, think: bool, stream: bool) -> dict:
    payload = {
        "model": model["model_id"],
        "messages": _openai_messages(messages),
        "stream": stream,
    }
    if stream:
        payload["stream_options"] = {"include_usage": True}
    mapped = EFFORT_MAP.get(effort or "medium")
    if think:
        payload["reasoning_effort"] = EFFORT_MAP["high"]
    elif mapped and mapped != "medium":
        payload["reasoning_effort"] = mapped
    if not stream:
        payload["max_tokens"] = 16
    return payload


def _openai_headers(model: dict) -> dict:
    return {
        "Authorization": f"Bearer {model['api_key']}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }


async def _stream_openai(
    model: dict, messages: list[dict], effort: str, think: bool
) -> AsyncIterator[tuple[str, object]]:
    url = completion_url(openai_base(model))
    client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
    response = None
    try:
        request = client.build_request(
            "POST", url, headers=_openai_headers(model),
            json=_openai_payload(model, messages, effort, think, True),
        )
        response = await client.send(request, stream=True)

        if response.status_code >= 400:
            await response.aread()
            await response.aclose()
            # Retry once without optional parameters that some providers reject.
            request = client.build_request(
                "POST", url, headers=_openai_headers(model),
                json={"model": model["model_id"], "messages": messages, "stream": True},
            )
            response = await client.send(request, stream=True)

        if response.status_code >= 400:
            body = (await response.aread()).decode("utf-8", "replace")
            yield ("error", _error_text(response.status_code, body))
            return

        async for line in response.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                data = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if data.get("error"):
                error = data["error"]
                message = error.get("message") if isinstance(error, dict) else str(error)
                yield ("error", message or "Provider error")
                return
            usage = data.get("usage")
            if usage:
                yield ("usage", usage)
            for choice in data.get("choices") or []:
                delta = choice.get("delta") or {}
                text = delta.get("content")
                if text:
                    yield ("delta", text)
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    yield ("reasoning", reasoning)
                finish = choice.get("finish_reason")
                if finish:
                    yield ("finish", finish)
    except httpx.HTTPError as exc:
        yield ("error", f"Could not reach the model provider: {exc}")
    finally:
        if response is not None:
            try:
                await response.aclose()
            except Exception:
                pass
        try:
            await client.aclose()
        except Exception:
            pass


# ---------------------------------------------------------------- Anthropic


def _anthropic_headers(model: dict) -> dict:
    return {
        "x-api-key": model["api_key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "accept": "text/event-stream",
    }


def _anthropic_payload(model: dict, messages: list[dict], budget: int, stream: bool) -> dict:
    system_parts = [str(m.get("content") or "") for m in messages if m.get("role") == "system"]
    conversation = []
    for message in merge_turns(messages):
        parts = []
        for block in message["content"]:
            if block["type"] == "text":
                parts.append({"type": "text", "text": block["text"]})
            else:
                parts.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": block["mime"],
                            "data": block["data"],
                        },
                    }
                )
        conversation.append({"role": message["role"], "content": parts})
    payload: dict = {
        "model": model["model_id"],
        "messages": conversation,
        "max_tokens": budget,
    }
    if system_parts:
        payload["system"] = "\n\n".join(p for p in system_parts if p.strip())
    if stream:
        payload["stream"] = True
    return payload


async def _anthropic_once(
    model: dict, messages: list[dict], budget: int
) -> AsyncIterator[tuple[str, object]]:
    url = f"{anthropic_base(model)}/messages"
    client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
    response = None
    input_tokens = 0
    output_tokens = 0
    try:
        request = client.build_request(
            "POST", url, headers=_anthropic_headers(model),
            json=_anthropic_payload(model, messages, budget, True),
        )
        response = await client.send(request, stream=True)
        if response.status_code >= 400:
            body = (await response.aread()).decode("utf-8", "replace")
            yield ("error", _error_text(response.status_code, body))
            return

        async for line in response.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                data = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            kind = data.get("type")
            if kind == "content_block_delta":
                delta = data.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yield ("delta", delta["text"])
                elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                    yield ("reasoning", delta["thinking"])
            elif kind == "message_start":
                usage = (data.get("message") or {}).get("usage") or {}
                input_tokens = int(usage.get("input_tokens") or 0)
            elif kind == "message_delta":
                usage = data.get("usage") or {}
                output_tokens = int(usage.get("output_tokens") or output_tokens)
                stop_reason = (data.get("delta") or {}).get("stop_reason")
                if stop_reason:
                    yield ("finish", stop_reason)
            elif kind == "error":
                error = data.get("error") or {}
                yield ("error", error.get("message") or "Anthropic error")
                return

        total = input_tokens + output_tokens
        if total:
            yield (
                "usage",
                {
                    "total_tokens": total,
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                },
            )
    except httpx.HTTPError as exc:
        yield ("error", f"Could not reach Anthropic: {exc}")
    finally:
        if response is not None:
            try:
                await response.aclose()
            except Exception:
                pass
        try:
            await client.aclose()
        except Exception:
            pass


async def _stream_anthropic(
    model: dict, messages: list[dict], effort: str, think: bool
) -> AsyncIterator[tuple[str, object]]:
    budget = max_tokens_for(effort)
    for attempt in range(2):
        produced = False
        failed = False
        stop_reason = None
        async for kind, value in _anthropic_once(model, messages, budget):
            if kind == "finish":
                stop_reason = value
                continue
            if kind == "error":
                failed = True
            if kind == "delta":
                produced = True
            yield (kind, value)
        if failed or produced or stop_reason != "max_tokens" or attempt == 1:
            if not failed and not produced and stop_reason == "max_tokens":
                yield (
                    "error",
                    "Claude used the whole output budget before answering. Try a higher effort level.",
                )
            if stop_reason:
                yield ("finish", stop_reason)
            return
        budget = max(budget * 2, RETRY_FLOOR)


# ---------------------------------------------------------------- Gemini


def _gemini_payload(messages: list[dict], budget: int) -> dict:
    system_parts = [str(m.get("content") or "") for m in messages if m.get("role") == "system"]
    contents = []
    for message in merge_turns(messages):
        parts = []
        for block in message["content"]:
            if block["type"] == "text":
                parts.append({"text": block["text"]})
            else:
                parts.append({"inlineData": {"mimeType": block["mime"], "data": block["data"]}})
        contents.append(
            {
                "role": "model" if message["role"] == "assistant" else "user",
                "parts": parts,
            }
        )
    payload: dict = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": budget},
    }
    if system_parts:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(p for p in system_parts if p.strip())}]}
    return payload


async def _gemini_once(
    model: dict, messages: list[dict], budget: int
) -> AsyncIterator[tuple[str, object]]:
    url = f"{gemini_base(model)}/models/{model['model_id']}:streamGenerateContent?alt=sse"
    headers = {
        "x-goog-api-key": model["api_key"],
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
    response = None
    try:
        request = client.build_request(
            "POST", url, headers=headers, json=_gemini_payload(messages, budget)
        )
        response = await client.send(request, stream=True)
        if response.status_code >= 400:
            body = (await response.aread()).decode("utf-8", "replace")
            yield ("error", _error_text(response.status_code, body))
            return

        async for line in response.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                data = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if isinstance(data, list):
                data = data[0] if data else {}
            if data.get("error"):
                yield ("error", _error_text(response.status_code, json.dumps(data)))
                return

            feedback = data.get("promptFeedback") or {}
            if feedback.get("blockReason"):
                yield ("error", f"Gemini blocked this request ({feedback['blockReason']}).")
                return

            for candidate in data.get("candidates") or []:
                for part in (candidate.get("content") or {}).get("parts") or []:
                    text = part.get("text")
                    if not text:
                        continue
                    if part.get("thought"):
                        yield ("reasoning", text)
                    else:
                        yield ("delta", text)
                reason = candidate.get("finishReason")
                if reason:
                    if reason in BLOCK_REASONS:
                        yield ("error", f"Gemini stopped the response ({reason}).")
                        return
                    yield ("finish", reason)

            metadata = data.get("usageMetadata") or {}
            if metadata.get("totalTokenCount"):
                yield (
                    "usage",
                    {
                        "total_tokens": int(metadata["totalTokenCount"]),
                        "prompt_tokens": int(metadata.get("promptTokenCount") or 0),
                        "completion_tokens": int(metadata.get("candidatesTokenCount") or 0),
                    },
                )
    except httpx.HTTPError as exc:
        yield ("error", f"Could not reach Gemini: {exc}")
    finally:
        if response is not None:
            try:
                await response.aclose()
            except Exception:
                pass
        try:
            await client.aclose()
        except Exception:
            pass


GEMINI_MIN_BUDGET = 24576


async def _stream_gemini(
    model: dict, messages: list[dict], effort: str, think: bool
) -> AsyncIterator[tuple[str, object]]:
    budget = max(max_tokens_for(effort), GEMINI_MIN_BUDGET)
    for attempt in range(2):
        produced = False
        failed = False
        finish_reason = None
        async for kind, value in _gemini_once(model, messages, budget):
            if kind == "finish":
                finish_reason = value
                continue
            if kind == "error":
                failed = True
            if kind == "delta":
                produced = True
            yield (kind, value)
        if failed or produced or finish_reason != "MAX_TOKENS" or attempt == 1:
            if not failed and not produced and finish_reason == "MAX_TOKENS":
                yield (
                    "error",
                    "Gemini spent its whole output budget thinking and never answered. "
                    "Raise the effort level and try again.",
                )
            if finish_reason:
                yield ("finish", finish_reason)
            return
        # The model was still thinking when it hit the cap: retry with more room.
        budget = max(budget * 2, RETRY_FLOOR)


# ---------------------------------------------------------------- Cursor Cloud Agents

CURSOR_RUN_TIMEOUT = 900
CURSOR_POLL_SECONDS = 5
CURSOR_ACTIVE = {"CREATING", "RUNNING", "WAITING_FOR_BACKGROUND_WORK"}


def _cursor_prompt(messages: list[dict]) -> str:
    lines = []
    for item in messages:
        content = item.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            )
        text = str(content or "").strip()
        if text:
            lines.append(f"{item.get('role', 'user').upper()}: {text}")
    return "\n\n".join(lines)


def _cursor_error(status: int, body: str) -> str:
    """Turn Cursor's plan gate into a sentence that says what to do."""
    if status == 403 and "plan_required" in (body or ""):
        return (
            "Cursor Cloud Agents need a Cursor Premium plan ($30/month) or higher — Free and "
            "Plus accounts cannot run them through the API. Upgrade at cursor.com/pricing, or "
            "use another model to build locally."
        )
    return _error_text(status, body)


async def _stream_cursor(
    model: dict, messages: list[dict], repo_url: str
) -> AsyncIterator[tuple[str, object]]:
    """Cursor models only run as cloud agents against a GitHub repository."""
    key = (model.get("api_key") or "").strip()
    repo = (repo_url or "").strip()
    base = cursor_base(model)
    if not key:
        yield ("error", "Add this Cursor model's API key in the admin dashboard first.")
        return
    if not repo:
        yield (
            "error",
            "Cursor models run in Cursor's cloud against a GitHub repository, so they cannot "
            "write to your local folder. Add the repository URL to this project, or pick a "
            "Gemini or OpenAI-compatible model to build files locally.",
        )
        return

    body = {
        "prompt": {"text": _cursor_prompt(messages)},
        "model": {"id": model.get("model_id") or "composer-2"},
        "repos": [{"url": repo}],
    }
    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=20.0))
    try:
        response = await client.post(
            f"{base}/v1/agents", auth=(key, ""), json=body
        )
        if response.status_code >= 400:
            yield ("error", _cursor_error(response.status_code, response.text))
            return
        try:
            created = response.json()
        except ValueError:
            yield ("error", "Cursor returned an unexpected response.")
            return
        agent_id = (created.get("agent") or {}).get("id")
        run_id = (created.get("run") or {}).get("id")
        if not agent_id or not run_id:
            yield ("error", "Cursor did not start a run for this request.")
            return

        yield ("reasoning", f"Cursor agent started on {repo} — this run is in the cloud.")
        deadline = time.monotonic() + CURSOR_RUN_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(CURSOR_POLL_SECONDS)
            try:
                status_response = await client.get(
                    f"{base}/v1/agents/{agent_id}/runs/{run_id}", auth=(key, "")
                )
            except httpx.HTTPError as exc:
                yield ("error", f"Lost contact with Cursor while waiting: {exc}")
                return
            if status_response.status_code >= 400:
                yield ("error", _error_text(status_response.status_code, status_response.text))
                return
            try:
                run = status_response.json()
            except ValueError:
                continue
            state = str(run.get("status") or "").upper()
            if state in CURSOR_ACTIVE:
                continue
            if state == "FINISHED":
                text = str(run.get("result") or "").strip()
                branches = ((run.get("git") or {}).get("branches")) or []
                pr_url = next(
                    (
                        str(branch.get("prUrl"))
                        for branch in branches
                        if isinstance(branch, dict) and branch.get("prUrl")
                    ),
                    "",
                )
                if not text:
                    text = "The Cursor agent finished without a written summary."
                if pr_url:
                    text += f"\n\nPull request: {pr_url}"
                yield ("delta", text)
                yield (
                    "usage",
                    {
                        "total_tokens": estimate_tokens(messages, text),
                        "prompt_tokens": estimate_tokens(messages),
                        "completion_tokens": estimate_tokens([], text),
                    },
                )
                return
            reason = run.get("error") or run.get("message") or state.lower().replace("_", " ")
            yield ("error", f"Cursor run ended ({state or 'unknown'}): {reason}")
            return
        yield ("error", "Cursor is still running in the cloud. Check the agent in your Cursor dashboard.")
    except httpx.HTTPError as exc:
        yield ("error", f"Could not reach Cursor: {exc}")
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


# ---------------------------------------------------------------- dispatch


async def stream_chat(
    model: dict,
    messages: list[dict],
    *,
    effort: str = "medium",
    think: bool = False,
    repo_url: str = "",
) -> AsyncIterator[tuple[str, object]]:
    """Yield deltas, reasoning, usage, errors and provider finish reasons."""
    provider = provider_of(model)
    if provider == "cursor":
        generator = _stream_cursor(model, messages, repo_url)
    elif provider == "anthropic":
        generator = _stream_anthropic(model, messages, effort, think)
    elif provider == "gemini":
        generator = _stream_gemini(model, messages, effort, think)
    else:
        generator = _stream_openai(model, messages, effort, think)
    async for event in generator:
        yield event


async def list_models(model: dict, api_key: str | None = None) -> tuple[bool, list[str], str]:
    """Ask the provider which model IDs this key can actually use."""
    provider = provider_of(model)
    key = (api_key or model.get("api_key") or "").strip()
    if not key:
        return False, [], "An API key is required to load models."

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if provider == "anthropic":
                response = await client.get(
                    f"{anthropic_base(model)}/models?limit=1000",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                )
                if response.status_code >= 400:
                    return False, [], _error_text(response.status_code, response.text)
                payload = response.json()
                ids = [item.get("id") for item in payload.get("data") or [] if item.get("id")]
            elif provider == "cursor":
                response = await client.get(
                    f"{cursor_base(model)}/v1/models", auth=(key, "")
                )
                if response.status_code >= 400:
                    return False, [], _cursor_error(response.status_code, response.text)
                payload = response.json()
                ids = [
                    item.get("id")
                    for item in payload.get("items") or payload.get("models") or []
                    if isinstance(item, dict) and item.get("id")
                ]
            elif provider == "gemini":
                response = await client.get(
                    f"{gemini_base(model)}/models?pageSize=200",
                    headers={"x-goog-api-key": key},
                )
                if response.status_code >= 400:
                    return False, [], _error_text(response.status_code, response.text)
                payload = response.json()
                ids = [
                    str(item.get("name", "")).split("/")[-1]
                    for item in payload.get("models") or []
                    if "generateContent" in (item.get("supportedGenerationMethods") or [])
                ]
            else:
                response = await client.get(
                    f"{openai_base(model)}/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
                if response.status_code >= 400:
                    return False, [], _error_text(response.status_code, response.text)
                payload = response.json()
                data = payload.get("data") or payload.get("models") or []
                ids = [item.get("id") for item in data if isinstance(item, dict) and item.get("id")]
    except httpx.HTTPError as exc:
        return False, [], f"Could not reach provider: {exc}"
    except ValueError:
        return False, [], "The provider returned an unexpected response."

    cleaned = sorted({str(i) for i in ids if i})
    if not cleaned:
        return False, [], "The provider returned no models for this key."
    return True, cleaned, f"{len(cleaned)} models available"


async def check_connection(model: dict) -> tuple[bool, str]:
    """Send a tiny request to confirm the key, base URL and model ID work."""
    provider = provider_of(model)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if provider == "cursor":
                response = await client.get(
                    f"{cursor_base(model)}/v1/models",
                    auth=((model.get("api_key") or "").strip(), ""),
                )
                if response.status_code >= 400:
                    return False, _cursor_error(response.status_code, response.text)
                return True, "Cursor key accepted"
            if provider == "anthropic":
                url = f"{anthropic_base(model)}/messages"
                headers = _anthropic_headers(model)
                payload = _anthropic_payload(
                    model,
                    [{"role": "user", "content": "Reply with the single word: ready"}],
                    32,
                    False,
                )
            elif provider == "gemini":
                url = f"{gemini_base(model)}/models/{model['model_id']}:generateContent"
                headers = {
                    "x-goog-api-key": model["api_key"],
                    "Content-Type": "application/json",
                }
                payload = {
                    "contents": [
                        {"role": "user", "parts": [{"text": "Reply with the single word: ready"}]}
                    ],
                    "generationConfig": {"maxOutputTokens": 256},
                }
            else:
                url = completion_url(openai_base(model))
                headers = {
                    "Authorization": f"Bearer {model['api_key']}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": model["model_id"],
                    "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
                    "max_tokens": 16,
                    "stream": False,
                }

            response = await client.post(url, headers=headers, json=payload)
            if response.status_code >= 400:
                return False, _error_text(response.status_code, response.text)
            return True, "Connection OK"
    except httpx.HTTPError as exc:
        return False, f"Could not reach provider: {exc}"
