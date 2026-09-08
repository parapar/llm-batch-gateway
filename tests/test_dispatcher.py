"""Dispatcher tests: claiming, node capacity, retries, health ejection,
fair-share across users, and crash recovery. All run against real
Task/Batch rows created through the HTTP API (so they're realistic) but
drive the dispatcher directly (dispatch_once/health_check_all) rather
than the background run_forever() loop, for determinism -- same spirit
as M2's tests calling batch_ops.complete_task/fail_task directly.
"""

from __future__ import annotations

import dataclasses
import json

import httpx
from fastapi.testclient import TestClient
from tests.fake_llama_node import make_fake_llama_app

from batchsvc.config import NodeConfig, Settings
from batchsvc.dispatcher import Dispatcher
from batchsvc.llama_client import LlamaClient
from batchsvc.models import Node, NodeHealth, Task, TaskStatus

ENDPOINT = "/v1/chat/completions"


def _auth(raw_key: str) -> dict:
    return {"Authorization": f"Bearer {raw_key}"}


def _make_user_and_key(client: TestClient, admin_headers: dict, username: str) -> tuple[dict, str]:
    user = client.post("/admin/users", json={"username": username}, headers=admin_headers).json()
    key = client.post(f"/admin/users/{user['id']}/api-keys", json={}, headers=admin_headers).json()["key"]
    return user, key


def _grant(client: TestClient, admin_headers: dict, user_id: str, tokens: int) -> None:
    resp = client.post(f"/admin/users/{user_id}/budget/grant", json={"tokens": tokens}, headers=admin_headers)
    assert resp.status_code == 200


def _batch_line(custom_id: str, *, content: str = "Hello!", max_tokens: int = 100) -> dict:
    body = {"messages": [{"role": "user", "content": content}], "max_tokens": max_tokens}
    return {"custom_id": custom_id, "method": "POST", "url": ENDPOINT, "body": body}


def _jsonl(lines: list[dict]) -> bytes:
    return ("\n".join(json.dumps(line) for line in lines) + "\n").encode("utf-8")


def _submit_batch(client: TestClient, key: str, lines: list[dict]) -> dict:
    file_resp = client.post(
        "/v1/files",
        files={"file": ("input.jsonl", _jsonl(lines), "application/jsonl")},
        data={"purpose": "batch"},
        headers=_auth(key),
    )
    file_id = file_resp.json()["id"]
    resp = client.post(
        "/v1/batches",
        json={"input_file_id": file_id, "endpoint": ENDPOINT, "completion_window": "24h"},
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _dispatcher_for(
    app, settings: Settings, *, nodes: list[NodeConfig], fake_apps: dict[str, object]
) -> Dispatcher:
    """Builds a Dispatcher sharing the app's real Database (so it sees
    the batches/tasks created via the `client` fixture) but with node
    config supplied per-test, and each node's HTTP traffic routed
    in-process to a fake llama-server app via httpx.ASGITransport."""
    dispatcher_settings = dataclasses.replace(settings, nodes=nodes)

    def client_factory(base_url: str) -> LlamaClient:
        fake_app = fake_apps[base_url]
        transport = httpx.ASGITransport(app=fake_app)
        return LlamaClient(base_url, transport=transport)

    return Dispatcher(app.state.db, dispatcher_settings, client_factory=client_factory)


async def test_dispatch_completes_pending_tasks_and_charges_real_usage(
    client: TestClient, admin_headers: dict, app, settings: Settings
):
    user, key = _make_user_and_key(client, admin_headers, "node-happy")
    _grant(client, admin_headers, user["id"], 100_000)
    batch = _submit_batch(client, key, [_batch_line("r1"), _batch_line("r2")])

    fake_app = make_fake_llama_app(prompt_tokens=10, completion_tokens=5)
    dispatcher = _dispatcher_for(
        app, settings, nodes=[NodeConfig(name="a", base_url="http://a.test", parallel_slots=4)],
        fake_apps={"http://a.test": fake_app},
    )
    dispatcher.startup()
    await dispatcher.health_check_all()

    dispatched = await dispatcher.dispatch_once()
    assert dispatched == 2

    final = client.get(f"/v1/batches/{batch['id']}", headers=_auth(key)).json()
    assert final["status"] == "completed"
    assert final["request_counts"] == {"total": 2, "completed": 2, "failed": 0}
    assert final["x_tokens"]["consumed"] == (10 + 5) * 2

    output = client.get(f"/v1/files/{final['output_file_id']}/content", headers=_auth(key)).content
    records = [json.loads(line) for line in output.decode().splitlines()]
    assert {r["custom_id"] for r in records} == {"r1", "r2"}
    for r in records:
        assert r["response"]["body"]["choices"][0]["message"]["content"].startswith("echo: Hello!")

    budget = client.get("/v1/budget", headers=_auth(key)).json()
    assert budget["reserved_tokens"] == 0
    assert budget["used_tokens"] == 30


async def test_dispatch_respects_node_capacity(client: TestClient, admin_headers: dict, app, settings):
    user, key = _make_user_and_key(client, admin_headers, "node-capacity")
    _grant(client, admin_headers, user["id"], 100_000)
    _submit_batch(client, key, [_batch_line("r1"), _batch_line("r2"), _batch_line("r3")])

    fake_app = make_fake_llama_app()
    dispatcher = _dispatcher_for(
        app, settings, nodes=[NodeConfig(name="a", base_url="http://a.test", parallel_slots=1)],
        fake_apps={"http://a.test": fake_app},
    )
    dispatcher.startup()
    await dispatcher.health_check_all()

    dispatched = await dispatcher.dispatch_once()
    assert dispatched == 1  # only one slot available

    with app.state.db.session_scope() as session:
        statuses = sorted(t.status for t in session.query(Task).all())
    assert statuses == [TaskStatus.COMPLETED, TaskStatus.PENDING, TaskStatus.PENDING]


async def test_dispatch_retries_transient_failure_then_succeeds(
    client: TestClient, admin_headers: dict, app, settings
):
    user, key = _make_user_and_key(client, admin_headers, "node-retry")
    _grant(client, admin_headers, user["id"], 100_000)
    batch = _submit_batch(client, key, [_batch_line("r1")])

    fake_app = make_fake_llama_app(fail_first_n_calls=1)
    dispatcher = _dispatcher_for(
        app, settings, nodes=[NodeConfig(name="a", base_url="http://a.test", parallel_slots=4)],
        fake_apps={"http://a.test": fake_app},
    )
    dispatcher.startup()
    await dispatcher.health_check_all()
    await dispatcher.dispatch_once()

    final = client.get(f"/v1/batches/{batch['id']}", headers=_auth(key)).json()
    assert final["status"] == "completed"
    assert final["request_counts"]["completed"] == 1
    assert fake_app.state.call_count() == 2  # one failure, one successful retry

    with app.state.db.session_scope() as session:
        task = session.query(Task).one()
        assert task.attempts == 1  # attempts counts dispatch claims, not per-attempt HTTP calls


async def test_dispatch_fails_task_after_exhausting_retries(
    client: TestClient, admin_headers: dict, app, settings
):
    user, key = _make_user_and_key(client, admin_headers, "node-exhaust")
    _grant(client, admin_headers, user["id"], 100_000)
    batch = _submit_batch(client, key, [_batch_line("r1")])

    fake_app = make_fake_llama_app(always_fail=True)
    node_settings = dataclasses.replace(
        settings, nodes=[NodeConfig(name="a", base_url="http://a.test", parallel_slots=4)]
    )
    node_settings = dataclasses.replace(node_settings, task_max_attempts=2)
    dispatcher = Dispatcher(
        app.state.db,
        node_settings,
        client_factory=lambda base_url: LlamaClient(
            base_url, transport=httpx.ASGITransport(app=fake_app)
        ),
    )
    dispatcher.startup()
    await dispatcher.health_check_all()
    await dispatcher.dispatch_once()

    final = client.get(f"/v1/batches/{batch['id']}", headers=_auth(key)).json()
    assert final["status"] == "completed"  # batch finalizes even though its one task failed
    assert final["request_counts"] == {"total": 1, "completed": 0, "failed": 1}
    assert final["error_file_id"] is not None
    assert fake_app.state.call_count() == 2  # task_max_attempts=2

    error_content = client.get(f"/v1/files/{final['error_file_id']}/content", headers=_auth(key)).content
    error_records = [json.loads(line) for line in error_content.decode().splitlines()]
    assert error_records[0]["custom_id"] == "r1"
    assert "failed after 2 attempt" in error_records[0]["error"]["message"]

    # A failed task's whole reservation is released, not charged: the
    # student's full grant is available again.
    budget_after = client.get("/v1/budget", headers=_auth(key)).json()
    assert budget_after["reserved_tokens"] == 0
    assert budget_after["used_tokens"] == 0
    assert budget_after["available_tokens"] == 100_000


async def test_health_check_ejects_unhealthy_node_after_threshold(app, settings):
    fake_app = make_fake_llama_app(healthy=False)
    node_settings = dataclasses.replace(
        settings, nodes=[NodeConfig(name="a", base_url="http://a.test", parallel_slots=4)],
        node_unhealthy_threshold=2,
    )
    dispatcher = Dispatcher(
        app.state.db,
        node_settings,
        client_factory=lambda base_url: LlamaClient(
            base_url, transport=httpx.ASGITransport(app=fake_app)
        ),
    )
    dispatcher.startup()

    await dispatcher.health_check_all()
    with app.state.db.session_scope() as session:
        node = session.get(Node, "a")
        assert node.health == NodeHealth.HEALTHY  # 1st failure: not ejected yet
        assert node.consecutive_failures == 1

    await dispatcher.health_check_all()
    with app.state.db.session_scope() as session:
        node = session.get(Node, "a")
        assert node.health == NodeHealth.UNHEALTHY  # 2nd failure hits the threshold
        assert node.consecutive_failures == 2


async def test_dispatch_skips_unhealthy_nodes(client: TestClient, admin_headers: dict, app, settings):
    user, key = _make_user_and_key(client, admin_headers, "node-unhealthy")
    _grant(client, admin_headers, user["id"], 100_000)
    _submit_batch(client, key, [_batch_line("r1")])

    fake_app = make_fake_llama_app(healthy=False)
    node_settings = dataclasses.replace(
        settings, nodes=[NodeConfig(name="a", base_url="http://a.test", parallel_slots=4)],
        node_unhealthy_threshold=1,
    )
    dispatcher = Dispatcher(
        app.state.db,
        node_settings,
        client_factory=lambda base_url: LlamaClient(
            base_url, transport=httpx.ASGITransport(app=fake_app)
        ),
    )
    dispatcher.startup()
    await dispatcher.health_check_all()  # one failure hits threshold=1, node ejected

    dispatched = await dispatcher.dispatch_once()
    assert dispatched == 0

    with app.state.db.session_scope() as session:
        task = session.query(Task).one()
        assert task.status == TaskStatus.PENDING


async def test_fair_share_round_robins_across_users(client: TestClient, admin_headers: dict, app, settings):
    user_a, key_a = _make_user_and_key(client, admin_headers, "fair-a")
    user_b, key_b = _make_user_and_key(client, admin_headers, "fair-b")
    _grant(client, admin_headers, user_a["id"], 100_000)
    _grant(client, admin_headers, user_b["id"], 100_000)

    # A submits a big batch first; B submits a small batch right after.
    _submit_batch(client, key_a, [_batch_line(f"a{i}") for i in range(6)])
    _submit_batch(client, key_b, [_batch_line("b1"), _batch_line("b2")])

    fake_app = make_fake_llama_app()
    dispatcher = _dispatcher_for(
        app, settings, nodes=[NodeConfig(name="a", base_url="http://a.test", parallel_slots=4)],
        fake_apps={"http://a.test": fake_app},
    )
    dispatcher.startup()
    await dispatcher.health_check_all()

    # Only 4 slots for 8 pending tasks: fair-share should give B (who only
    # has 2 requests total) both of theirs rather than A monopolizing all 4.
    dispatched = await dispatcher.dispatch_once()
    assert dispatched == 4

    with app.state.db.session_scope() as session:
        completed_custom_ids = {
            t.custom_id for t in session.query(Task).filter(Task.status == TaskStatus.COMPLETED)
        }
    assert completed_custom_ids == {"b1", "b2", "a0", "a1"}


async def test_recover_running_tasks_resets_them_to_pending(app):
    with app.state.db.session_scope() as session:
        from batchsvc.models import Batch, FileObject, FilePurpose, User

        user = User(username="crash-victim")
        session.add(user)
        session.flush()
        input_file = FileObject(
            id="file_crashtest",
            user_id=user.id,
            purpose=FilePurpose.BATCH_INPUT,
            filename="input.jsonl",
            path="/dev/null",
            bytes=0,
        )
        session.add(input_file)
        session.flush()
        batch = Batch(
            id="batch_crashtest",
            user_id=user.id,
            input_file_id=input_file.id,
            request_total=1,
        )
        session.add(batch)
        session.add(
            Task(
                batch_id=batch.id,
                line_index=0,
                custom_id="r1",
                request_body={"messages": []},
                status=TaskStatus.RUNNING,
                node_name="some-node-that-crashed",
            )
        )
        session.commit()

    dispatcher = Dispatcher(app.state.db, dataclasses.replace(app.state.settings, nodes=[]))
    recovered = dispatcher.recover_running_tasks()
    assert recovered == 1

    with app.state.db.session_scope() as session:
        task = session.query(Task).filter(Task.custom_id == "r1").one()
        assert task.status == TaskStatus.PENDING
        assert task.node_name is None
