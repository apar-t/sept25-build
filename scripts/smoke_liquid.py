"""One tool-calling round trip against LFM2.5 on the local llama-server.

    uv run scripts/smoke_liquid.py
"""

import json
import os
import re
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(base_url=os.environ["LFM_BASE_URL"], api_key=os.environ.get("LFM_API_KEY", "local"))
MODEL = os.environ.get("LFM_MODEL", "LFM2.5-2.6B")

tools = [{"type": "function", "function": {
    "name": "get_weather", "description": "Get the current weather for a city.",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
IMPL = {"get_weather": lambda city: {"city": city, "temp_c": 17, "sky": "fog"}}

messages = [{"role": "system", "content": "You are a helpful assistant. Call tools when they help."},
            {"role": "user", "content": "What's the weather in San Francisco?"}]

t = time.monotonic()
for _ in range(6):
    r = client.chat.completions.create(model=MODEL, messages=messages, tools=tools,
                                       temperature=0.1, max_tokens=1024, extra_body={"top_k": 50})
    msg = r.choices[0].message
    messages.append(msg.model_dump(exclude_none=True))
    if not msg.tool_calls:
        print("answer:", re.sub(r"<think>.*?</think>", "", msg.content or "", flags=re.S).strip())
        break
    for c in msg.tool_calls:
        print(f"tool call: {c.function.name}({c.function.arguments})")
        out = IMPL[c.function.name](**json.loads(c.function.arguments or "{}"))
        messages.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(out)})
print(f"{time.monotonic() - t:.1f}s, last usage: {r.usage}")
