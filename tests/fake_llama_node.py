"""A minimal in-process stand-in for llama-server's OpenAI-compatible
surface (/health, /v1/chat/completions), used with httpx.ASGITransport so
dispatcher tests never touch a real socket or a real model.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request


def make_fake_llama_app(
    *,
    fail_first_n_calls: int = 0,
    always_fail: bool = False,
    healthy: bool = True,
    prompt_tokens: int = 12,
    completion_tokens: int = 8,
) -> FastAPI:
    """`fail_first_n_calls` simulates transient failures that succeed on
    retry; `always_fail` simulates a node that never completes a request
    (to exercise attempt exhaustion); `healthy=False` makes /health
    always fail (to exercise the ejection threshold)."""
    app = FastAPI()
    state = {"calls": 0}

    @app.get("/health")
    async def health():
        if not healthy:
            raise HTTPException(status_code=503, detail="not ready")
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        state["calls"] += 1
        if always_fail or state["calls"] <= fail_first_n_calls:
            raise HTTPException(status_code=500, detail="simulated node failure")
        body = await request.json()
        last_message = body["messages"][-1]["content"]
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": f"echo: {last_message}"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    app.state.call_count = lambda: state["calls"]
    return app
