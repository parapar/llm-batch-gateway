"""HTTP-level tests for /v1/files."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient


def _make_user_and_key(client: TestClient, admin_headers: dict, username: str) -> tuple[dict, str]:
    user = client.post("/admin/users", json={"username": username}, headers=admin_headers).json()
    key = client.post(f"/admin/users/{user['id']}/api-keys", json={}, headers=admin_headers).json()["key"]
    return user, key


def _auth(raw_key: str) -> dict:
    return {"Authorization": f"Bearer {raw_key}"}


def test_upload_requires_purpose_batch(client: TestClient, admin_headers: dict):
    _, key = _make_user_and_key(client, admin_headers, "nina")
    resp = client.post(
        "/v1/files",
        files={"file": ("input.jsonl", b'{"a":1}\n', "application/jsonl")},
        data={"purpose": "fine-tune"},
        headers=_auth(key),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "purpose"


def test_upload_rejects_empty_file(client: TestClient, admin_headers: dict):
    _, key = _make_user_and_key(client, admin_headers, "oscar")
    resp = client.post(
        "/v1/files",
        files={"file": ("input.jsonl", b"", "application/jsonl")},
        data={"purpose": "batch"},
        headers=_auth(key),
    )
    assert resp.status_code == 400


def test_upload_get_and_download_roundtrip(client: TestClient, admin_headers: dict):
    _, key = _make_user_and_key(client, admin_headers, "penny")
    content = b'{"custom_id":"1","method":"POST","url":"/v1/chat/completions","body":{}}\n'
    resp = client.post(
        "/v1/files",
        files={"file": ("input.jsonl", content, "application/jsonl")},
        data={"purpose": "batch"},
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text
    file_obj = resp.json()
    assert file_obj["object"] == "file"
    assert file_obj["purpose"] == "batch"
    assert file_obj["bytes"] == len(content)
    assert file_obj["filename"] == "input.jsonl"

    meta_resp = client.get(f"/v1/files/{file_obj['id']}", headers=_auth(key))
    assert meta_resp.status_code == 200
    assert meta_resp.json()["id"] == file_obj["id"]

    content_resp = client.get(f"/v1/files/{file_obj['id']}/content", headers=_auth(key))
    assert content_resp.status_code == 200
    assert content_resp.content == content


def test_file_access_requires_auth(client: TestClient, admin_headers: dict):
    _, key = _make_user_and_key(client, admin_headers, "quinn")
    resp = client.post(
        "/v1/files",
        files={"file": ("input.jsonl", b'{"a":1}\n', "application/jsonl")},
        data={"purpose": "batch"},
        headers=_auth(key),
    )
    file_id = resp.json()["id"]

    unauthed = client.get(f"/v1/files/{file_id}")
    assert unauthed.status_code == 401


def test_file_not_visible_to_other_user(client: TestClient, admin_headers: dict):
    _, key_a = _make_user_and_key(client, admin_headers, "riley")
    _, key_b = _make_user_and_key(client, admin_headers, "sam")

    resp = client.post(
        "/v1/files",
        files={"file": ("input.jsonl", b'{"a":1}\n', "application/jsonl")},
        data={"purpose": "batch"},
        headers=_auth(key_a),
    )
    file_id = resp.json()["id"]

    other_resp = client.get(f"/v1/files/{file_id}", headers=_auth(key_b))
    assert other_resp.status_code == 404


def test_uploaded_content_is_not_pre_validated_as_jsonl(client: TestClient, admin_headers: dict):
    """Upload is generic (matches the real Files API); JSONL schema
    validation happens at batch creation time, not upload time."""
    _, key = _make_user_and_key(client, admin_headers, "tara")
    garbage = b"not even json\n"
    resp = client.post(
        "/v1/files",
        files={"file": ("input.jsonl", garbage, "application/jsonl")},
        data={"purpose": "batch"},
        headers=_auth(key),
    )
    assert resp.status_code == 201
    # Sanity: what we uploaded really isn't valid JSON.
    try:
        json.loads(garbage)
        raise AssertionError("expected garbage to be invalid JSON")
    except json.JSONDecodeError:
        pass
