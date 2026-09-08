"""HTTP-level tests for /v1/batches: submission, validation, budget
reservation, status, listing, and cancellation. Completion/finalization
(the results you'd download) is covered separately in
test_batch_lifecycle.py since nothing drives task completion until M3's
dispatcher exists.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from batchsvc.tokens import estimate_request_tokens

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


def _jsonl(lines: list[dict]) -> bytes:
    return ("\n".join(json.dumps(line) for line in lines) + "\n").encode("utf-8")


def _batch_line(custom_id: str, *, content: str = "Hello!", max_tokens: int | None = 100) -> dict:
    body = {"model": "ignored", "messages": [{"role": "user", "content": content}]}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    return {"custom_id": custom_id, "method": "POST", "url": ENDPOINT, "body": body}


def _upload(client: TestClient, key: str, lines: list[dict]) -> str:
    resp = client.post(
        "/v1/files",
        files={"file": ("input.jsonl", _jsonl(lines), "application/jsonl")},
        data={"purpose": "batch"},
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_submit_batch_reserves_tokens_and_reports_status(client: TestClient, admin_headers: dict):
    user, key = _make_user_and_key(client, admin_headers, "wren")
    _grant(client, admin_headers, user["id"], 100_000)

    lines = [_batch_line("r1", content="Hi"), _batch_line("r2", content="Hello there")]
    file_id = _upload(client, key, lines)

    resp = client.post(
        "/v1/batches",
        json={"input_file_id": file_id, "endpoint": ENDPOINT, "completion_window": "24h"},
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text
    batch = resp.json()
    assert batch["object"] == "batch"
    assert batch["status"] == "in_progress"
    assert batch["input_file_id"] == file_id
    assert batch["request_counts"] == {"total": 2, "completed": 0, "failed": 0}
    assert batch["output_file_id"] is None
    assert batch["in_progress_at"] is not None
    assert batch["expires_at"] is not None

    expected_reserve = sum(
        sum(estimate_request_tokens(line["body"], default_max_tokens=512)) for line in lines
    )
    assert batch["x_tokens"]["reserved"] == expected_reserve
    assert batch["x_tokens"]["consumed"] == 0

    budget = client.get("/v1/budget", headers=_auth(key)).json()
    assert budget["reserved_tokens"] == expected_reserve
    assert budget["available_tokens"] == 100_000 - expected_reserve


def test_submit_batch_insufficient_budget_reserves_nothing(client: TestClient, admin_headers: dict):
    user, key = _make_user_and_key(client, admin_headers, "xavier")
    _grant(client, admin_headers, user["id"], 10)  # far too little

    file_id = _upload(client, key, [_batch_line("r1", max_tokens=1000)])
    resp = client.post(
        "/v1/batches",
        json={"input_file_id": file_id, "endpoint": ENDPOINT, "completion_window": "24h"},
        headers=_auth(key),
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "insufficient_quota"

    # Nothing should have been reserved or created.
    budget = client.get("/v1/budget", headers=_auth(key)).json()
    assert budget["reserved_tokens"] == 0
    assert budget["available_tokens"] == 10

    listing = client.get("/v1/batches", headers=_auth(key)).json()
    assert listing["data"] == []


def test_submit_rejects_unsupported_endpoint(client: TestClient, admin_headers: dict):
    user, key = _make_user_and_key(client, admin_headers, "yara")
    _grant(client, admin_headers, user["id"], 100_000)
    file_id = _upload(client, key, [_batch_line("r1")])

    resp = client.post(
        "/v1/batches",
        json={"input_file_id": file_id, "endpoint": "/v1/embeddings", "completion_window": "24h"},
        headers=_auth(key),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "endpoint"


def test_submit_rejects_unsupported_completion_window(client: TestClient, admin_headers: dict):
    user, key = _make_user_and_key(client, admin_headers, "zack")
    _grant(client, admin_headers, user["id"], 100_000)
    file_id = _upload(client, key, [_batch_line("r1")])

    resp = client.post(
        "/v1/batches",
        json={"input_file_id": file_id, "endpoint": ENDPOINT, "completion_window": "1h"},
        headers=_auth(key),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "completion_window"


def test_submit_rejects_malformed_jsonl_line(client: TestClient, admin_headers: dict):
    user, key = _make_user_and_key(client, admin_headers, "amy")
    _grant(client, admin_headers, user["id"], 100_000)
    file_id = _upload(client, key, [])  # placeholder, overwritten below

    # Upload raw malformed content directly (helper always builds valid lines).
    resp = client.post(
        "/v1/files",
        files={"file": ("bad.jsonl", b"not json at all\n", "application/jsonl")},
        data={"purpose": "batch"},
        headers=_auth(key),
    )
    bad_file_id = resp.json()["id"]

    resp = client.post(
        "/v1/batches",
        json={"input_file_id": bad_file_id, "endpoint": ENDPOINT, "completion_window": "24h"},
        headers=_auth(key),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "input_file_id"
    assert "invalid JSON" in resp.json()["error"]["message"]

    # And the well-formed upload from earlier is untouched / unrelated.
    assert file_id


def test_submit_rejects_duplicate_custom_id(client: TestClient, admin_headers: dict):
    user, key = _make_user_and_key(client, admin_headers, "bea")
    _grant(client, admin_headers, user["id"], 100_000)
    file_id = _upload(client, key, [_batch_line("dup"), _batch_line("dup")])

    resp = client.post(
        "/v1/batches",
        json={"input_file_id": file_id, "endpoint": ENDPOINT, "completion_window": "24h"},
        headers=_auth(key),
    )
    assert resp.status_code == 400
    assert "duplicate custom_id" in resp.json()["error"]["message"]


def test_submit_rejects_missing_messages(client: TestClient, admin_headers: dict):
    user, key = _make_user_and_key(client, admin_headers, "cole")
    _grant(client, admin_headers, user["id"], 100_000)
    bad_line = {"custom_id": "r1", "method": "POST", "url": ENDPOINT, "body": {}}
    file_id = _upload(client, key, [bad_line])

    resp = client.post(
        "/v1/batches",
        json={"input_file_id": file_id, "endpoint": ENDPOINT, "completion_window": "24h"},
        headers=_auth(key),
    )
    assert resp.status_code == 400
    assert "messages" in resp.json()["error"]["message"]


def test_batch_not_visible_to_other_user(client: TestClient, admin_headers: dict):
    user_a, key_a = _make_user_and_key(client, admin_headers, "dana")
    _, key_b = _make_user_and_key(client, admin_headers, "eli")
    _grant(client, admin_headers, user_a["id"], 100_000)

    file_id = _upload(client, key_a, [_batch_line("r1")])
    batch = client.post(
        "/v1/batches",
        json={"input_file_id": file_id, "endpoint": ENDPOINT, "completion_window": "24h"},
        headers=_auth(key_a),
    ).json()

    resp = client.get(f"/v1/batches/{batch['id']}", headers=_auth(key_b))
    assert resp.status_code == 404


def test_cannot_submit_batch_against_someone_elses_file(client: TestClient, admin_headers: dict):
    _, key_a = _make_user_and_key(client, admin_headers, "finn")
    user_b, key_b = _make_user_and_key(client, admin_headers, "gwen")
    _grant(client, admin_headers, user_b["id"], 100_000)

    file_id = _upload(client, key_a, [_batch_line("r1")])
    resp = client.post(
        "/v1/batches",
        json={"input_file_id": file_id, "endpoint": ENDPOINT, "completion_window": "24h"},
        headers=_auth(key_b),
    )
    assert resp.status_code == 404


def test_list_batches_scoped_to_user_and_ordered_newest_first(client: TestClient, admin_headers: dict):
    user, key = _make_user_and_key(client, admin_headers, "hana")
    _grant(client, admin_headers, user["id"], 100_000)

    ids = []
    for i in range(3):
        file_id = _upload(client, key, [_batch_line(f"r{i}")])
        batch = client.post(
            "/v1/batches",
            json={"input_file_id": file_id, "endpoint": ENDPOINT, "completion_window": "24h"},
            headers=_auth(key),
        ).json()
        ids.append(batch["id"])

    listing = client.get("/v1/batches", headers=_auth(key)).json()
    assert listing["object"] == "list"
    returned_ids = [b["id"] for b in listing["data"]]
    assert returned_ids == list(reversed(ids))


def test_cancel_batch_releases_reserved_tokens(client: TestClient, admin_headers: dict):
    user, key = _make_user_and_key(client, admin_headers, "ivan")
    _grant(client, admin_headers, user["id"], 100_000)

    file_id = _upload(client, key, [_batch_line("r1"), _batch_line("r2")])
    batch = client.post(
        "/v1/batches",
        json={"input_file_id": file_id, "endpoint": ENDPOINT, "completion_window": "24h"},
        headers=_auth(key),
    ).json()

    budget_before = client.get("/v1/budget", headers=_auth(key)).json()
    assert budget_before["reserved_tokens"] > 0

    resp = client.post(f"/v1/batches/{batch['id']}/cancel", headers=_auth(key))
    assert resp.status_code == 200
    cancelled = resp.json()
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancelled_at"] is not None

    budget_after = client.get("/v1/budget", headers=_auth(key)).json()
    assert budget_after["reserved_tokens"] == 0
    assert budget_after["available_tokens"] == 100_000


def test_cancel_already_cancelled_batch_conflicts(client: TestClient, admin_headers: dict):
    user, key = _make_user_and_key(client, admin_headers, "jill")
    _grant(client, admin_headers, user["id"], 100_000)
    file_id = _upload(client, key, [_batch_line("r1")])
    batch = client.post(
        "/v1/batches",
        json={"input_file_id": file_id, "endpoint": ENDPOINT, "completion_window": "24h"},
        headers=_auth(key),
    ).json()

    client.post(f"/v1/batches/{batch['id']}/cancel", headers=_auth(key))
    resp = client.post(f"/v1/batches/{batch['id']}/cancel", headers=_auth(key))
    assert resp.status_code == 409
