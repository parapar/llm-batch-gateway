"""Configuration loading.

Settings come from a YAML file (config/config.yaml by default, override
with BATCHSVC_CONFIG) with environment variables layered on top for the
values that are sensitive or environment-specific (currently just the
admin token).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class NodeConfig:
    name: str
    base_url: str
    parallel_slots: int = 4


@dataclass(frozen=True)
class LdapConfig:
    """Directory settings for the student portal's login (see ldap_auth.py).

    Two bind modes:
      - "direct": the student's DN is derivable from their username, so we
        bind straight as them using user_dn_template.
      - "search": bind as service_account_dn first, find the student with
        search_filter under search_base, then rebind as the DN found.
    """

    server_uri: str
    bind_mode: str = "direct"  # "direct" | "search"
    user_dn_template: str = "uid={username},ou=people"
    service_account_dn: str | None = None
    service_account_password: str | None = None
    search_base: str | None = None
    search_filter: str = "(uid={username})"
    use_ssl: bool = False
    start_tls: bool = True
    ca_certs_file: str | None = None
    timeout_seconds: float = 10.0
    attr_display_name: str = "cn"
    attr_email: str = "mail"
    attr_member_of: str = "memberOf"
    # Empty/None = any successful bind is allowed.
    required_group_dn: str | None = None


@dataclass(frozen=True)
class PortalConfig:
    enabled: bool = True
    # Signs session cookies. Set via BATCHSVC_PORTAL_SECRET in real
    # deployments; a blank secret disables the portal rather than
    # silently signing with something guessable.
    session_secret: str = ""
    session_lifetime_minutes: int = 480
    # Send cookies only over HTTPS. Turn off only for local http:// dev.
    cookie_secure: bool = True
    auto_provision: bool = True
    default_grant_tokens: int = 100_000
    login_attempts_per_minute: int = 10


@dataclass(frozen=True)
class Settings:
    database_path: Path
    blob_dir: Path
    admin_token: str
    default_max_tokens: int = 512
    result_retention_days: int = 7
    nodes: list[NodeConfig] = field(default_factory=list)

    # Dispatcher (M3) tunables.
    task_max_attempts: int = 3
    task_request_timeout_seconds: float = 300.0
    node_unhealthy_threshold: int = 3
    health_check_interval_seconds: float = 15.0
    dispatch_idle_poll_seconds: float = 1.0

    # ETA estimation (M4) tunables.
    eta_ewma_alpha: float = 0.3
    eta_min_samples_for_confidence: int = 5
    # Bootstrap values used until enough real samples exist -- deliberately
    # conservative (slow) defaults matching the AMD Strix Halo boxes this
    # was built for, not a fast GPU box.
    eta_bootstrap_tokens_per_second: float = 5.0
    eta_bootstrap_completion_tokens: float = 200.0

    # Retention job (M5) tunables.
    retention_check_interval_seconds: float = 3600.0

    # Student portal (M6). ldap=None means no directory configured, in
    # which case the portal serves a clear "not configured" error rather
    # than a login form that can't work.
    ldap: LdapConfig | None = None
    portal: PortalConfig = field(default_factory=PortalConfig)

    # Logging (M5). JSON lines by default (easy to ship to a log
    # aggregator); set false for human-readable console output during
    # local development.
    log_json: bool = True

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path}"


def _config_path() -> Path:
    env_path = os.environ.get("BATCHSVC_CONFIG")
    if env_path:
        return Path(env_path)
    repo_local = Path("config/config.yaml")
    if repo_local.exists():
        return repo_local
    return Path("config/config.example.yaml")


def load_settings(path: Path | None = None) -> Settings:
    cfg_path = path or _config_path()
    raw: dict = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text()) or {}

    database_path = Path(raw.get("database_path", "data/batchsvc.db"))
    blob_dir = Path(raw.get("blob_dir", "data/blobs"))
    admin_token = os.environ.get("BATCHSVC_ADMIN_TOKEN", raw.get("admin_token", ""))
    nodes = [
        NodeConfig(
            name=n["name"],
            base_url=n["base_url"],
            parallel_slots=int(n.get("parallel_slots", 4)),
        )
        for n in raw.get("nodes") or []
    ]

    return Settings(
        database_path=database_path,
        blob_dir=blob_dir,
        admin_token=admin_token,
        default_max_tokens=int(raw.get("default_max_tokens", 512)),
        result_retention_days=int(raw.get("result_retention_days", 7)),
        nodes=nodes,
        task_max_attempts=int(raw.get("task_max_attempts", 3)),
        task_request_timeout_seconds=float(raw.get("task_request_timeout_seconds", 300.0)),
        node_unhealthy_threshold=int(raw.get("node_unhealthy_threshold", 3)),
        health_check_interval_seconds=float(raw.get("health_check_interval_seconds", 15.0)),
        dispatch_idle_poll_seconds=float(raw.get("dispatch_idle_poll_seconds", 1.0)),
        eta_ewma_alpha=float(raw.get("eta_ewma_alpha", 0.3)),
        eta_min_samples_for_confidence=int(raw.get("eta_min_samples_for_confidence", 5)),
        eta_bootstrap_tokens_per_second=float(raw.get("eta_bootstrap_tokens_per_second", 5.0)),
        eta_bootstrap_completion_tokens=float(raw.get("eta_bootstrap_completion_tokens", 200.0)),
        retention_check_interval_seconds=float(raw.get("retention_check_interval_seconds", 3600.0)),
        log_json=bool(raw.get("log_json", True)),
        ldap=_load_ldap(raw.get("ldap")),
        portal=_load_portal(raw.get("portal")),
    )


def _load_ldap(raw: dict | None) -> LdapConfig | None:
    if not raw or not raw.get("enabled", True) or not raw.get("server_uri"):
        return None
    defaults = LdapConfig(server_uri="")
    return LdapConfig(
        server_uri=raw["server_uri"],
        bind_mode=raw.get("bind_mode", defaults.bind_mode),
        user_dn_template=raw.get("user_dn_template", defaults.user_dn_template),
        service_account_dn=raw.get("service_account_dn"),
        # Prefer the env var: keeps the service account password out of a
        # config file that tends to end up in version control.
        service_account_password=os.environ.get(
            "BATCHSVC_LDAP_SERVICE_PASSWORD", raw.get("service_account_password")
        ),
        search_base=raw.get("search_base"),
        search_filter=raw.get("search_filter", defaults.search_filter),
        use_ssl=bool(raw.get("use_ssl", defaults.use_ssl)),
        start_tls=bool(raw.get("start_tls", defaults.start_tls)),
        ca_certs_file=raw.get("ca_certs_file"),
        timeout_seconds=float(raw.get("timeout_seconds", defaults.timeout_seconds)),
        attr_display_name=raw.get("attr_display_name", defaults.attr_display_name),
        attr_email=raw.get("attr_email", defaults.attr_email),
        attr_member_of=raw.get("attr_member_of", defaults.attr_member_of),
        required_group_dn=raw.get("required_group_dn") or None,
    )


def _load_portal(raw: dict | None) -> PortalConfig:
    raw = raw or {}
    defaults = PortalConfig()
    return PortalConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        session_secret=os.environ.get("BATCHSVC_PORTAL_SECRET", raw.get("session_secret", "")),
        session_lifetime_minutes=int(
            raw.get("session_lifetime_minutes", defaults.session_lifetime_minutes)
        ),
        cookie_secure=bool(raw.get("cookie_secure", defaults.cookie_secure)),
        auto_provision=bool(raw.get("auto_provision", defaults.auto_provision)),
        default_grant_tokens=int(raw.get("default_grant_tokens", defaults.default_grant_tokens)),
        login_attempts_per_minute=int(
            raw.get("login_attempts_per_minute", defaults.login_attempts_per_minute)
        ),
    )
