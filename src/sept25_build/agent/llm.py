"""LLM access for the agent.

Two tiers:
  - Liquid LFM2.5 judges every new policy sentence (cheap, small, a hackathon sponsor)
  - a stronger reviewer model (default openai/gpt-5.6-sol via OpenRouter) gives a second opinion only
    on the rare sentences that would change a verdict, or where Liquid and the regex disagree

    LLM_BACKEND=openrouter  (default when OPENROUTER_API_KEY is set)
        model liquid/lfm-2.5-2.6b:free. Free tier is capped (~50 requests/day without credits),
        so the agent only calls it when a policy paragraph actually changed.
    LLM_BACKEND=local       llama-server at LOCAL_URL (LOCAL_LFM_URL, or LFM_BASE_URL if it is localhost)

A 429/timeout pauses that backend for COOLDOWN_S (60s) instead of for the whole long-running process;
disable() is permanent (fixture replays).
"""

import json
import os
import re
import time

import openai
import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

BACKEND = os.environ.get("LLM_BACKEND") or ("openrouter" if os.environ.get("OPENROUTER_API_KEY") else "local")
_lfm_url = os.environ.get("LFM_BASE_URL", "")
# local llama-server (also the tokenizer for token counts): LOCAL_LFM_URL, else a localhost LFM_BASE_URL, else :8080
LOCAL_URL = os.environ.get("LOCAL_LFM_URL") or (
    _lfm_url if ("localhost" in _lfm_url or "127.0.0.1" in _lfm_url) else "http://localhost:8080/v1")
if BACKEND == "openrouter":
    BASE_URL = "https://openrouter.ai/api/v1"
    MODEL = os.environ.get("OPENROUTER_MODEL", "liquid/lfm-2.5-2.6b:free")
    _key = os.environ.get("OPENROUTER_API_KEY", "")
else:
    BASE_URL = LOCAL_URL
    MODEL = "LFM2.5-2.6B"
    _key = os.environ.get("LFM_API_KEY", "local")
_client = OpenAI(base_url=BASE_URL, api_key=_key or "missing", timeout=30, max_retries=0)
COOLDOWN_S = 60
_down_until = 0.0      # after a 429 or timeout, skip the backend until this monotonic time (circuit breaker)
_disabled = False      # set by disable(): fixture replays use the regex path to save quota (permanent)


def disable() -> None:
    global _disabled
    _disabled = True


REVIEW_MODEL = os.environ.get("REVIEW_MODEL", "openai/gpt-5.6-sol")
_or_key = os.environ.get("OPENROUTER_API_KEY", "")
_reviewer = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=_or_key or "missing", timeout=30, max_retries=0)
_review_down_until = 0.0


def review_available() -> bool:
    return (bool(_or_key) and not _disabled and time.monotonic() >= _review_down_until
            and REVIEW_MODEL.lower() != "none")


def review_call(system: str, user: str, schema: dict) -> tuple[dict, dict]:
    """Second-opinion call to the reviewer model. Same contract as json_call."""
    global _review_down_until
    try:
        r = _reviewer.chat.completions.create(
            model=REVIEW_MODEL, max_tokens=4000,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {"name": "out", "strict": True, "schema": schema}},
            extra_body={"reasoning": {"effort": "low"}},
        )
    except (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError) as e:
        _review_down_until = time.monotonic() + COOLDOWN_S
        raise RuntimeError(f"reviewer unavailable (pausing {COOLDOWN_S}s): {e}") from e
    except openai.APIError as e:
        raise RuntimeError(f"reviewer error: {e}") from e
    text = (r.choices[0].message.content or "") if r.choices else ""
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise ValueError(f"no JSON in reviewer output: {text[:200]!r}")
    usage = {"input_tokens": r.usage.prompt_tokens if r.usage else 0,
             "output_tokens": r.usage.completion_tokens if r.usage else 0}
    return json.loads(m.group(0)), usage


def available() -> bool:
    if _disabled:
        return False
    if time.monotonic() < _down_until:
        return False
    if BACKEND == "openrouter":
        return bool(_key)
    try:
        return requests.get(LOCAL_URL.removesuffix("/v1") + "/health", timeout=5).ok
    except requests.RequestException:
        return False


def describe() -> str:
    if _disabled:
        return "off (rules only)"
    left = _down_until - time.monotonic()
    if left > 0:
        return f"{MODEL} via {BACKEND} (cooling down {int(left) + 1}s)"
    return f"{MODEL} via {BACKEND}" + ("" if available() else " (UNAVAILABLE)")


def count_tokens(text: str) -> int:
    """Exact count from the local llama-server tokenizer if it's up; otherwise ~4 chars/token."""
    try:
        r = requests.post(LOCAL_URL.removesuffix("/v1") + "/tokenize", json={"content": text}, timeout=5)
        r.raise_for_status()
        return len(r.json()["tokens"])
    except (requests.RequestException, KeyError, ValueError):
        return len(text) // 4


def json_call(system: str, user: str, schema: dict, max_tokens: int = 2500) -> tuple[dict, dict]:
    """One structured call. Returns (parsed_json, usage).

    Raises ValueError on unparseable output, RuntimeError if the backend refuses (rate limit etc.).
    """
    global _down_until
    try:
        r = _client.chat.completions.create(
            model=MODEL, temperature=0.1, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {"name": "out", "strict": True, "schema": schema}},
            extra_body={"top_k": 50},
        )
    except openai.RateLimitError as e:
        _down_until = time.monotonic() + COOLDOWN_S
        raise RuntimeError(f"{BACKEND} rate limit (pausing {COOLDOWN_S}s): {e}") from e
    except (openai.APITimeoutError, openai.APIConnectionError) as e:
        _down_until = time.monotonic() + COOLDOWN_S  # a hung backend would otherwise stall every vendor for 30s
        raise RuntimeError(f"{BACKEND} unreachable (pausing {COOLDOWN_S}s): {e}") from e
    except openai.APIError as e:
        raise RuntimeError(f"{BACKEND} error: {e}") from e
    if not r.choices:
        raise ValueError("empty response")
    text = re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S).strip()
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise ValueError(f"no JSON in model output: {text[:200]!r}")
    usage = {"input_tokens": r.usage.prompt_tokens if r.usage else 0,
             "output_tokens": r.usage.completion_tokens if r.usage else 0}
    return json.loads(m.group(0)), usage
