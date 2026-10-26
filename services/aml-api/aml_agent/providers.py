"""Fixed NVIDIA endpoint, no retries, racing or paid-provider fallback."""

from __future__ import annotations

import json
import random
import re
import threading
import time
from typing import Any

import httpx

from .profile import PLANNER, PROVIDER
from .settings import Settings


class ModelError(RuntimeError):
    pass


class ContextBudgetError(ModelError):
    pass


class TransientModelError(ModelError):
    """Connection failure, 5xx or 429: no completion was produced."""

    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


def model_messages(system: str, packet: dict) -> list[dict]:
    return [
        {
            "role": "system",
            "content": system
            + "\nReturn one JSON INSTANCE matching the response format. Do not reproduce a schema.",
        },
        {
            "role": "user",
            "content": json.dumps(packet, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def input_upper_bound(system: str, packet: dict) -> int:
    return (
        len(json.dumps(model_messages(system, packet), ensure_ascii=False).encode())
        + 128
    )


def truncation_shape(content: str) -> str:
    """Where a truncated JSON output ran away, without retaining its text."""
    keys = re.findall(r'"([A-Za-z_]{1,40})"\s*:', content)
    tail = content[-100:]
    if len(content) - len(content.rstrip()) >= 80 or not tail.strip():
        pattern = "whitespace"
    elif len(set(tail)) <= 4:
        pattern = "repeated_characters"
    else:
        pattern = "text"
    return f"last_field={keys[-1] if keys else 'none'} chars={len(content)} tail={pattern}"


RUNAWAY_WHITESPACE = 80


# NVIDIA limits this key by concurrency and by request rate (measured: about 8-10 requests a
# minute sustained). Every model call in this process passes one bounded gate and one pacer, and
# a 429 is waited out (up to about three minutes) instead of ending the case.
# One gate and one pacer per endpoint: a self-hosted server and NVIDIA's API limit independently.
GATES: dict[str, threading.BoundedSemaphore] = {}
PACE_LOCK = threading.Lock()
PACE: dict[str, float] = {}
RATE_LIMIT_ATTEMPTS = 8


def gate(endpoint: str) -> threading.BoundedSemaphore:
    with PACE_LOCK:
        return GATES.setdefault(endpoint, threading.BoundedSemaphore(PROVIDER["max_concurrent_requests"]))


def pace(endpoint: str, per_minute: float | None = None) -> None:
    """Space request starts at the endpoint's requests-per-minute (a self-hosted server may set its own)."""
    interval = 60.0 / (per_minute or PROVIDER["max_requests_per_minute"])
    with PACE_LOCK:
        start = max(time.monotonic(), PACE.get(endpoint, 0.0))
        PACE[endpoint] = start + interval
    time.sleep(max(0.0, start - time.monotonic()))


class Nvidia:
    def __init__(self, settings: Settings):
        self.settings = settings

    def complete(self, body: dict) -> dict:
        """One completion through the concurrency gate; 429s are waited out before giving up."""
        for attempt in range(RATE_LIMIT_ATTEMPTS):
            pace(self.settings.model_base_url, self.settings.requests_per_minute)
            with gate(self.settings.model_base_url):
                try:
                    return self.stream(body)
                except TransientModelError as error:
                    if "429" not in str(error) or attempt == RATE_LIMIT_ATTEMPTS - 1:
                        raise
                    wait = error.retry_after or min(5 * 2 ** attempt, 45) + random.random() * 3
            time.sleep(wait)
        raise TransientModelError("NVIDIA HTTP 429")

    def stream(self, body: dict) -> dict:
        """Stream one completion; stop early if the grammar starts emitting only whitespace.

        The JSON grammar admits unbounded whitespace between tokens, and the model can
        loop there until max_tokens. Length bounds cannot prevent that, so the stream
        is cut once a long whitespace run appears.
        """
        content, model, usage, finish, trailing, window = [], None, None, None, 0, ""
        with httpx.stream(
            "POST",
            self.settings.model_base_url + "/chat/completions",
            headers={"Authorization": "Bearer " + self.settings.nvidia_key},
            json=body,
            timeout=PROVIDER["timeout_seconds"],
            follow_redirects=False,
        ) as response:
            if response.status_code == 429 or response.status_code >= 500:
                try:
                    wait = float(response.headers.get("retry-after", 0))
                except ValueError:
                    wait = 0
                raise TransientModelError(f"NVIDIA HTTP {response.status_code}", wait)
            if response.status_code != 200:
                raise ModelError(f"NVIDIA HTTP {response.status_code}; no fallback")
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                chunk = json.loads(payload)
                model = chunk.get("model") or model
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    delta = (choice.get("delta") or {}).get("content") or ""
                    content.append(delta)
                    trailing = trailing + len(delta) if not delta.strip() else len(delta) - len(delta.rstrip())
                    window = (window + delta)[-200:]
                    finish = choice.get("finish_reason") or finish
                # A long whitespace run, or a recent window that is almost only whitespace.
                if trailing > RUNAWAY_WHITESPACE or (len(window) == 200 and len(window.split()) <= 3 and len("".join(window.split())) <= 8):
                    return {"aborted": True, "model": model, "content": "".join(content)}
        return {
            "model": model,
            "choices": [{"finish_reason": finish, "message": {"content": "".join(content)}}],
            "usage": usage or {},
        }

    def generate(self, system: str, packet: dict, schema: dict) -> tuple[dict, dict]:
        if not self.settings.nvidia_key:
            raise ModelError("NVIDIA_API_KEY missing")

        def compact(value):
            if isinstance(value, dict):
                return {
                    key: compact(item)
                    for key, item in value.items()
                    if key not in ("title", "default")
                }
            if isinstance(value, list):
                return [compact(item) for item in value]
            return value

        messages = model_messages(system, packet)
        # Conservative text-byte bound; provider token usage remains authoritative.
        if input_upper_bound(system, packet) > PLANNER["input_hard_tokens_per_request"]:
            raise ContextBudgetError("Evidence packet exceeds the local input bound")
        body = {
            "model": self.settings.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "aml_response", "schema": compact(schema), "strict": True},
            },
            "temperature": PROVIDER["temperature"],
            "top_p": 0.95,
            "max_tokens": PLANNER["output_hard_tokens_per_request_including_reasoning"],
            "chat_template_kwargs": {"enable_thinking": PROVIDER["reasoning_enabled"]},
        }
        try:
            data = self.complete(body)
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.PoolTimeout, httpx.ConnectTimeout) as error:
            # A slow or queued model (Ultra under load) is retried, not a terminal failure.
            raise TransientModelError(f"Model endpoint unavailable or slow ({type(error).__name__})") from error
        except (httpx.HTTPError, ValueError) as error:
            raise ModelError(
                "NVIDIA transport/response failure; credentials withheld"
            ) from error
        if data.get("aborted"):
            # Provider usage is unavailable for an aborted stream. Charge provable upper
            # bounds: request bytes >= prompt tokens, received characters >= completion tokens.
            return {
                "_invalid_response": "Aborted whitespace runaway; "
                + truncation_shape(data["content"])
            }, {
                "prompt_tokens": min(input_upper_bound(system, packet), PLANNER["input_hard_tokens_per_request"]),
                "completion_tokens": min(len(data["content"]), PLANNER["output_hard_tokens_per_request_including_reasoning"]),
                "usage_source": "upper_bound_aborted_stream",
            }
        if data.get("model") != self.settings.model:
            raise ModelError("Provider returned unexpected model; halt experiment")
        usage = data.get("usage", {})
        if any(
            type(usage.get(k)) is not int or usage[k] < 0
            for k in ("prompt_tokens", "completion_tokens")
        ):
            raise ModelError(
                "Missing reliable usage; halt rather than invent token counts"
            )
        if (
            usage["prompt_tokens"] > PLANNER["input_hard_tokens_per_request"]
            or usage["completion_tokens"]
            > PLANNER["output_hard_tokens_per_request_including_reasoning"]
        ):
            raise ModelError("Provider exceeded reserved request budget")
        try:
            choice = data["choices"][0]
            if choice.get("finish_reason") != "stop":
                return {
                    "_invalid_response": "Truncated or unexpected finish reason; "
                    + truncation_shape(choice.get("message", {}).get("content") or "")
                }, usage
            content = choice["message"]["content"]
            result: Any = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("Expected object")
            return result, usage
        except (KeyError, IndexError, TypeError, ValueError):
            return {
                "_invalid_response": "Invalid JSON; one budgeted repair is permitted"
            }, usage
