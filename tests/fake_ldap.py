"""In-process fake LDAP directory for portal tests.

Uses ldap3's own MOCK_SYNC strategy, so binds are validated against real
userPassword attributes and searches run through ldap3's real filter
parser -- the code under test takes the same paths it would against a
live directory, minus the socket.

MOCK_SYNC entries belong to one Connection's strategy, so each connection
the factory hands out is seeded with the same directory -- one
authentication makes two (service account bind, then user bind), and
both need to see the same entries. Nothing under test writes to the
directory, so per-connection seeding is equivalent to a shared one.
"""

from __future__ import annotations

from ldap3 import MOCK_SYNC, Connection, Server

BASE_DN = "dc=example,dc=edu"
PEOPLE_DN = f"ou=people,{BASE_DN}"
GROUP_DN = f"cn=llm-course,ou=groups,{BASE_DN}"
SERVICE_DN = f"cn=svc-batchsvc,{BASE_DN}"
SERVICE_PASSWORD = "service-secret"


def default_directory() -> dict[str, dict]:
    """alice and bob are enrolled (in the course group); carol exists in
    the directory but isn't."""
    return {
        SERVICE_DN: {"objectClass": "person", "userPassword": SERVICE_PASSWORD, "sn": "svc"},
        f"uid=alice,{PEOPLE_DN}": {
            "objectClass": "inetOrgPerson",
            "userPassword": "alice-pw",
            "cn": "Alice Andersson",
            "sn": "Andersson",
            "mail": "alice@example.edu",
            "memberOf": [GROUP_DN],
        },
        f"uid=bob,{PEOPLE_DN}": {
            "objectClass": "inetOrgPerson",
            "userPassword": "bob-pw",
            "cn": "Bob Booth",
            "sn": "Booth",
            "mail": "bob@example.edu",
            "memberOf": [GROUP_DN],
        },
        f"uid=carol,{PEOPLE_DN}": {
            "objectClass": "inetOrgPerson",
            "userPassword": "carol-pw",
            "cn": "Carol Crane",
            "sn": "Crane",
            "mail": "carol@example.edu",
        },
    }


def make_connection_factory(entries: dict[str, dict] | None = None):
    """Returns a ConnectionFactory (see ldap_auth.ConnectionFactory) that
    hands out fresh mock connections onto an identically seeded
    directory."""
    directory = entries if entries is not None else default_directory()

    def factory(user: str | None, password: str | None) -> Connection:
        conn = Connection(
            Server("fake-ldap"),
            user=user,
            password=password,
            client_strategy=MOCK_SYNC,
            raise_exceptions=False,
        )
        for dn, attrs in directory.items():
            conn.strategy.add_entry(dn, attrs)
        return conn

    return factory
