"""Standalone stub llama-server, for load testing (scripts/load_test.py).

Deliberately separate from tests/fake_llama_node.py (that one's wired
for httpx.ASGITransport in-process pytest use with instant responses).
This one runs as a real subprocess over a real socket and adds
configurable artificial latency, so a load test against it says
something about behavior under realistic-ish response times, not just
correctness.

Run directly: STUB_LATENCY_SECONDS=0.5 uvicorn scripts.stub_llama_node:app --port 9101
"""

from __future__ import annotations

import asyncio
import os
import random

from fastapi import FastAPI, Request

app = FastAPI()

LATENCY_SECONDS = float(os.environ.get("STUB_LATENCY_SECONDS", "0.3"))
LATENCY_JITTER_SECONDS = float(os.environ.get("STUB_LATENCY_JITTER_SECONDS", "0.15"))
FAILURE_RATE = float(os.environ.get("STUB_FAILURE_RATE", "0.0"))  # 0.0-1.0, simulated transient errors


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    delay = max(0.0, LATENCY_SECONDS + random.uniform(-LATENCY_JITTER_SECONDS, LATENCY_JITTER_SECONDS))
    await asyncio.sleep(delay)

    if FAILURE_RATE > 0 and random.random() < FAILURE_RATE:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="stub: simulated transient failure")

    body = await request.json()
    last_message = body["messages"][-1]["content"]
    prompt_tokens = max(1, len(last_message) // 4)
    completion_tokens = random.randint(20, 80)
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": f"stub response to: {last_message[:60]}"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
