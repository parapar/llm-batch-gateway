"""Token estimation for upfront budget reservation.

This is a heuristic (~4 chars/token, plus OpenAI's well-known per-message
chat overhead), not an exact tokenizer -- getting it exactly right needs
the model's real tokenizer, which lives on the llama-server nodes
(`/tokenize`) and isn't wired up until M3's dispatcher exists. Until
then, this module is the single place that logic will replace: swap
`estimate_prompt_tokens` for a node-backed call and nothing else in the
reservation path needs to change.

Being conservative (slightly over-counting) is the safe direction here:
it only affects how much budget gets reserved up front, never what gets
actually charged -- that always comes from real usage once a task
completes (see batch_ops.complete_task), so over-reservation just means
a released surplus, not a mischarge.
"""

from __future__ import annotations

import math

# Per-message overhead in OpenAI's chat token-counting guide (role +
# formatting tokens around each message). Close enough for llama.cpp
# chat templates too, and errs conservative either way.
_PER_MESSAGE_OVERHEAD = 4
_PRIMING_TOKENS = 2
_CHARS_PER_TOKEN = 4.0


def count_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))


def _message_text(content: object) -> str:
    """Chat message content is either a plain string, or a list of
    content parts (e.g. multimodal). We only estimate the text parts --
    non-text parts (images, audio) still get forwarded to the model in
    M3, but we don't have a token-cost model for them here."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return " ".join(parts)
    return ""


def estimate_prompt_tokens(messages: list[dict]) -> int:
    total = _PRIMING_TOKENS
    for message in messages:
        total += _PER_MESSAGE_OVERHEAD
        total += count_text_tokens(_message_text(message.get("content")))
        name = message.get("name")
        if isinstance(name, str):
            total += count_text_tokens(name)
    return total


def estimate_request_tokens(body: dict, *, default_max_tokens: int) -> tuple[int, int]:
    """Returns (prompt_tokens_estimate, max_tokens) for one chat.completions
    batch line. max_tokens comes from the request if present, else the
    configured default -- see docs/PLAN.md: worst-case output is always
    bounded, never left open-ended."""
    messages = body.get("messages") or []
    prompt_tokens = estimate_prompt_tokens(messages)
    max_tokens = body.get("max_tokens")
    if max_tokens is None:
        max_tokens = default_max_tokens
    return prompt_tokens, int(max_tokens)
