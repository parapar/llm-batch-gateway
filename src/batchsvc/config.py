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
    )
