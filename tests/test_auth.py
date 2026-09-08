"""Student-facing auth: API key bearer auth against /v1/budget."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _create_user_and_key(client: TestClient, admin_headers: dict, username: str) -> tuple[dict, str]:
    user = client.post("/admin/users", json={"username": username}, headers=admin_headers).json()
    key_resp = client.post(f"/admin/users/{user['id']}/api-keys", json={}, headers=admin_headers)
    return user, key_resp.json()["key"]


def test_missing_key_returns_401(client: TestClient):
    resp = client.get("/v1/budget")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"


def test_bogus_key_returns_401(client: TestClient):
    resp = client.get("/v1/budget", headers={"Authorization": "Bearer sk-not-a-real-key"})
    assert resp.status_code == 401


def test_valid_key_returns_own_budget(client: TestClient, admin_headers: dict):
    user, raw_key = _create_user_and_key(client, admin_headers, "ivy")
    client.post(f"/admin/users/{user['id']}/budget/grant", json={"tokens": 777}, headers=admin_headers)

    resp = client.get("/v1/budget", headers={"Authorization": f"Bearer {raw_key}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == user["id"]
    assert body["available_tokens"] == 777


def test_revoked_key_returns_401(client: TestClient, admin_headers: dict):
    user, raw_key = _create_user_and_key(client, admin_headers, "jack")
    keys = client.get(f"/admin/users/{user['id']}/api-keys", headers=admin_headers).json()
    client.delete(f"/admin/api-keys/{keys[0]['id']}", headers=admin_headers)

    resp = client.get("/v1/budget", headers={"Authorization": f"Bearer {raw_key}"})
    assert resp.status_code == 401


def test_disabled_user_key_returns_401(client: TestClient, admin_headers: dict):
    user, raw_key = _create_user_and_key(client, admin_headers, "karen")
    client.post(f"/admin/users/{user['id']}/disable", headers=admin_headers)

    resp = client.get("/v1/budget", headers={"Authorization": f"Bearer {raw_key}"})
    assert resp.status_code == 401


def test_keys_are_scoped_to_their_own_user(client: TestClient, admin_headers: dict):
    user_a, key_a = _create_user_and_key(client, admin_headers, "liam")
    user_b, key_b = _create_user_and_key(client, admin_headers, "mia")
    client.post(f"/admin/users/{user_a['id']}/budget/grant", json={"tokens": 100}, headers=admin_headers)
    client.post(f"/admin/users/{user_b['id']}/budget/grant", json={"tokens": 999}, headers=admin_headers)

    resp_a = client.get("/v1/budget", headers={"Authorization": f"Bearer {key_a}"})
    resp_b = client.get("/v1/budget", headers={"Authorization": f"Bearer {key_b}"})
    assert resp_a.json()["available_tokens"] == 100
    assert resp_b.json()["available_tokens"] == 999
