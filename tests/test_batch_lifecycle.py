"""End-to-end batch lifecycle test.

There's no dispatcher yet (that's M3), so nothing in the running service
calls batch_ops.complete_task/fail_task outside of this test. This test
plays that role directly -- fetching the Task rows a submitted batch
created and settling them the way M3's dispatcher eventually will -- to
prove the completion -> finalization -> downloadable-results pipeline
that batch_ops.py already implements actually works end to end.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from batchsvc import batch_ops
from batchsvc.db import Database
from batchsvc.models import Task

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
    body = {
        "model": "ignored",
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
    }
    return {"custom_id": custom_id, "method": "POST", "url": ENDPOINT, "body": body}


def _jsonl(lines: list[dict]) -> bytes:
    return ("\n".join(json.dumps(line) for line in lines) + "\n").encode("utf-8")


def _upload(client: TestClient, key: str, lines: list[dict]) -> str:
    resp = client.post(
        "/v1/files",
        files={"file": ("input.jsonl", _jsonl(lines), "application/jsonl")},
        data={"purpose": "batch"},
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_full_batch_lifecycle_submit_complete_finalize_download(client: TestClient, admin_headers: dict, app):
    db: Database = app.state.db
    blob_dir = app.state.settings.blob_dir

    user, key = _make_user_and_key(client, admin_headers, "kira")
    _grant(client, admin_headers, user["id"], 100_000)

    lines = [_batch_line("r1"), _batch_line("r2"), _batch_line("r3")]
    file_id = _upload(client, key, lines)
    batch = client.post(
        "/v1/batches",
        json={"input_file_id": file_id, "endpoint": ENDPOINT, "completion_window": "24h"},
        headers=_auth(key),
    ).json()
    assert batch["status"] == "in_progress"

    with db.session_scope() as session:
        tasks = (
            session.query(Task)
            .filter(Task.batch_id == batch["id"])
            .order_by(Task.line_index)
            .all()
        )
        assert len(tasks) == 3
        by_custom_id = {t.custom_id: t for t in tasks}

        # r1, r2 "succeed"; r3 "fails" -- standing in for what M3's
        # dispatcher will report per task once it exists.
        batch_ops.complete_task(
            session,
            task=by_custom_id["r1"],
            response_body={"choices": [{"message": {"role": "assistant", "content": "Hi!"}}]},
            prompt_tokens=20,
            completion_tokens=30,
            blob_dir=blob_dir,
        )
        batch_ops.complete_task(
            session,
            task=by_custom_id["r2"],
            response_body={"choices": [{"message": {"role": "assistant", "content": "Hello!"}}]},
            prompt_tokens=15,
            completion_tokens=10,
            blob_dir=blob_dir,
        )
        batch_ops.fail_task(
            session, task=by_custom_id["r3"], error="node timeout after 3 retries", blob_dir=blob_dir
        )

    final = client.get(f"/v1/batches/{batch['id']}", headers=_auth(key)).json()
    assert final["status"] == "completed"
    assert final["request_counts"] == {"total": 3, "completed": 2, "failed": 1}
    assert final["output_file_id"] is not None
    assert final["error_file_id"] is not None
    assert final["completed_at"] is not None
    assert final["finalizing_at"] is not None
    assert final["x_tokens"]["consumed"] == 20 + 30 + 15 + 10

    output_content = client.get(f"/v1/files/{final['output_file_id']}/content", headers=_auth(key)).content
    output_records = [json.loads(line) for line in output_content.decode().splitlines()]
    assert {r["custom_id"] for r in output_records} == {"r1", "r2"}
    r1_record = next(r for r in output_records if r["custom_id"] == "r1")
    assert r1_record["response"]["status_code"] == 200
    assert r1_record["response"]["body"]["choices"][0]["message"]["content"] == "Hi!"
    assert r1_record["error"] is None

    error_content = client.get(f"/v1/files/{final['error_file_id']}/content", headers=_auth(key)).content
    error_records = [json.loads(line) for line in error_content.decode().splitlines()]
    assert len(error_records) == 1
    assert error_records[0]["custom_id"] == "r3"
    assert error_records[0]["response"] is None
    assert "node timeout" in error_records[0]["error"]["message"]

    # Budget: r1+r2 charged their real usage, r3's full reservation was
    # released untouched, and nothing is left dangling in `reserved`.
    budget = client.get("/v1/budget", headers=_auth(key)).json()
    assert budget["reserved_tokens"] == 0
    assert budget["used_tokens"] == 20 + 30 + 15 + 10
    assert budget["available_tokens"] == 100_000 - budget["used_tokens"]

    # A completed batch is no longer cancellable.
    cancel_resp = client.post(f"/v1/batches/{batch['id']}/cancel", headers=_auth(key))
    assert cancel_resp.status_code == 409
