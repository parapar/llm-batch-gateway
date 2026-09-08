# Llama Batch Inference Service — Implementation Plan

## Context

Two AMD Strix Halo machines run `llama.cpp`'s `llama-server`, each serving a
single model. Inference is slow, so requests are batched (OpenAI Batch API
shape), queued, and dispatched across both machines with load balancing.
Students authenticate with API keys and have a combined input+output token
budget; requests that would exceed the remaining budget are rejected before
any work is queued. Accounting is durable (SQLite, WAL mode) and auditable.

## Decisions locked in

- **Stack**: Python + FastAPI + SQLAlchemy + SQLite (WAL). Embedded, ACID,
  single-file, trivial to back up — fills the role HSQLDB would have played,
  without the JVM ceremony.
- **API shape**: Full OpenAI Batch API flow — `POST /v1/files` (JSONL
  upload) → `POST /v1/batches` (`input_file_id`) → `GET /v1/batches/{id}`
  → `GET /v1/files/{output_file_id}/content`. Students point the official
  `openai` SDK at `base_url=".../v1"` and it works unmodified.
- **Budget enforcement**: reserve worst-case upfront (measured prompt
  tokens + each line's `max_tokens`), reject the *whole* batch atomically
  if it doesn't fit, release the unused reservation as each task actually
  completes and is charged its real usage. Budget can never be overrun.
- **Inference type supported**: `/v1/chat/completions` lines only (matches
  what `llama-server` and coursework actually use). `/v1/embeddings` and
  `/v1/completions` are explicitly out of scope for the first cut but the
  task model doesn't preclude adding them later.
- **Model parameter**: ignored/overwritten server-side — each deployment
  serves exactly one model per node pool.

## Architecture

```
                 students (openai SDK)
                          │  Bearer sk-…
                    ┌─────▼──────┐
                    │  FastAPI   │  /v1/files  /v1/batches  /admin
                    │  API layer │  auth → validate → reserve budget
                    └─────┬──────┘
              ┌───────────┼───────────┐
        ┌─────▼─────┐ ┌───▼────┐ ┌────▼─────┐
        │  SQLite   │ │ blob   │ │ async    │
        │  (WAL)    │ │ store  │ │ dispatch │
        │ users     │ │ *.jsonl│ │  loop    │
        │ budgets   │ └────────┘ └────┬─────┘
        │ ledger    │                 │ least-outstanding + health
        │ batches   │          ┌──────┴──────┐
        │ tasks     │      ┌───▼───┐     ┌───▼───┐
        │ nodes     │      │halo-1 │     │halo-2 │  llama-server -np 4
        └───────────┘      └───────┘     └───────┘
```

One dispatcher process owns all scheduling; the API layer only enqueues.
That keeps token accounting and node-capacity bookkeeping in a single place
with no distributed locking required.

## Data model

| table            | purpose                                                             |
|-------------------|----------------------------------------------------------------------|
| `users`           | student identity, active flag                                       |
| `api_keys`        | `sk-` prefix + sha256 hash, revocable, many per user                 |
| `budgets`         | granted / used / **reserved** tokens per user                        |
| `ledger_entries`  | append-only audit: grant, reserve, release, charge — budget is always derivable from replay |
| `files`           | uploaded JSONL + generated output/error files, sha256, on-disk path  |
| `batches`         | OpenAI batch object + reserved_tokens, request_counts, timestamps    |
| `tasks`           | **one row per JSONL line** — the unit of scheduling, retry, and accounting |
| `nodes`           | base_url, parallel slots, health, EWMA throughput                    |

Splitting each batch into per-line `tasks` is the key design decision: it
gives per-request retry, cross-node load balancing within a single batch,
exact usage accounting, and live progress for the ETA estimate.

## Endpoints

### OpenAI-compatible (works with `OpenAI(base_url=...)` unmodified)
- `POST /v1/files` (`purpose=batch`) → `file_…`
- `POST /v1/batches` → `batch_<hash>`
- `GET /v1/batches/{id}` — status + progress + ETA
- `GET /v1/batches` — list
- `POST /v1/batches/{id}/cancel`
- `GET /v1/files/{id}/content` → results JSONL (separate error file for
  failed lines)

Status uses OpenAI's vocabulary (`validating → in_progress → finalizing →
completed/failed/cancelled/expired`), with additional non-breaking fields
on the status response:

```json
{"id":"batch_…","status":"in_progress",
 "request_counts":{"total":500,"completed":137,"failed":2},
 "x_queue_position":1,
 "x_estimated_seconds_remaining":1840,
 "x_estimated_completion_at":"2026-09-08T11:42:00Z",
 "x_tokens":{"reserved":420000,"consumed":118430}}
```

### Admin (separate admin key)
Create students, grant/reset budgets, view usage, enable/disable nodes,
queue overview.

## Budget enforcement

At submit, per line: exact prompt tokens via the node's
`/apply-template` + `/tokenize` (falls back to a heuristic if all nodes are
down), plus `max_tokens` as worst-case output (required, or defaulted from
config — without it worst-case is unbounded). Sum → atomic reserve against
`granted - used - reserved`. If it doesn't fit, the whole batch is
rejected and nothing is stored:

```
HTTP 429  {"error":{"code":"insufficient_quota","type":"insufficient_quota",
  "message":"Batch needs 412,300 tokens; 88,120 remaining of 2,000,000."}}
```

As each task finishes, actual usage is charged and the unused part of its
reservation released. A crash or cancel releases the remainder on
recovery, so budget can never be overrun or silently leaked.

## Scheduling & load balancing

- Capacity = configured parallel slots per node (`-np`), tracked as
  in-flight counters in the dispatcher rather than trusting `/slots`.
- Claim order: fair-share round-robin across users, then FIFO by batch,
  then line order — so one student's large batch can't starve the class.
- Placement: least-outstanding among healthy nodes. Health polled via
  `/health`; N consecutive failures ejects a node, its in-flight tasks are
  requeued elsewhere.
- Retries with backoff on 5xx/timeouts; a task that exhausts retries lands
  in the batch's error file rather than failing the whole batch.
- Startup recovery: any `running` task reverts to `pending`.

## ETA model

Rolling cluster throughput (EWMA of tokens/sec, aggregate across nodes,
prompt tokens weighted ~1/10 of generated ones) against remaining work
ahead of the batch in the queue plus its own remainder. Expected output
per task = `min(max_tokens, EWMA of observed completion lengths)`, which
converges quickly and beats using `max_tokens` as the estimate. Bootstrap
with constants in config until enough samples exist; report a coarse
estimate flagged low-confidence during the first runs.

## Milestones

| # | Deliverable |
|---|---|
| **M0** | Repo skeleton: uv/pyproject, FastAPI app, YAML config, SQLite WAL + models, ruff, pytest, stub llama node for tests |
| **M1** | Users, API keys, budgets, ledger, admin CLI + admin API; accounting invariant tests |
| **M2** | `/v1/files` + `/v1/batches` submit/status/list/cancel/results, JSONL validation, budget reservation, OpenAI error envelope |
| **M3** | Node pool, health, dispatcher loop, fair-share claiming, retries, usage charging, batch finalization, crash recovery |
| **M4** | Throughput stats + ETA fields on the status endpoint |
| **M5** | Expiry/limits, structured logging, `/metrics`, student quickstart docs, systemd + Docker, end-to-end load test against stub nodes |

## Working assumptions (flag if wrong)

- Budget is one combined input+output token cap per student, refilled by
  adding grants (no automatic periodic reset).
- Auth is Bearer API keys (`Authorization: Bearer sk-…`).
- Batch results retained 7 days after completion, then purged.
- Two nodes configured in YAML, 4 parallel slots each — adjustable per
  deployment.
