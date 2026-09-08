"""HTTP-level tests for the admin API (users, api keys, budget, ledger)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_admin_endpoints_require_admin_token(client: TestClient):
    resp = client.get("/admin/users")
    assert resp.status_code == 401

    resp = client.get("/admin/users", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 403


def test_create_and_list_user(client: TestClient, admin_headers: dict):
    payload = {"username": "alice", "full_name": "Alice A."}
    resp = client.post("/admin/users", json=payload, headers=admin_headers)
    assert resp.status_code == 201, resp.text
    user = resp.json()
    assert user["username"] == "alice"
    assert user["is_active"] is True

    resp = client.get("/admin/users", headers=admin_headers)
    assert resp.status_code == 200
    usernames = [u["username"] for u in resp.json()]
    assert "alice" in usernames


def test_duplicate_username_rejected(client: TestClient, admin_headers: dict):
    client.post("/admin/users", json={"username": "bob"}, headers=admin_headers)
    resp = client.post("/admin/users", json={"username": "bob"}, headers=admin_headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


def test_new_user_has_zero_budget(client: TestClient, admin_headers: dict):
    user = client.post("/admin/users", json={"username": "carol"}, headers=admin_headers).json()
    resp = client.get(f"/admin/users/{user['id']}/budget", headers=admin_headers)
    assert resp.status_code == 200
    budget = resp.json()
    assert budget == {
        "user_id": user["id"],
        "granted_tokens": 0,
        "reserved_tokens": 0,
        "used_tokens": 0,
        "available_tokens": 0,
        "updated_at": budget["updated_at"],
    }


def test_grant_budget_updates_available(client: TestClient, admin_headers: dict):
    user = client.post("/admin/users", json={"username": "dave"}, headers=admin_headers).json()
    resp = client.post(
        f"/admin/users/{user['id']}/budget/grant",
        json={"tokens": 5000, "note": "semester allowance"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["available_tokens"] == 5000

    ledger_resp = client.get(f"/admin/users/{user['id']}/ledger", headers=admin_headers)
    assert ledger_resp.status_code == 200
    entries = ledger_resp.json()
    assert len(entries) == 1
    assert entries[0]["entry_type"] == "grant"
    assert entries[0]["granted_delta"] == 5000
    assert entries[0]["note"] == "semester allowance"


def test_grant_rejects_non_positive_amount(client: TestClient, admin_headers: dict):
    user = client.post("/admin/users", json={"username": "erin"}, headers=admin_headers).json()
    resp = client.post(
        f"/admin/users/{user['id']}/budget/grant", json={"tokens": 0}, headers=admin_headers
    )
    assert resp.status_code == 422  # pydantic Field(gt=0) rejects at the schema level


def test_budget_operations_on_unknown_user_404(client: TestClient, admin_headers: dict):
    resp = client.get("/admin/users/does-not-exist/budget", headers=admin_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_create_list_and_revoke_api_key(client: TestClient, admin_headers: dict):
    user = client.post("/admin/users", json={"username": "frank"}, headers=admin_headers).json()

    resp = client.post(f"/admin/users/{user['id']}/api-keys", json={"label": "laptop"}, headers=admin_headers)
    assert resp.status_code == 201
    created = resp.json()
    assert created["key"].startswith("sk-")
    assert created["key_prefix"] == created["key"][: len(created["key_prefix"])]

    resp = client.get(f"/admin/users/{user['id']}/api-keys", headers=admin_headers)
    assert resp.status_code == 200
    keys = resp.json()
    assert len(keys) == 1
    assert keys[0]["revoked_at"] is None
    assert "key_hash" not in keys[0]  # never exposed over the API

    resp = client.delete(f"/admin/api-keys/{created['id']}", headers=admin_headers)
    assert resp.status_code == 204

    resp = client.get(f"/admin/users/{user['id']}/api-keys", headers=admin_headers)
    assert resp.json()[0]["revoked_at"] is not None


def test_disable_and_enable_user(client: TestClient, admin_headers: dict):
    user = client.post("/admin/users", json={"username": "gina"}, headers=admin_headers).json()
    resp = client.post(f"/admin/users/{user['id']}/disable", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    resp = client.post(f"/admin/users/{user['id']}/enable", headers=admin_headers)
    assert resp.json()["is_active"] is True


def test_reconcile_is_noop_when_no_drift(client: TestClient, admin_headers: dict):
    user = client.post("/admin/users", json={"username": "hank"}, headers=admin_headers).json()
    client.post(f"/admin/users/{user['id']}/budget/grant", json={"tokens": 300}, headers=admin_headers)
    resp = client.post(f"/admin/users/{user['id']}/budget/reconcile", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["available_tokens"] == 300
