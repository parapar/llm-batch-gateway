# Batch Inference Service

A batch inference gateway for `llama.cpp` (`llama-server`) nodes, speaking
the OpenAI Batch API. Built for a lab of slow inference machines (AMD
Strix Halo) shared by students with per-student token budgets.

See [`docs/PLAN.md`](docs/PLAN.md) for the full design and milestone plan.
This README covers what's implemented so far (**M0 + M1 + M2 + M3**:
project skeleton, accounting, the OpenAI Batch API surface, and the
dispatcher that actually runs inference) and how to run it.

## What's here (M0 + M1 + M2 + M3)

- FastAPI app with SQLite (WAL mode) storage.
- Full data model for users, API keys, budgets, ledger entries, files,
  batches, tasks, and nodes.
- **Token budget accounting** (`batchsvc/ledger.py`): grant / reserve /
  release / charge / adjust, all append-only via `ledger_entries`, with
  the materialized `budgets` row always reconstructable from history
  (`ledger.recompute_budget`, exposed as `POST
  /admin/users/{id}/budget/reconcile`).
- Admin API (`/admin/*`, bearer admin token) to create students, issue/
  revoke API keys, grant budget, and inspect the ledger.
- Student-facing auth (bearer `sk-...` API key) and `GET /v1/budget` so a
  student can check their own remaining tokens.
- **OpenAI-compatible Batch API** (`/v1/files`, `/v1/batches`): upload a
  JSONL input file, submit a batch, poll status, list, cancel, download
  results — the official `openai` SDK works against this unmodified with
  `base_url=".../v1"`. Submission reserves worst-case tokens (measured
  prompt + each line's `max_tokens`) atomically against the student's
  budget before anything is created; malformed input files or an
  unsupported endpoint are rejected synchronously with no partial state
  left behind.
- **Dispatcher** (`batchsvc/dispatcher.py`): a background asyncio loop
  (started automatically when `nodes` is non-empty in config) that
  claims pending tasks, fair-share round-robins them across users, load
  balances across healthy nodes by in-flight count (never trusting a
  node's own `/slots`), retries transient failures with backoff, and
  ejects a node from rotation after consecutive `/health` failures.
  Every task it completes or fails settles through the same
  `batch_ops.complete_task`/`fail_task` pipeline M2 already built, so
  batches finalize (write `output.jsonl`/`error.jsonl`, charge/release
  real token usage) exactly the same way whether driven by the
  dispatcher or, as in some tests, called directly. A stuck `RUNNING`
  task from a previous crash is reset to `PENDING` on startup.
- Admin CLI: `batchsvc-admin create-user|create-key|grant|list-users`.

Not yet implemented: ETA estimation (M4) and the retention/cleanup job,
expiry enforcement, and `/metrics` (M5). Everything else in the plan is
live and exercised end to end (see `tests/test_dispatcher.py`, and it's
been verified over real HTTP sockets against a stand-in llama-server,
not just in-process).

## Setup

Requires Python 3.11+. Using [uv](https://docs.astral.sh/uv/):

```bash
uv venv
uv pip install -e ".[dev]"
```

Copy the example config and set an admin token:

```bash
cp config/config.example.yaml config/config.yaml
# edit config.yaml, or just override at runtime:
export BATCHSVC_ADMIN_TOKEN="pick-a-real-secret"
```

To actually run inference, add your llama-server node(s) under `nodes:`
in `config.yaml` (see the comments there for the dispatcher's other
tunables -- retry attempts, health check interval, etc.). Leave `nodes`
empty to run API-only (submitted batches just sit at `in_progress`
forever with nothing to drive them — useful for exercising the API
surface without a GPU/node available).

## Running

```bash
source .venv/bin/activate
uvicorn batchsvc.main:create_app --factory --reload
```

(The app is exposed as a factory, not a module-level `app`, so importing
`batchsvc.main` — from tests or the CLI — never has side effects on the
configured database/blob paths.)

## Admin CLI

```bash
batchsvc-admin create-user alice --full-name "Alice A." --with-key --grant 100000
batchsvc-admin grant alice 50000 --note "midterm top-up"
batchsvc-admin list-users
```

## Admin API

All `/admin/*` routes require `Authorization: Bearer <admin_token>`.

```bash
curl -s localhost:8000/admin/users -H "Authorization: Bearer $BATCHSVC_ADMIN_TOKEN"

curl -s -X POST localhost:8000/admin/users \
  -H "Authorization: Bearer $BATCHSVC_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"username": "alice", "full_name": "Alice A."}'

curl -s -X POST localhost:8000/admin/users/<user_id>/budget/grant \
  -H "Authorization: Bearer $BATCHSVC_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"tokens": 100000, "note": "semester allowance"}'
```

## Student-facing endpoints

```bash
curl -s localhost:8000/v1/budget -H "Authorization: Bearer sk-..."

# Upload a batch input file (JSONL, one request per line, OpenAI's shape).
curl -s -X POST localhost:8000/v1/files -H "Authorization: Bearer sk-..." \
  -F purpose=batch -F file=@input.jsonl

# Submit the batch.
curl -s -X POST localhost:8000/v1/batches -H "Authorization: Bearer sk-..." \
  -H "Content-Type: application/json" \
  -d '{"input_file_id": "file_...", "endpoint": "/v1/chat/completions", "completion_window": "24h"}'

# Poll status.
curl -s localhost:8000/v1/batches/batch_... -H "Authorization: Bearer sk-..."

# Once status is "completed", download results the same way as any file.
curl -s localhost:8000/v1/files/file_.../content -H "Authorization: Bearer sk-..."

# Cancel a batch that's still validating/in_progress.
curl -s -X POST localhost:8000/v1/batches/batch_.../cancel -H "Authorization: Bearer sk-..."
```

Or with the official SDK, unmodified:

```python
from openai import OpenAI

client = OpenAI(api_key="sk-...", base_url="http://localhost:8000/v1")
f = client.files.create(file=open("input.jsonl", "rb"), purpose="batch")
batch = client.batches.create(
    input_file_id=f.id, endpoint="/v1/chat/completions", completion_window="24h"
)
```

Each JSONL line follows OpenAI's batch input shape; `model` is accepted
but ignored (this server always serves the one model it's configured
with):

```json
{"custom_id": "req-1", "method": "POST", "url": "/v1/chat/completions", "body": {"messages": [{"role": "user", "content": "..."}], "max_tokens": 200}}
```

## Tests

```bash
pytest -q       # 54 tests: ledger, admin API, auth, files/batches, lifecycle, dispatcher
ruff check .
```

Dispatcher tests run against a fake llama-server (`tests/fake_llama_node.py`,
an in-process FastAPI app reached via `httpx.ASGITransport` -- no real
sockets, no real model) so they're fast and deterministic while still
exercising the real HTTP client and JSON wire format.

## Layout

```
src/batchsvc/
  config.py       settings (YAML + env)
  db.py           SQLite/WAL engine + session management
  models.py       SQLAlchemy ORM models (all tables from docs/PLAN.md)
  ledger.py       token budget accounting (grant/reserve/release/charge)
  tokens.py       heuristic token estimator for upfront reservation
  blobs.py        on-disk storage for file/batch JSONL blobs
  batch_ops.py    JSONL validation, batch submit/cancel, task completion + finalization
  llama_client.py thin async HTTP client for one llama-server node
  dispatcher.py   claims/load-balances/retries tasks across nodes; health checks; crash recovery
  security.py     API key generation/hashing
  errors.py       OpenAI-shaped error envelope
  deps.py         FastAPI auth/DB dependencies
  schemas.py      pydantic request/response models
  routers/
    admin.py      /admin/* (users, keys, budget, ledger)
    misc.py       /healthz, /v1/budget
    files.py      /v1/files (upload, metadata, content)
    batches.py    /v1/batches (submit, status, list, cancel)
  main.py         app factory (starts the dispatcher as a lifespan-managed background task)
  cli.py          batchsvc-admin CLI
tests/            pytest suite (fixtures in conftest.py; fake_llama_node.py for dispatcher tests)
config/           config.example.yaml
docs/PLAN.md      full design + milestone plan
```
