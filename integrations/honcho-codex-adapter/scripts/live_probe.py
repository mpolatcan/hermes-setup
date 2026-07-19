from __future__ import annotations

import json

from honcho_codex_adapter.driver import HermesCodexDriver
from honcho_codex_adapter.models import ChatCompletionRequest

request = ChatCompletionRequest.model_validate({
    "model": "honcho-deriver-luna",
    "messages": [
        {"role": "system", "content": "Return only data matching the supplied schema."},
        {"role": "user", "content": "Set answer to the single word ready."},
    ],
    "response_format": {
        "type": "json_schema",
        "json_schema": {
            "name": "adapter_probe",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        },
    },
    "reasoning_effort": "low",
})
result = HermesCodexDriver().complete(request)
parsed = json.loads(result.content or "")
assert parsed == {"answer": "ready"}, parsed
print(json.dumps({
    "ok": True,
    "model": result.model,
    "finish_reason": result.finish_reason,
    "parsed": parsed,
    "usage": result.usage,
}, indent=2))
