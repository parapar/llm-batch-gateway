"""ETA estimation tests: bootstrap behavior before any samples exist,
convergence once the dispatcher has completed real tasks, confidence
flagging, queue-position/ahead-of-queue accounting, and the
"unavailable" cases (no nodes, batch already terminal).
"""

from __future__ import annotations

import dataclasses
import json

import httpx
from fastapi.testclient import TestClient
from tests.fake_llama_node import make_fake_llama_app

from batchsvc.config import NodeConfig, Settings
from batchsvc.dispatcher import Dispatcher
from batchsvc.llama_client import LlamaClient

ENDPOINT = "/v1/chat/completions"


def _auth(raw_key: str) -> dict:
    return {"Authorization": f"Bearer {raw_key}"}


def _make_user_and_key(client: TestClient, admin_headers: dict, username: str) -> tuple[dict, str]:
    user = client.post("/admin/users", json={"username": username}, headers=admin_headers).json()
    key = client.post(f"/admin/users/{user['id']}/api-keys", json={}, headers=admin_headers).json()["key"]
    return user, key


def _grant(client: TestClient, admin_headers: dict, user_id: str, tokens: int) -> None:
    resp = client.post(f"/admin/users/{user_id}/budget/grant", json={"tokens": tokens}, headers=admin_headers)
    assert resp.status_code == 200


def _batch_line(custom_id: str, *, content: str = "Hello!", max_tokens: int = 100) -> dict:
    body = {"messages": [{"role": "user", "content": content}], "max_tokens": max_tokens}
    return {"custom_id": custom_id, "method": "POST", "url": ENDPOINT, "body": body}


def _jsonl(lines: list[dict]) -> bytes:
    return ("\n".join(json.dumps(line) for line in lines) + "\n").encode("utf-8")


def _submit_batch(client: TestClient, key: str, lines: list[dict]) -> dict:
    file_resp = client.post(
        "/v1/files",
        files={"file": ("input.jsonl", _jsonl(lines), "application/jsonl")},
        data={"purpose": "batch"},
        headers=_auth(key),
    )
    file_id = file_resp.json()["id"]
    resp = client.post(
        "/v1/batches",
        json={"input_file_id": file_id, "endpoint": ENDPOINT, "completion_window": "24h"},
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_eta_unavailable_when_no_nodes_configured(client: TestClient, admin_headers: dict):
    user, key = _make_user_and_key(client, admin_headers, "eta-nonodes")
    _grant(client, admin_headers, user["id"], 100_000)
    batch = _submit_batch(client, key, [_batch_line("r1")])
    assert batch["x_eta"] == {
        "estimated_seconds_remaining": None,
        "estimated_completion_at": None,
        "queue_position": None,
        "confidence": "unavailable",
    }


async def test_eta_unavailable_once_batch_completed(
    client: TestClient, admin_headers: dict, app, settings: Settings
):
    user, key = _make_user_and_key(client, admin_headers, "eta-done")
    _grant(client, admin_headers, user["id"], 100_000)
    batch = _submit_batch(client, key, [_batch_line("r1")])

    node_settings = dataclasses.replace(
        settings, nodes=[NodeConfig(name="a", base_url="http://a.test", parallel_slots=4)]
    )
    app.state.settings = node_settings  # the router reads app.state.settings, not this fixture

    fake_app = make_fake_llama_app()
    dispatcher = Dispatcher(
        app.state.db,
        node_settings,
        client_factory=lambda base_url: LlamaClient(base_url, transport=httpx.ASGITransport(app=fake_app)),
    )
    dispatcher.startup()
    await dispatcher.health_check_all()
    await dispatcher.dispatch_once()

    final = client.get(f"/v1/batches/{batch['id']}", headers=_auth(key)).json()
    assert final["status"] == "completed"
    assert final["x_eta"]["confidence"] == "unavailable"
    assert final["x_eta"]["estimated_seconds_remaining"] is None


async def test_eta_uses_bootstrap_before_any_samples(
    client: TestClient, admin_headers: dict, app, settings: Settings
):
    user, key = _make_user_and_key(client, admin_headers, "eta-bootstrap")
    _grant(client, admin_headers, user["id"], 1_000_000)
    # 2 slots, plenty of pending work left unclaimed so the batch stays
    # in_progress and we can read its ETA before anything completes.
    lines = [_batch_line(f"r{i}", max_tokens=100) for i in range(5)]
    batch = _submit_batch(client, key, lines)

    node_settings = dataclasses.replace(
        settings, nodes=[NodeConfig(name="a", base_url="http://a.test", parallel_slots=2)]
    )
    app.state.settings = node_settings

    fake_app = make_fake_llama_app()
    dispatcher = Dispatcher(
        app.state.db,
        node_settings,
        client_factory=lambda base_url: LlamaClient(base_url, transport=httpx.ASGITransport(app=fake_app)),
    )
    dispatcher.startup()  # syncs the node row so eta.py sees 2 healthy slots

    status = client.get(f"/v1/batches/{batch['id']}", headers=_auth(key)).json()
    assert status["x_eta"]["confidence"] == "low"
    assert status["x_eta"]["estimated_seconds_remaining"] is not None
    assert status["x_eta"]["estimated_seconds_remaining"] > 0
    assert status["x_eta"]["estimated_completion_at"] is not None
    assert status["x_eta"]["queue_position"] == 0  # nothing else in the queue ahead of it

    # Sanity-check the bootstrap arithmetic directly: each task's expected
    # output is capped at its own max_tokens=100 (well under the 200-token
    # bootstrap), so 5 tasks * ~100 tokens / (bootstrap tps * 2 slots),
    # give or take the small prompt contribution.
    expected_floor = (5 * 100) / (node_settings.eta_bootstrap_tokens_per_second * 2)
    assert status["x_eta"]["estimated_seconds_remaining"] >= expected_floor * 0.9


async def test_eta_confidence_becomes_normal_after_enough_samples(
    client: TestClient, admin_headers: dict, app, settings: Settings
):
    user, key = _make_user_and_key(client, admin_headers, "eta-confidence")
    _grant(client, admin_headers, user["id"], 1_000_000)

    node_settings = dataclasses.replace(
        settings,
        nodes=[NodeConfig(name="a", base_url="http://a.test", parallel_slots=4)],
        eta_min_samples_for_confidence=3,
    )
    app.state.settings = node_settings

    fake_app = make_fake_llama_app(prompt_tokens=10, completion_tokens=20)
    dispatcher = Dispatcher(
        app.state.db,
        node_settings,
        client_factory=lambda base_url: LlamaClient(base_url, transport=httpx.ASGITransport(app=fake_app)),
    )
    dispatcher.startup()
    await dispatcher.health_check_all()

    # Complete 3 small batches' worth of single tasks to build up samples.
    for i in range(3):
        _submit_batch(client, key, [_batch_line(f"warm{i}")])
        await dispatcher.dispatch_once()

    # One more batch, left pending, to read a confident ETA off of.
    batch = _submit_batch(client, key, [_batch_line("final", max_tokens=100)])
    status = client.get(f"/v1/batches/{batch['id']}", headers=_auth(key)).json()
    assert status["x_eta"]["confidence"] == "normal"
    assert status["x_eta"]["estimated_seconds_remaining"] > 0


async def test_eta_queue_position_counts_batches_ahead(
    client: TestClient, admin_headers: dict, app, settings: Settings
):
    user, key = _make_user_and_key(client, admin_headers, "eta-queue")
    _grant(client, admin_headers, user["id"], 1_000_000)

    node_settings = dataclasses.replace(
        settings, nodes=[NodeConfig(name="a", base_url="http://a.test", parallel_slots=1)]
    )
    app.state.settings = node_settings

    dispatcher = Dispatcher(app.state.db, node_settings)
    dispatcher.startup()  # just to sync the node row; no dispatch happening here

    first = _submit_batch(client, key, [_batch_line("first")])
    second = _submit_batch(client, key, [_batch_line("second")])
    third = _submit_batch(client, key, [_batch_line("third")])

    def eta_of(batch_id: str) -> dict:
        return client.get(f"/v1/batches/{batch_id}", headers=_auth(key)).json()["x_eta"]

    assert eta_of(first["id"])["queue_position"] == 0
    assert eta_of(second["id"])["queue_position"] == 1
    assert eta_of(third["id"])["queue_position"] == 2
    # More work ahead should mean a longer estimate.
    first_eta = eta_of(first["id"])["estimated_seconds_remaining"]
    third_eta = eta_of(third["id"])["estimated_seconds_remaining"]
    assert third_eta > first_eta
