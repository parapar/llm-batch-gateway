from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from batchsvc.config import Settings
from batchsvc.db import Database, build_database
from batchsvc.main import create_app

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_path=tmp_path / "test.db",
        blob_dir=tmp_path / "blobs",
        admin_token=ADMIN_TOKEN,
    )


@pytest.fixture
def db(settings: Settings) -> Database:
    return build_database(settings)


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}
