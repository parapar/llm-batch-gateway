"""Tests for GET /metrics: auth gate and that the expected series show up
with sane values after some activity. Not asserting exact Prometheus
text formatting (that's prometheus_client's job, not ours) -- just that
our own labels/values are present and correct.
"""

from __future__ import annotations

import dataclasses
import json

from fastapi.testclient import TestClient

from batchsvc.config import NodeConfig

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


def _batch_line(custom_id: str) -> dict:
    body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 50}
    return {"custom_id": custom_id, "method": "POST", "url": ENDPOINT, "body": body}


def _submit_batch(client: TestClient, key: str) -> dict:
    content = json.dumps(_batch_line("r1")) + "\n"
    file_resp = client.post(
        "/v1/files",
        files={"file": ("input.jsonl", content.encode(), "application/jsonl")},
        data={"purpose": "batch"},
        headers=_auth(key),
    )
    file_id = file_resp.json()["id"]
    resp = client.post(
        "/v1/batches",
        json={"input_file_id": file_id, "endpoint": ENDPOINT, "completion_window": "24h"},
        headers=_auth(key),
    )
    assert resp.status_code == 201
    return resp.json()


def test_metrics_requires_admin_token(client: TestClient):
    resp = client.get("/metrics")
    assert resp.status_code == 401

    resp = client.get("/metrics", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 403


def test_metrics_reports_batch_and_task_counts(client: TestClient, admin_headers: dict):
    user, key = _make_user_and_key(client, admin_headers, "metrics-1")
    _grant(client, admin_headers, user["id"], 100_000)
    _submit_batch(client, key)

    resp = client.get("/metrics", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text

    assert 'batchsvc_batches{status="in_progress"} 1.0' in body
    assert 'batchsvc_tasks{status="pending"} 1.0' in body
    # Every known status label is emitted, not just the ones with activity.
    assert 'batchsvc_batches{status="cancelled"} 0.0' in body


def test_metrics_reports_node_gauges_when_nodes_configured(
    client: TestClient, admin_headers: dict, app, settings
):
    node_settings = dataclasses.replace(
        settings, nodes=[NodeConfig(name="node-x", base_url="http://node-x.test", parallel_slots=3)]
    )
    app.state.settings = node_settings

    from batchsvc.dispatcher import Dispatcher

    dispatcher = Dispatcher(app.state.db, node_settings)
    dispatcher.startup()  # syncs the "node-x" row into the DB
    app.state.dispatcher = dispatcher

    resp = client.get("/metrics", headers=admin_headers)
    body = resp.text
    assert 'batchsvc_node_parallel_slots{node="node-x"} 3.0' in body
    assert 'batchsvc_node_healthy{node="node-x"} 1.0' in body
    assert 'batchsvc_node_in_flight_tasks{node="node-x"} 0.0' in body


def test_metrics_http_request_counter_increments(client: TestClient, admin_headers: dict):
    before = client.get("/metrics", headers=admin_headers).text
    client.get("/healthz")
    client.get("/healthz")
    after = client.get("/metrics", headers=admin_headers).text

    def _count_for_healthz(body: str) -> float:
        for line in body.splitlines():
            if not line.startswith("batchsvc_http_requests_total{"):
                continue
            if '"/healthz"' in line and 'status="200"' in line:
                return float(line.rsplit(" ", 1)[1])
        return 0.0

    assert _count_for_healthz(after) >= _count_for_healthz(before) + 2
