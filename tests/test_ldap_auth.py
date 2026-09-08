"""Unit tests for LDAP authentication, against ldap3's mock directory
(tests/fake_ldap.py) rather than a live server.

The security-relevant behaviours here -- empty passwords, filter
injection, no user enumeration -- are the reason this module exists as
its own layer instead of being inlined into the portal routes.
"""

from __future__ import annotations

import pytest
from tests.fake_ldap import (
    GROUP_DN,
    PEOPLE_DN,
    SERVICE_DN,
    SERVICE_PASSWORD,
    make_connection_factory,
)

from batchsvc.config import LdapConfig
from batchsvc.ldap_auth import LdapAuthenticator, LdapAuthFailed, LdapError


def _direct_config(**overrides) -> LdapConfig:
    base = {
        "server_uri": "ldap://fake",
        "bind_mode": "direct",
        "user_dn_template": "uid={username}," + PEOPLE_DN,
        "start_tls": False,
    }
    return LdapConfig(**{**base, **overrides})


def _search_config(**overrides) -> LdapConfig:
    base = {
        "server_uri": "ldap://fake",
        "bind_mode": "search",
        "service_account_dn": SERVICE_DN,
        "service_account_password": SERVICE_PASSWORD,
        "search_base": PEOPLE_DN,
        "search_filter": "(uid={username})",
        "start_tls": False,
    }
    return LdapConfig(**{**base, **overrides})


def _auth(config: LdapConfig) -> LdapAuthenticator:
    return LdapAuthenticator(config, connection_factory=make_connection_factory())


def test_direct_bind_returns_identity_attributes():
    identity = _auth(_direct_config()).authenticate("alice", "alice-pw")
    assert identity.username == "alice"
    assert identity.display_name == "Alice Andersson"
    assert identity.email == "alice@example.edu"


def test_search_bind_returns_identity_attributes():
    identity = _auth(_search_config()).authenticate("bob", "bob-pw")
    assert identity.username == "bob"
    assert identity.display_name == "Bob Booth"


def test_wrong_password_is_rejected():
    with pytest.raises(LdapAuthFailed):
        _auth(_direct_config()).authenticate("alice", "not-the-password")


def test_empty_password_is_rejected_before_touching_the_directory():
    """LDAP treats a bind with a valid DN and empty password as an
    unauthenticated bind that *succeeds*; without this check, leaving the
    password box blank would log you in as any username you know."""
    with pytest.raises(LdapAuthFailed):
        _auth(_direct_config()).authenticate("alice", "")


def test_unknown_user_fails_the_same_way_as_a_wrong_password():
    # Same exception type in both modes: the portal must not become a way
    # to find out who exists in the directory.
    with pytest.raises(LdapAuthFailed):
        _auth(_direct_config()).authenticate("nosuchperson", "whatever")
    with pytest.raises(LdapAuthFailed):
        _auth(_search_config()).authenticate("nosuchperson", "whatever")


@pytest.mark.parametrize(
    "username",
    [
        "alice)(uid=*",  # filter injection
        "*",  # wildcard match-anyone
        "alice,ou=admins",  # DN injection
        "alice\\",  # escape-char smuggling
        "alice password",  # whitespace
        "",  # empty
        "a" * 65,  # overlong
    ],
)
def test_hostile_usernames_are_rejected(username):
    with pytest.raises(LdapAuthFailed):
        _auth(_search_config()).authenticate(username, "alice-pw")


def test_required_group_admits_members():
    identity = _auth(_search_config(required_group_dn=GROUP_DN)).authenticate("alice", "alice-pw")
    assert identity.username == "alice"


def test_required_group_rejects_non_members():
    # carol has valid credentials but isn't enrolled in the course group.
    with pytest.raises(LdapAuthFailed):
        _auth(_search_config(required_group_dn=GROUP_DN)).authenticate("carol", "carol-pw")


def test_group_check_is_skipped_when_no_group_configured():
    identity = _auth(_search_config(required_group_dn=None)).authenticate("carol", "carol-pw")
    assert identity.username == "carol"


def test_unknown_bind_mode_is_a_configuration_error():
    with pytest.raises(LdapError):
        _auth(_direct_config(bind_mode="telepathy")).authenticate("alice", "alice-pw")


def test_search_mode_requires_a_search_base():
    with pytest.raises(LdapError):
        _auth(_search_config(search_base=None)).authenticate("alice", "alice-pw")


def test_bad_service_account_is_an_error_not_an_auth_failure():
    """A broken service account is our problem, not the student's -- it
    must not be reported to them as 'wrong password'."""
    config = _search_config(service_account_password="wrong-service-password")
    with pytest.raises(LdapError):
        _auth(config).authenticate("alice", "alice-pw")
