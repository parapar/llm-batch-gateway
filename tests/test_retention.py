"""Tests for the M5 retention job: batch expiry and result-file purging.

Drives retention.expire_overdue_batches/purge_expired_results directly
against real Batch/Task rows created through the HTTP API and settled
via batch_ops (same pattern as test_batch_lifecycle.py), rather than
waiting on RetentionJob's real polling interval.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from batchsvc import batch_ops, retention
from batchsvc.db import Database
from batchsvc.models import Batch, FileObject, Task

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


def _batch_line(custom_id: str, *, max_tokens: int = 100) -> dict:
    body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": max_tokens}
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


def test_expire_overdue_batch_releases_reservation(
    client: TestClient, admin_headers: dict, app, settings
):
    user, key = _make_user_and_key(client, admin_headers, "ret-expire")
    _grant(client, admin_headers, user["id"], 100_000)
    batch = _submit_batch(client, key, [_batch_line("r1")])

    db: Database = app.state.db
    with db.session_scope() as session:
        row = session.get(Batch, batch["id"])
        row.expires_at = retention._now() - timedelta(hours=1)
        session.commit()

        expired_count = retention.expire_overdue_batches(session)
    assert expired_count == 1

    final = client.get(f"/v1/batches/{batch['id']}", headers=_auth(key)).json()
    assert final["status"] == "expired"
    assert final["expired_at"] is not None

    budget = client.get("/v1/budget", headers=_auth(key)).json()
    assert budget["reserved_tokens"] == 0
    assert budget["available_tokens"] == 100_000


def test_expire_leaves_batches_not_yet_due_alone(client: TestClient, admin_headers: dict, app):
    user, key = _make_user_and_key(client, admin_headers, "ret-notyet")
    _grant(client, admin_headers, user["id"], 100_000)
    batch = _submit_batch(client, key, [_batch_line("r1")])

    db: Database = app.state.db
    with db.session_scope() as session:
        expired_count = retention.expire_overdue_batches(session)
    assert expired_count == 0

    final = client.get(f"/v1/batches/{batch['id']}", headers=_auth(key)).json()
    assert final["status"] == "in_progress"


def test_purge_deletes_result_files_past_retention(
    client: TestClient, admin_headers: dict, app, settings
):
    user, key = _make_user_and_key(client, admin_headers, "ret-purge")
    _grant(client, admin_headers, user["id"], 100_000)
    batch = _submit_batch(client, key, [_batch_line("r1")])

    db: Database = app.state.db
    with db.session_scope() as session:
        task = session.query(Task).filter(Task.batch_id == batch["id"]).one()
        batch_ops.complete_task(
            session,
            task=task,
            response_body={"choices": [{"message": {"content": "hi"}}]},
            prompt_tokens=5,
            completion_tokens=5,
            blob_dir=settings.blob_dir,
        )

    final = client.get(f"/v1/batches/{batch['id']}", headers=_auth(key)).json()
    assert final["status"] == "completed"
    output_file_id = final["output_file_id"]
    assert output_file_id is not None

    with db.session_scope() as session:
        blob_path = session.get(FileObject, output_file_id).path
    assert Path(blob_path).exists()

    with db.session_scope() as session:
        row = session.get(Batch, batch["id"])
        row.completed_at = retention._now() - timedelta(days=settings.result_retention_days + 1)
        session.commit()

        purged_count = retention.purge_expired_results(session, settings)
    assert purged_count == 1

    assert not Path(blob_path).exists()

    after = client.get(f"/v1/batches/{batch['id']}", headers=_auth(key)).json()
    assert after["output_file_id"] is None
    assert after["error_file_id"] is None

    missing_resp = client.get(f"/v1/files/{output_file_id}", headers=_auth(key))
    assert missing_resp.status_code == 404


def test_purge_leaves_recent_completed_batches_alone(
    client: TestClient, admin_headers: dict, app, settings
):
    user, key = _make_user_and_key(client, admin_headers, "ret-recent")
    _grant(client, admin_headers, user["id"], 100_000)
    batch = _submit_batch(client, key, [_batch_line("r1")])

    db: Database = app.state.db
    with db.session_scope() as session:
        task = session.query(Task).filter(Task.batch_id == batch["id"]).one()
        batch_ops.complete_task(
            session,
            task=task,
            response_body={"choices": []},
            prompt_tokens=1,
            completion_tokens=1,
            blob_dir=settings.blob_dir,
        )

    with db.session_scope() as session:
        purged_count = retention.purge_expired_results(session, settings)
    assert purged_count == 0

    after = client.get(f"/v1/batches/{batch['id']}", headers=_auth(key)).json()
    assert after["output_file_id"] is not None


def test_purge_is_idempotent(client: TestClient, admin_headers: dict, app, settings):
    user, key = _make_user_and_key(client, admin_headers, "ret-idempotent")
    _grant(client, admin_headers, user["id"], 100_000)
    batch = _submit_batch(client, key, [_batch_line("r1")])

    db: Database = app.state.db
    with db.session_scope() as session:
        task = session.query(Task).filter(Task.batch_id == batch["id"]).one()
        batch_ops.complete_task(
            session,
            task=task,
            response_body={"choices": []},
            prompt_tokens=1,
            completion_tokens=1,
            blob_dir=settings.blob_dir,
        )
        row = session.get(Batch, batch["id"])
        row.completed_at = retention._now() - timedelta(days=settings.result_retention_days + 1)
        session.commit()

        first_pass = retention.purge_expired_results(session, settings)
    assert first_pass == 1

    with db.session_scope() as session:
        second_pass = retention.purge_expired_results(session, settings)
    assert second_pass == 0  # already purged (purged_at set), not reconsidered


def test_retention_job_run_once_reports_counts(client: TestClient, admin_headers: dict, app, settings):
    user, key = _make_user_and_key(client, admin_headers, "ret-runonce")
    _grant(client, admin_headers, user["id"], 100_000)
    batch = _submit_batch(client, key, [_batch_line("r1")])

    db: Database = app.state.db
    with db.session_scope() as session:
        row = session.get(Batch, batch["id"])
        row.expires_at = retention._now() - timedelta(hours=1)
        session.commit()

    job = retention.RetentionJob(db, settings)
    expired, purged = job.run_once()
    assert expired == 1
    assert purged == 0

    final = client.get(f"/v1/batches/{batch['id']}", headers=_auth(key)).json()
    assert final["status"] == "expired"
