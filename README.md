# Batch Inference Service

A batch inference gateway for `llama.cpp` (`llama-server`) nodes, speaking
the OpenAI Batch API. Built for a lab of slow inference machines (AMD
Strix Halo) shared by students with per-student token budgets.

See [`docs/PLAN.md`](docs/PLAN.md) for the full design and milestone plan.
This README covers what's implemented so far (**M0 + M1**: project
skeleton and accounting) and how to run it.

## What's here (M0 + M1)

- FastAPI app with SQLite (WAL mode) storage.
- Full data model for users, API keys, budgets, ledger entries, and
  schema-stable (but not yet wired up) tables for files/batches/tasks/nodes.
- **Token budget accounting** (`batchsvc/ledger.py`): grant / reserve /
  release / charge / adjust, all append-only via `ledger_entries`, with
  the materialized `budgets` row always reconstructable from history
  (`ledger.recompute_budget`, exposed as `POST
  /admin/users/{id}/budget/reconcile`).
- Admin API (`/admin/*`, bearer admin token) to create students, issue/
  revoke API keys, grant budget, and inspect the ledger.
- Student-facing auth (bearer `sk-...` API key) and `GET /v1/budget` so a
  student can check their own remaining tokens.
- Admin CLI: `batchsvc-admin create-user|create-key|grant|list-users`.

Not yet implemented: `/v1/files`, `/v1/batches` (M2), the dispatcher and
node pool (M3), and ETA estimation (M4).

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

## Student-facing endpoints (so far)

```bash
curl -s localhost:8000/v1/budget -H "Authorization: Bearer sk-..."
```

## Tests

```bash
pytest -q       # 27 tests: ledger invariants, admin API, auth
ruff check .
```

## Layout

```
src/batchsvc/
  config.py       settings (YAML + env)
  db.py           SQLite/WAL engine + session management
  models.py       SQLAlchemy ORM models (all tables from docs/PLAN.md)
  ledger.py       token budget accounting (grant/reserve/release/charge)
  security.py     API key generation/hashing
  errors.py       OpenAI-shaped error envelope
  deps.py         FastAPI auth/DB dependencies
  schemas.py      pydantic request/response models
  routers/
    admin.py      /admin/* (users, keys, budget, ledger)
    misc.py       /healthz, /v1/budget
  main.py         app factory
  cli.py          batchsvc-admin CLI
tests/            pytest suite (fixtures in conftest.py)
config/           config.example.yaml
docs/PLAN.md      full design + milestone plan
```
