"""LLM access for the agent: Liquid LFM2.5, via OpenRouter (default) or a local llama-server.

    LLM_BACKEND=openrouter  (default when OPENROUTER_API_KEY is set)
        model liquid/lfm-2.5-2.6b:free. Free tier is capped (~50 requests/day without credits),
        so the agent only calls it when a policy paragraph actually changed.
    LLM_BACKEND=local       llama-server at LFM_BASE_URL (see AGENTS.md "Setup")
"""

import json
import os
import re

import openai
import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

BACKEND = os.environ.get("LLM_BACKEND") or ("openrouter" if os.environ.get("OPENROUTER_API_KEY") else "local")
LOCAL_URL = os.environ.get("LOCAL_LFM_URL", "http://localhost:8080/v1")  # also the tokenizer for token counts
if BACKEND == "openrouter":
    BASE_URL = "https://openrouter.ai/api/v1"
    MODEL = os.environ.get("OPENROUTER_MODEL", "liquid/lfm-2.5-2.6b:free")
    _key = os.environ.get("OPENROUTER_API_KEY", "")
else:
    BASE_URL = LOCAL_URL
    MODEL = "LFM2.5-2.6B"
    _key = os.environ.get("LFM_API_KEY", "local")
_client = OpenAI(base_url=BASE_URL, api_key=_key or "missing", timeout=30, max_retries=0)
_rate_limited = False  # after a 429 or timeout, stop calling the backend for this process (circuit breaker)
_disabled = False      # set by disable(): fixture replays use the regex path to save quota


def disable() -> None:
    global _disabled
    _disabled = True


def available() -> bool:
    if _disabled:
        return False
    if _rate_limited:
        return False
    if BACKEND == "openrouter":
        return bool(_key)
    try:
        return requests.get(LOCAL_URL.removesuffix("/v1") + "/health", timeout=5).ok
    except requests.RequestException:
        return False


def describe() -> str:
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
    global _rate_limited
    try:
        r = _client.chat.completions.create(
            model=MODEL, temperature=0.1, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {"name": "out", "strict": True, "schema": schema}},
            extra_body={"top_k": 50},
        )
    except openai.RateLimitError as e:
        _rate_limited = True
        raise RuntimeError(f"{BACKEND} rate limit: {e}") from e
    except (openai.APITimeoutError, openai.APIConnectionError) as e:
        _rate_limited = True  # a hung backend would otherwise stall every vendor for 30s
        raise RuntimeError(f"{BACKEND} unreachable: {e}") from e
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
