"""Thin async HTTP client for one llama-server node.

llama-server exposes an OpenAI-compatible /v1/chat/completions endpoint
and a /health endpoint; we don't need anything else. Every Task's
request_body is already in that shape (it's exactly what the student put
in their batch input line), so it's forwarded close to verbatim -- the
`transport` hook exists purely so tests can point this at an in-process
fake node (httpx.ASGITransport) instead of a real socket.
"""

from __future__ import annotations

import httpx


class NodeRequestError(Exception):
    """Raised for anything that should count as a failed attempt: a
    non-2xx response, a timeout, or a connection error."""


class LlamaClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/health")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def chat_completion(self, body: dict) -> dict:
        try:
            resp = await self._client.post("/v1/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise NodeRequestError(str(e)) from e
        if resp.status_code >= 400:
            raise NodeRequestError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            return resp.json()
        except ValueError as e:
            raise NodeRequestError(f"non-JSON response: {e}") from e

    async def aclose(self) -> None:
        await self._client.aclose()
