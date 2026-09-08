# Batch Inference Service

A batch inference gateway for `llama.cpp` (`llama-server`) nodes, speaking
the OpenAI Batch API. Built for a lab of slow inference machines (AMD
Strix Halo) shared by students with per-student token budgets.

See [`docs/PLAN.md`](docs/PLAN.md) for the full design and milestone plan.
The plan (**M0 through M5**) is fully implemented, plus a **student web
portal** with LDAP login on top. This README covers what's here and how
to run it; see also [`docs/QUICKSTART.md`](docs/QUICKSTART.md) (for
students) and [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)
(Docker/systemd).

## What's here

- FastAPI app with SQLite (WAL mode, `busy_timeout=20000` so concurrent
  writers queue for the lock instead of failing outright) storage. Every
  recurring background write (dispatcher, retention job) runs via
  `asyncio.to_thread` rather than directly on the event loop, so a
  writer waiting on that lock -- or on `ledger.py`'s own
  `threading.Lock` -- costs one worker thread, not the whole server.
  Both issues were found by `scripts/load_test.py` under real concurrent
  load; see `docs/PLAN.md` for the detail.
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
- **ETA estimation** (`batchsvc/eta.py`): every batch status response
  carries an `x_eta` block (`estimated_seconds_remaining`,
  `estimated_completion_at`, `queue_position`, `confidence`). Built from
  a rolling EWMA of real cluster throughput and expected (not
  worst-case) output length, fed by every task the dispatcher actually
  completes; before enough samples exist (or with no dispatcher
  running) it falls back to conservative bootstrap constants and flags
  `confidence: "low"`. `queue_position` approximates the dispatcher's
  fair-share ordering as simple FIFO-by-batch-submission-time, which is
  close enough for a rough estimate without replaying the exact
  round-robin on every status request.
- **Retention job** (`batchsvc/retention.py`): runs unconditionally
  (unlike the dispatcher, it doesn't need nodes configured) as a
  lifespan-managed background loop. Expires batches that outlive their
  `completion_window` (releasing whatever budget was still reserved,
  same effect as a student cancelling), and deletes output/error files
  -- from disk and the DB -- once a finished batch is older than
  `result_retention_days`. Both passes are idempotent and safe to
  re-run.
- **Structured logging** (`batchsvc/logging_setup.py`): JSON lines to
  stdout by default, one object per record; a request-logging
  middleware logs every HTTP call (method, route template, status,
  duration).
- **`GET /metrics`** (admin-token protected, like `/admin/*`):
  Prometheus text format -- request counts, batch/task counts by
  status, per-node health/in-flight/capacity, and the cluster
  throughput EWMA feeding the ETA model.
- **Student portal** (`/portal`): a small server-rendered web app where
  students sign in with their **LDAP** directory credentials and see
  their token budget, what each job cost them, and their API key.
  Supports both direct-bind and service-account search-then-bind, an
  optional required-group gate, and auto-provisioning (first successful
  login creates the account with a configurable default grant). Keys
  stay hash-only in the database: the portal shows the prefix, and
  "generate a new key" reveals the new one exactly once while revoking
  the old. See `batchsvc/ldap_auth.py` and `batchsvc/portal/`.
- Admin CLI: `batchsvc-admin create-user|create-key|grant|list-users`.
- **Deployment**: a `Dockerfile` and a documented systemd unit
  ([`deploy/batchsvc.service`](deploy/batchsvc.service)) -- see
  [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).
- **Load test** ([`scripts/load_test.py`](scripts/load_test.py)): spins
  up real stub llama-server node(s) and a real batchsvc server as
  subprocesses, then floods it with concurrent students end to end
  (submit → poll → download) and reports throughput/latency. Not part
  of the pytest suite (it's slow by design); run it manually.

Everything in the plan is live and exercised end to end -- see
`tests/test_dispatcher.py`, `tests/test_eta.py`, `tests/test_retention.py`
and `tests/test_metrics.py` -- and the core submit-through-dispatch flow
has additionally been verified over real HTTP sockets against a
stand-in llama-server (both ad hoc and via `scripts/load_test.py`), not
just in-process.

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

## Student portal (LDAP)

To let students sign in and see their own budget, fill in the `portal:`
and `ldap:` sections of `config.yaml` (every option is commented in
`config/config.example.yaml`) and set a session secret:

```bash
export BATCHSVC_PORTAL_SECRET="$(openssl rand -hex 32)"
# only for bind_mode: search
export BATCHSVC_LDAP_SERVICE_PASSWORD="..."
```

The portal is then at `/portal`. Both bind modes are supported:

```yaml
ldap:
  enabled: true
  server_uri: "ldaps://ldap.internal.example.edu:636"
  use_ssl: true
  bind_mode: "direct"                                        # or "search"
  user_dn_template: "uid={username},ou=people,dc=example,dc=edu"
  required_group_dn: "cn=llm-course,ou=groups,dc=example,dc=edu"   # optional
```

Notes worth knowing before you point this at a real directory:

- **Serve it over HTTPS.** `cookie_secure: true` (the default) means
  session cookies are only sent over TLS, so the portal simply won't
  keep you logged in over plain `http://`. Students type real
  university passwords into this form.
- Certificates are **verified** for `use_ssl`/`start_tls`; set
  `ca_certs_file` if your directory uses an internal CA.
- With `auto_provision: true` (the default) any student who can bind
  gets an account and `default_grant_tokens` on first login. Use
  `required_group_dn` to scope that to enrolled students, or turn
  auto-provisioning off and pre-create accounts with `batchsvc-admin`.
- The portal never learns or stores a student's directory password: it
  is only ever used for a bind against your server.

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

# Poll status -- includes x_tokens (budget impact) and x_eta (rough
# time remaining, once a dispatcher is configured and running).
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
pytest -q       # ledger, admin API, auth, files/batches, lifecycle, dispatcher, ETA, retention, metrics
ruff check .
```

Dispatcher tests run against a fake llama-server (`tests/fake_llama_node.py`,
an in-process FastAPI app reached via `httpx.ASGITransport` -- no real
sockets, no real model) so they're fast and deterministic while still
exercising the real HTTP client and JSON wire format. For a slower,
real-sockets end-to-end check (including real subprocess servers and
realistic latency), see `scripts/load_test.py`:

```bash
python scripts/load_test.py --students 10 --lines-per-batch 10
```

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
  eta.py          rolling throughput EWMA + per-batch remaining-time estimate
  retention.py    batch expiry + result-file purging (background job)
  logging_setup.py  structured (JSON) logging configuration
  security.py     API key generation/hashing
  errors.py       OpenAI-shaped error envelope
  deps.py         FastAPI auth/DB dependencies
  schemas.py      pydantic request/response models
  routers/
    admin.py      /admin/* (users, keys, budget, ledger)
    misc.py       /healthz, /v1/budget
    files.py      /v1/files (upload, metadata, content)
    batches.py    /v1/batches (submit, status, list, cancel)
    metrics.py    /metrics (Prometheus text format, admin-token protected)
  ldap_auth.py    LDAP bind/search + group check for the student portal
  portal/
    routes.py     /portal (login, dashboard, API key rotation)
    service.py    provisioning, dashboard figures, key rotation
    session.py    signed-cookie sessions + CSRF tokens
    templates/    server-rendered HTML (no build step)
  main.py         app factory (dispatcher + retention job as lifespan-managed background tasks)
  cli.py          batchsvc-admin CLI
tests/            pytest suite (fixtures in conftest.py; fake_llama_node.py for dispatcher tests)
scripts/
  stub_llama_node.py  standalone stub node for load testing (real subprocess, real latency)
  load_test.py    end-to-end load test orchestrator (not part of pytest -- run manually)
config/           config.example.yaml
deploy/
  batchsvc.service  systemd unit (install steps in its own comments)
Dockerfile        container build
docs/
  PLAN.md         full design + milestone plan
  QUICKSTART.md   student-facing guide (portal, submit a batch, check status, download results)
  DEPLOYMENT.md   Docker / systemd deployment guide
```
