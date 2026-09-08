"""HTTP-level tests for the student portal: login, session handling,
budget display, key rotation, and the access-control edges.

Uses the real FastAPI app with a mock directory swapped in at
app.state.ldap -- the same injection seam the dispatcher tests use for
llama-server.
"""

from __future__ import annotations

import dataclasses
import json
import re

import pytest
from fastapi.testclient import TestClient
from tests.fake_ldap import GROUP_DN, PEOPLE_DN, make_connection_factory

from batchsvc.config import LdapConfig, PortalConfig, Settings
from batchsvc.ldap_auth import LdapAuthenticator
from batchsvc.main import create_app
from batchsvc.models import ApiKey, User
from batchsvc.portal.session import COOKIE_NAME

ENDPOINT = "/v1/chat/completions"


def _ldap_config(**overrides) -> LdapConfig:
    base = {
        "server_uri": "ldap://fake",
        "bind_mode": "direct",
        "user_dn_template": "uid={username}," + PEOPLE_DN,
        "start_tls": False,
    }
    return LdapConfig(**{**base, **overrides})


def _portal_config(**overrides) -> PortalConfig:
    base = {
        "session_secret": "test-portal-secret",
        "cookie_secure": False,  # TestClient talks http://
        "auto_provision": True,
        "default_grant_tokens": 50_000,
    }
    return PortalConfig(**{**base, **overrides})


def _build_client(settings: Settings, *, ldap=None, portal=None, directory=None) -> TestClient:
    configured = dataclasses.replace(
        settings, ldap=ldap or _ldap_config(), portal=portal or _portal_config()
    )
    app = create_app(configured)
    if configured.ldap is not None:
        app.state.ldap = LdapAuthenticator(
            configured.ldap, connection_factory=make_connection_factory(directory)
        )
    return TestClient(app)


@pytest.fixture
def portal_client(settings: Settings) -> TestClient:
    return _build_client(settings)


def _login(client: TestClient, username: str, password: str):
    return client.post(
        "/portal/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no csrf token in page"
    return match.group(1)


# --- Login ---


def test_login_page_renders(portal_client: TestClient):
    resp = portal_client.get("/portal/login")
    assert resp.status_code == 200
    assert "Sign in" in resp.text
    assert 'name="password"' in resp.text


def test_successful_login_sets_session_and_redirects(portal_client: TestClient):
    resp = _login(portal_client, "alice", "alice-pw")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal/"
    cookie = resp.cookies.get(COOKIE_NAME)
    assert cookie


def test_session_cookie_is_httponly_and_scoped_to_the_portal(portal_client: TestClient):
    resp = _login(portal_client, "alice", "alice-pw")
    set_cookie = resp.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/portal" in set_cookie


def test_wrong_password_is_rejected_without_a_session(portal_client: TestClient):
    resp = _login(portal_client, "alice", "wrong-password")
    assert resp.status_code == 401
    assert COOKIE_NAME not in resp.cookies
    assert "couldn&#39;t sign you in" in resp.text or "couldn't sign you in" in resp.text


def test_unknown_user_gets_the_same_message_as_a_wrong_password(portal_client: TestClient):
    wrong_pw = _login(portal_client, "alice", "wrong-password")
    unknown = _login(portal_client, "definitely-not-a-student", "whatever")
    assert unknown.status_code == wrong_pw.status_code == 401
    # Identical wording: the login form must not reveal who exists.
    assert unknown.text == wrong_pw.text


@pytest.mark.parametrize(
    "body",
    [
        "username=alice&password=",  # present but blank
        "username=alice",  # field omitted entirely
    ],
)
def test_empty_password_does_not_log_you_in(portal_client: TestClient, body: str):
    """An LDAP bind with a real DN and an empty password succeeds as an
    unauthenticated bind, so 'leave the box blank' must not be a way in.
    Sent as a raw body because httpx drops empty form values."""
    resp = portal_client.post(
        "/portal/login",
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert COOKIE_NAME not in resp.cookies
    # And it comes back as the login page, not a raw validation blob.
    assert "Sign in" in resp.text


def test_login_is_rate_limited(settings: Settings):
    client = _build_client(settings, portal=_portal_config(login_attempts_per_minute=3))
    for _ in range(3):
        assert _login(client, "alice", "bad").status_code == 401
    limited = _login(client, "alice", "bad")
    assert limited.status_code == 429
    # And a correct password is refused too while the limit holds, so the
    # limiter can't be walked around by guessing right on attempt N+1.
    assert _login(client, "alice", "alice-pw").status_code == 429


# --- Provisioning and access control ---


def test_first_login_provisions_the_account_with_the_default_grant(
    settings: Settings, admin_headers: dict
):
    client = _build_client(settings, portal=_portal_config(default_grant_tokens=25_000))
    _login(client, "alice", "alice-pw")

    users = client.get("/admin/users", headers=admin_headers).json()
    alice = next(u for u in users if u["username"] == "alice")
    assert alice["full_name"] == "Alice Andersson"

    budget = client.get(f"/admin/users/{alice['id']}/budget", headers=admin_headers).json()
    assert budget["granted_tokens"] == 25_000
    assert budget["available_tokens"] == 25_000


def test_second_login_does_not_grant_again(settings: Settings, admin_headers: dict):
    client = _build_client(settings, portal=_portal_config(default_grant_tokens=25_000))
    _login(client, "alice", "alice-pw")
    _login(client, "alice", "alice-pw")

    users = client.get("/admin/users", headers=admin_headers).json()
    alice = next(u for u in users if u["username"] == "alice")
    budget = client.get(f"/admin/users/{alice['id']}/budget", headers=admin_headers).json()
    assert budget["granted_tokens"] == 25_000


def test_unknown_account_is_refused_when_auto_provisioning_is_off(settings: Settings):
    client = _build_client(settings, portal=_portal_config(auto_provision=False))
    resp = _login(client, "alice", "alice-pw")
    assert resp.status_code == 403
    assert "Ask your instructor" in resp.text


def test_preexisting_account_logs_in_with_auto_provisioning_off(
    settings: Settings, admin_headers: dict
):
    client = _build_client(settings, portal=_portal_config(auto_provision=False))
    client.post("/admin/users", json={"username": "alice"}, headers=admin_headers)
    assert _login(client, "alice", "alice-pw").status_code == 303


def test_disabled_account_cannot_log_in(settings: Settings, admin_headers: dict):
    client = _build_client(settings)
    _login(client, "alice", "alice-pw")
    users = client.get("/admin/users", headers=admin_headers).json()
    alice = next(u for u in users if u["username"] == "alice")
    client.post(f"/admin/users/{alice['id']}/disable", headers=admin_headers)

    resp = _login(client, "alice", "alice-pw")
    assert resp.status_code == 403


def test_group_requirement_is_enforced_at_the_portal(settings: Settings):
    client = _build_client(settings, ldap=_ldap_config(required_group_dn=GROUP_DN))
    assert _login(client, "alice", "alice-pw").status_code == 303  # enrolled
    assert _login(client, "carol", "carol-pw").status_code == 401  # not enrolled


# --- Dashboard ---


def test_dashboard_requires_a_session(portal_client: TestClient):
    resp = portal_client.get("/portal/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal/login"


def test_dashboard_shows_budget_figures(portal_client: TestClient):
    _login(portal_client, "alice", "alice-pw")
    resp = portal_client.get("/portal/")
    assert resp.status_code == 200
    assert "Alice Andersson" in resp.text
    assert "50,000" in resp.text  # the default grant, formatted
    assert "Token budget" in resp.text


def test_dashboard_shows_jobs_and_their_token_cost(
    settings: Settings, admin_headers: dict, tmp_path
):
    client = _build_client(settings)
    _login(client, "alice", "alice-pw")

    # Give alice an API key and run a batch through the real API, then
    # settle one task so there's a charge to display.
    users = client.get("/admin/users", headers=admin_headers).json()
    alice_id = next(u for u in users if u["username"] == "alice")["id"]
    key = client.post(
        f"/admin/users/{alice_id}/api-keys", json={}, headers=admin_headers
    ).json()["key"]
    auth = {"Authorization": f"Bearer {key}"}

    line = {
        "custom_id": "r1",
        "method": "POST",
        "url": ENDPOINT,
        "body": {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 50},
    }
    file_id = client.post(
        "/v1/files",
        files={"file": ("in.jsonl", (json.dumps(line) + "\n").encode(), "application/jsonl")},
        data={"purpose": "batch"},
        headers=auth,
    ).json()["id"]
    batch = client.post(
        "/v1/batches",
        json={"input_file_id": file_id, "endpoint": ENDPOINT, "completion_window": "24h"},
        headers=auth,
    ).json()

    from batchsvc import batch_ops
    from batchsvc.models import Task

    app_db = client.app.state.db
    with app_db.session_scope() as session:
        task = session.query(Task).filter(Task.batch_id == batch["id"]).one()
        batch_ops.complete_task(
            session,
            task=task,
            response_body={"choices": []},
            prompt_tokens=30,
            completion_tokens=20,
            blob_dir=client.app.state.settings.blob_dir,
        )

    page = client.get("/portal/").text
    assert batch["id"][:20] in page
    assert "completed" in page
    assert "50" in page  # 30 + 20 tokens charged


# --- API key rotation ---


def test_new_account_has_no_key_and_can_generate_one(portal_client: TestClient):
    _login(portal_client, "alice", "alice-pw")
    page = portal_client.get("/portal/").text
    assert "have an API key yet" in page

    resp = portal_client.post("/portal/api-key", data={"csrf_token": _csrf_from(page)})
    assert resp.status_code == 200
    assert "only time it will be shown" in resp.text
    assert "sk-" in resp.text


def test_generated_key_actually_works_against_the_api(portal_client: TestClient):
    _login(portal_client, "alice", "alice-pw")
    page = portal_client.get("/portal/").text
    resp = portal_client.post("/portal/api-key", data={"csrf_token": _csrf_from(page)})

    match = re.search(r'class="keybox">(sk-[^<]+)</code>', resp.text)
    assert match, "new key not shown on the page"
    raw_key = match.group(1)

    budget = portal_client.get("/v1/budget", headers={"Authorization": f"Bearer {raw_key}"})
    assert budget.status_code == 200
    assert budget.json()["available_tokens"] == 50_000


def test_regenerating_revokes_the_previous_key(portal_client: TestClient):
    _login(portal_client, "alice", "alice-pw")
    page = portal_client.get("/portal/").text
    first = portal_client.post("/portal/api-key", data={"csrf_token": _csrf_from(page)})
    first_key = re.search(r'class="keybox">(sk-[^<]+)</code>', first.text).group(1)

    second = portal_client.post("/portal/api-key", data={"csrf_token": _csrf_from(first.text)})
    second_key = re.search(r'class="keybox">(sk-[^<]+)</code>', second.text).group(1)
    assert second_key != first_key

    assert (
        portal_client.get("/v1/budget", headers={"Authorization": f"Bearer {first_key}"}).status_code
        == 401
    )
    assert (
        portal_client.get(
            "/v1/budget", headers={"Authorization": f"Bearer {second_key}"}
        ).status_code
        == 200
    )


def test_key_rotation_requires_a_valid_csrf_token(portal_client: TestClient):
    _login(portal_client, "alice", "alice-pw")
    resp = portal_client.post("/portal/api-key", data={"csrf_token": "forged"})
    assert resp.status_code == 400
    assert "Invalid form token" in resp.text


def test_key_rotation_requires_a_session(portal_client: TestClient):
    resp = portal_client.post(
        "/portal/api-key", data={"csrf_token": "anything"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal/login"


def test_only_your_own_key_is_replaced(settings: Settings, admin_headers: dict):
    """Rotating alice's key must not touch bob's."""
    client = _build_client(settings)
    _login(client, "bob", "bob-pw")
    bob_page = client.get("/portal/").text
    bob_resp = client.post("/portal/api-key", data={"csrf_token": _csrf_from(bob_page)})
    bob_key = re.search(r'class="keybox">(sk-[^<]+)</code>', bob_resp.text).group(1)

    client.post("/portal/logout", data={"csrf_token": _csrf_from(bob_resp.text)})
    _login(client, "alice", "alice-pw")
    alice_page = client.get("/portal/").text
    client.post("/portal/api-key", data={"csrf_token": _csrf_from(alice_page)})

    assert (
        client.get("/v1/budget", headers={"Authorization": f"Bearer {bob_key}"}).status_code == 200
    )


# --- Session lifecycle ---


def test_logout_clears_the_session(portal_client: TestClient):
    _login(portal_client, "alice", "alice-pw")
    page = portal_client.get("/portal/").text
    portal_client.post("/portal/logout", data={"csrf_token": _csrf_from(page)})

    resp = portal_client.get("/portal/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal/login"


def test_tampered_session_cookie_is_not_accepted(portal_client: TestClient):
    _login(portal_client, "alice", "alice-pw")
    portal_client.cookies.set(COOKIE_NAME, "forged-cookie-value", path="/portal")
    resp = portal_client.get("/portal/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal/login"


def test_portal_is_unavailable_without_a_session_secret(settings: Settings):
    client = _build_client(settings, portal=_portal_config(session_secret=""))
    resp = client.get("/portal/login")
    assert resp.status_code == 503
    assert "not enabled" in resp.text


def test_portal_reports_clearly_when_no_directory_is_configured(settings: Settings):
    configured = dataclasses.replace(settings, ldap=None, portal=_portal_config())
    client = TestClient(create_app(configured))
    resp = client.post(
        "/portal/login", data={"username": "alice", "password": "alice-pw"}, follow_redirects=False
    )
    assert resp.status_code == 503
    assert "No directory is configured" in resp.text


def test_portal_never_stores_the_raw_key(portal_client: TestClient):
    """The portal shows a key once; what lands in the database is only
    its hash and prefix."""
    _login(portal_client, "alice", "alice-pw")
    page = portal_client.get("/portal/").text
    resp = portal_client.post("/portal/api-key", data={"csrf_token": _csrf_from(page)})
    raw_key = re.search(r'class="keybox">(sk-[^<]+)</code>', resp.text).group(1)

    with portal_client.app.state.db.session_scope() as session:
        user = session.query(User).filter(User.username == "alice").one()
        keys = session.query(ApiKey).filter(ApiKey.user_id == user.id).all()
        assert keys
        for key in keys:
            assert raw_key not in (key.key_hash, key.label or "")
            assert key.key_prefix == raw_key[: len(key.key_prefix)]


def test_directory_supplied_display_name_is_escaped(settings: Settings):
    """cn comes from the directory, not from us. If someone can set a
    display name containing markup, it must render as text."""
    from tests.fake_ldap import PEOPLE_DN as PDN
    from tests.fake_ldap import default_directory

    directory = default_directory()
    directory[f"uid=mallory,{PDN}"] = {
        "objectClass": "inetOrgPerson",
        "userPassword": "mallory-pw",
        "cn": "<script>alert('xss')</script>",
        "sn": "M",
        "mail": "m@example.edu",
    }
    client = _build_client(settings, directory=directory)
    _login(client, "mallory", "mallory-pw")

    page = client.get("/portal/").text
    assert "<script>alert('xss')</script>" not in page
    assert "&lt;script&gt;" in page
