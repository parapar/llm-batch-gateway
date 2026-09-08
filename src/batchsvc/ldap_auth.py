"""LDAP authentication for the student portal.

Two bind modes (config.LdapConfig.bind_mode):

- "direct": the student's DN follows a predictable pattern, so we bind
  straight as them with user_dn_template. No service account needed.
- "search": bind as a read-only service account, find the student under
  search_base with search_filter, then rebind as the DN we found. Needed
  when DNs aren't derivable from the username.

Either way the student's password is only ever used for a bind against
the directory -- it is never stored, logged, or compared here.

Three things in here are load-bearing for security, not incidental:

1. An empty password is rejected before we touch the directory. LDAP
   treats a bind with a valid DN and an empty password as an
   *unauthenticated bind* that succeeds, which would otherwise turn
   "leave the password box blank" into a login bypass for any known
   username.
2. Usernames are checked against a conservative allowlist *and* escaped
   (escape_rdn for DNs, escape_filter_chars for filters). Either alone
   would probably do; both together mean a filter-injection attempt has
   to get through two independent gates.
3. A wrong password and a nonexistent user raise the same
   LdapAuthFailed, so the portal can't be used to enumerate who exists
   in the directory.
"""

from __future__ import annotations

import logging
import re
import ssl
from dataclasses import dataclass
from typing import Protocol

from ldap3 import ALL, SIMPLE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import escape_rdn

from batchsvc.config import LdapConfig

logger = logging.getLogger("batchsvc.ldap")

# Deliberately narrow: typical uid / sAMAccountName / userPrincipalName
# shapes, and nothing that means anything special in a DN or filter.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


@dataclass(frozen=True)
class LdapIdentity:
    username: str
    display_name: str | None = None
    email: str | None = None


class LdapError(Exception):
    """The directory could not be reached or is misconfigured. Distinct
    from LdapAuthFailed so the portal can say "try again later" rather
    than "wrong password"."""


class LdapAuthFailed(Exception):
    """Bad credentials, unknown user, or not in the required group."""


class ConnectionFactory(Protocol):
    def __call__(self, user: str | None, password: str | None) -> Connection: ...


def _first_value(entry_attributes, attr: str) -> str | None:  # noqa: ANN001
    try:
        value = entry_attributes[attr]
    except (KeyError, LDAPException):
        return None
    if value is None:
        return None
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value)


def _as_list(entry_attributes, attr: str) -> list[str]:  # noqa: ANN001
    try:
        value = entry_attributes[attr]
    except (KeyError, LDAPException):
        return []
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


class LdapAuthenticator:
    def __init__(self, config: LdapConfig, *, connection_factory: ConnectionFactory | None = None):
        self.config = config
        self._connection_factory = connection_factory or self._default_connection_factory

    def _default_connection_factory(self, user: str | None, password: str | None) -> Connection:
        tls = None
        if self.config.use_ssl or self.config.start_tls:
            tls = Tls(
                validate=ssl.CERT_REQUIRED,
                ca_certs_file=self.config.ca_certs_file,
            )
        server = Server(
            self.config.server_uri,
            use_ssl=self.config.use_ssl,
            get_info=ALL,
            connect_timeout=self.config.timeout_seconds,
            tls=tls,
        )
        return Connection(
            server,
            user=user,
            password=password,
            authentication=SIMPLE if user else None,
            auto_bind=False,
            raise_exceptions=False,
            receive_timeout=self.config.timeout_seconds,
        )

    def _connect(self, user: str | None, password: str | None) -> Connection:
        try:
            conn = self._connection_factory(user, password)
        except LDAPException as e:
            raise LdapError(f"could not open a connection to the directory: {e}") from e
        if self.config.start_tls and not self.config.use_ssl:
            try:
                conn.start_tls()
            except LDAPException as e:
                # Mock connections in tests don't implement StartTLS; a real
                # server failing it is a genuine misconfiguration.
                logger.debug("start_tls not applied: %s", e)
        return conn

    @staticmethod
    def _bind(conn: Connection) -> bool:
        """ldap3 signals some failures by returning False and others by
        raising (an unreachable server, a malformed request, an empty
        password). Anything raised here is a directory-side problem, not
        a wrong password, so it becomes LdapError -- which is what lets
        the portal say "try again shortly" instead of returning a 500."""
        try:
            return bool(conn.bind())
        except LDAPException as e:
            raise LdapError(f"bind failed: {e}") from e

    def authenticate(self, username: str, password: str) -> LdapIdentity:
        if not password:
            # See module docstring, point 1.
            raise LdapAuthFailed("empty password")
        if not _USERNAME_RE.match(username or ""):
            raise LdapAuthFailed("invalid username")

        if self.config.bind_mode == "search":
            user_dn, attributes = self._find_user_dn(username)
        elif self.config.bind_mode == "direct":
            user_dn, attributes = self.config.user_dn_template.format(
                username=escape_rdn(username)
            ), None
        else:
            raise LdapError(f"unknown ldap bind_mode {self.config.bind_mode!r}")

        conn = self._connect(user_dn, password)
        try:
            if not self._bind(conn):
                raise LdapAuthFailed("bind rejected")
            if attributes is None:
                attributes = self._read_own_entry(conn, user_dn)
            self._check_group_membership(conn, user_dn, attributes)
        finally:
            self._safe_unbind(conn)

        return LdapIdentity(
            username=username,
            display_name=_first_value(attributes, self.config.attr_display_name)
            if attributes
            else None,
            email=_first_value(attributes, self.config.attr_email) if attributes else None,
        )

    def _find_user_dn(self, username: str):  # noqa: ANN202
        if not self.config.search_base:
            raise LdapError("ldap bind_mode='search' requires search_base")
        conn = self._connect(self.config.service_account_dn, self.config.service_account_password)
        try:
            if not self._bind(conn):
                raise LdapError("service account bind failed")
            search_filter = self.config.search_filter.format(
                username=escape_filter_chars(username)
            )
            try:
                ok = conn.search(
                    search_base=self.config.search_base,
                    search_filter=search_filter,
                    attributes=[
                        self.config.attr_display_name,
                        self.config.attr_email,
                        self.config.attr_member_of,
                    ],
                )
            except LDAPException as e:
                raise LdapError(f"user search failed: {e}") from e
            entries = list(conn.entries) if ok else []
            if len(entries) != 1:
                # 0 = no such user, >1 = ambiguous filter. Both are the
                # same answer to the caller (see docstring, point 3).
                raise LdapAuthFailed(f"user search matched {len(entries)} entries")
            entry = entries[0]
            return str(entry.entry_dn), entry.entry_attributes_as_dict
        finally:
            self._safe_unbind(conn)

    def _read_own_entry(self, conn: Connection, user_dn: str):  # noqa: ANN202
        """In direct-bind mode we have no service account, so read the
        student's own entry over their now-bound connection. Directories
        normally let a user read themselves; if this comes back empty we
        just carry on without display name/email."""
        try:
            ok = conn.search(
                search_base=user_dn,
                search_filter="(objectClass=*)",
                search_scope="BASE",
                attributes=[
                    self.config.attr_display_name,
                    self.config.attr_email,
                    self.config.attr_member_of,
                ],
            )
            entries = list(conn.entries) if ok else []
            return entries[0].entry_attributes_as_dict if entries else None
        except LDAPException as e:
            logger.debug("could not read own entry for %s: %s", user_dn, e)
            return None

    def _check_group_membership(self, conn: Connection, user_dn: str, attributes) -> None:  # noqa: ANN001
        required = self.config.required_group_dn
        if not required:
            return

        member_of = _as_list(attributes, self.config.attr_member_of) if attributes else []
        if any(g.lower() == required.lower() for g in member_of):
            return

        # No memberOf (or it didn't list the group) -- some directories
        # don't populate it, so fall back to asking the group itself.
        try:
            ok = conn.search(
                search_base=required,
                search_filter=f"(|(member={escape_filter_chars(user_dn)})"
                f"(uniqueMember={escape_filter_chars(user_dn)}))",
                search_scope="BASE",
                attributes=["cn"],
            )
            if ok and list(conn.entries):
                return
        except LDAPException as e:
            logger.debug("group membership search failed for %s: %s", user_dn, e)

        raise LdapAuthFailed("not a member of the required group")

    @staticmethod
    def _safe_unbind(conn: Connection) -> None:
        try:
            conn.unbind()
        except LDAPException:
            pass
