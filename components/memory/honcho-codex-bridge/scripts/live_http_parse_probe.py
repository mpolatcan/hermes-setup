from __future__ import annotations

import json
from pathlib import Path

from openai import OpenAI
from pydantic import BaseModel


class AdapterProbe(BaseModel):
    answer: str


api_key = Path("/tmp/honcho-codex-adapter-test.key").read_text().strip()
client = OpenAI(api_key=api_key, base_url="http://127.0.0.1:18080/v1", timeout=180.0)
try:
    completion = client.beta.chat.completions.parse(
        model="honcho-deriver",
        messages=[
            {"role": "system", "content": "Return the requested structured data."},
            {"role": "user", "content": "Set answer to HTTP-ready."},
        ],
        response_format=AdapterProbe,
        reasoning_effort="low",
    )
    message = completion.choices[0].message
    assert message.parsed == AdapterProbe(answer="HTTP-ready"), message.parsed
    print(json.dumps({
        "ok": True,
        "sdk": "openai.beta.chat.completions.parse",
        "parsed": message.parsed.model_dump(),
        "finish_reason": completion.choices[0].finish_reason,
        "usage": completion.usage.model_dump() if completion.usage else None,
    }, indent=2))
finally:
    client.close()
