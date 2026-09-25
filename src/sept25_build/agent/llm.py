"""LLM access for the agent: Liquid LFM2.5 on the local llama-server (OpenAI-compatible)."""

import json
import os
import re

import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

BASE_URL = os.environ.get("LFM_BASE_URL", "http://localhost:8080/v1")
MODEL = os.environ.get("LFM_MODEL", "LFM2.5-2.6B")
_client = OpenAI(base_url=BASE_URL, api_key=os.environ.get("LFM_API_KEY", "local"), timeout=90)


def available() -> bool:
    try:
        return requests.get(BASE_URL.removesuffix("/v1") + "/health", timeout=2).ok
    except requests.RequestException:
        return False


def count_tokens(text: str) -> int:
    """Exact count from the llama-server tokenizer; ~4 chars/token if the server is down."""
    try:
        r = requests.post(BASE_URL.removesuffix("/v1") + "/tokenize", json={"content": text}, timeout=10)
        r.raise_for_status()
        return len(r.json()["tokens"])
    except (requests.RequestException, KeyError, ValueError):
        return len(text) // 4


def json_call(system: str, user: str, schema: dict, max_tokens: int = 700) -> tuple[dict, dict]:
    """One structured call. Returns (parsed_json, usage). Raises ValueError on unparseable output."""
    r = _client.chat.completions.create(
        model=MODEL, temperature=0.1, max_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_schema", "json_schema": {"name": "out", "schema": schema}},
        extra_body={"top_k": 50},
    )
    text = re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S).strip()
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise ValueError(f"no JSON in model output: {text[:200]!r}")
    usage = {"input_tokens": r.usage.prompt_tokens if r.usage else 0,
             "output_tokens": r.usage.completion_tokens if r.usage else 0}
    return json.loads(m.group(0)), usage
