"""Config loading for the portal/LDAP sections.

Worth its own tests because two of the behaviours here are safety
defaults that fail *closed*: an ldap section that isn't enabled yields
no authenticator at all, and a blank session secret leaves the portal
switched off rather than signing cookies with a placeholder.
"""

from __future__ import annotations

import yaml

from batchsvc.config import load_settings


def _write(tmp_path, config: dict):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def test_missing_ldap_section_yields_no_directory(tmp_path):
    settings = load_settings(_write(tmp_path, {"admin_token": "x"}))
    assert settings.ldap is None


def test_disabled_ldap_section_yields_no_directory(tmp_path):
    path = _write(tmp_path, {"ldap": {"enabled": False, "server_uri": "ldap://x"}})
    assert load_settings(path).ldap is None


def test_ldap_section_without_server_uri_yields_no_directory(tmp_path):
    assert load_settings(_write(tmp_path, {"ldap": {"enabled": True}})).ldap is None


def test_full_ldap_section_is_loaded(tmp_path):
    path = _write(
        tmp_path,
        {
            "ldap": {
                "enabled": True,
                "server_uri": "ldaps://dir.example.edu:636",
                "bind_mode": "search",
                "service_account_dn": "cn=svc,dc=example,dc=edu",
                "service_account_password": "from-file",
                "search_base": "ou=people,dc=example,dc=edu",
                "search_filter": "(sAMAccountName={username})",
                "use_ssl": True,
                "start_tls": False,
                "required_group_dn": "cn=course,dc=example,dc=edu",
            }
        },
    )
    ldap = load_settings(path).ldap
    assert ldap is not None
    assert ldap.bind_mode == "search"
    assert ldap.search_filter == "(sAMAccountName={username})"
    assert ldap.use_ssl is True
    assert ldap.required_group_dn == "cn=course,dc=example,dc=edu"


def test_service_password_env_var_overrides_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BATCHSVC_LDAP_SERVICE_PASSWORD", "from-env")
    path = _write(
        tmp_path,
        {"ldap": {"enabled": True, "server_uri": "ldap://x", "service_account_password": "from-file"}},
    )
    assert load_settings(path).ldap.service_account_password == "from-env"


def test_portal_secret_env_var_overrides_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BATCHSVC_PORTAL_SECRET", "from-env")
    path = _write(tmp_path, {"portal": {"session_secret": "from-file"}})
    assert load_settings(path).portal.session_secret == "from-env"


def test_portal_defaults_are_safe(tmp_path):
    portal = load_settings(_write(tmp_path, {})).portal
    assert portal.session_secret == ""  # no secret => portal serves "not enabled"
    assert portal.cookie_secure is True  # https-only cookies unless explicitly relaxed


def test_shipped_example_config_is_inert(tmp_path):
    """config.example.yaml is the fallback when no config.yaml exists, so
    it must never come up with a live portal or directory."""
    settings = load_settings(__import__("pathlib").Path("config/config.example.yaml"))
    assert settings.ldap is None
    assert settings.portal.session_secret == ""
