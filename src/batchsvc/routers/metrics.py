"""GET /metrics: Prometheus text-format operational metrics.

Admin-token protected (like /admin/*) -- point a Prometheus scrape
config at it with `bearer_token: <admin_token>` (or `authorization:
{credentials: <admin_token>}` in newer Prometheus versions).

Two kinds of series, both registered against prometheus_client's
default global registry:

- Counters (batchsvc_http_requests_total) accumulate across the
  process's lifetime, incremented inline by main.py's request-logging
  middleware as requests happen.
- Gauges (everything else) reflect current state and are refreshed from
  a cheap local SQLite query/dispatcher snapshot right before each
  scrape, rather than being kept continuously up to date -- there's
  only ever one scraper reading them, so "compute on read" is simpler
  and cannot drift.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from sqlalchemy import func
from sqlalchemy.orm import Session

from batchsvc.deps import get_db, require_admin
from batchsvc.models import Batch, BatchStatus, DispatchStat, Node, NodeHealth, Task, TaskStatus

router = APIRouter(tags=["metrics"], dependencies=[Depends(require_admin)])

HTTP_REQUESTS_TOTAL = Counter(
    "batchsvc_http_requests_total", "HTTP requests handled", ["method", "path", "status"]
)

BATCHES_BY_STATUS = Gauge("batchsvc_batches", "Batches currently in each status", ["status"])
TASKS_BY_STATUS = Gauge("batchsvc_tasks", "Tasks currently in each status", ["status"])
NODE_HEALTHY = Gauge("batchsvc_node_healthy", "1 if the node is healthy, else 0", ["node"])
NODE_IN_FLIGHT = Gauge("batchsvc_node_in_flight_tasks", "Tasks currently dispatched to this node", ["node"])
NODE_PARALLEL_SLOTS = Gauge("batchsvc_node_parallel_slots", "Configured concurrency for this node", ["node"])
CLUSTER_TOKENS_PER_SECOND = Gauge(
    "batchsvc_cluster_tokens_per_second", "Rolling per-slot throughput EWMA feeding the ETA model"
)
CLUSTER_SAMPLE_COUNT = Gauge(
    "batchsvc_cluster_throughput_samples", "Completed-task samples the throughput EWMA is based on"
)


def _refresh_gauges(db: Session, request: Request) -> None:
    batch_counts = dict(db.query(Batch.status, func.count()).group_by(Batch.status).all())
    for status in BatchStatus:
        BATCHES_BY_STATUS.labels(status=status.value).set(batch_counts.get(status, 0))

    task_counts = dict(db.query(Task.status, func.count()).group_by(Task.status).all())
    for status in TaskStatus:
        TASKS_BY_STATUS.labels(status=status.value).set(task_counts.get(status, 0))

    dispatcher = request.app.state.dispatcher
    in_flight = dispatcher.in_flight_snapshot() if dispatcher is not None else {}
    for node in db.query(Node).all():
        NODE_HEALTHY.labels(node=node.name).set(1 if node.health == NodeHealth.HEALTHY else 0)
        NODE_IN_FLIGHT.labels(node=node.name).set(in_flight.get(node.name, 0))
        NODE_PARALLEL_SLOTS.labels(node=node.name).set(node.parallel_slots)

    stat = db.get(DispatchStat, "global")
    CLUSTER_TOKENS_PER_SECOND.set(stat.tokens_per_second_ewma or 0.0 if stat else 0.0)
    CLUSTER_SAMPLE_COUNT.set(stat.sample_count if stat else 0)


@router.get("/metrics")
def metrics(request: Request, db: Session = Depends(get_db)) -> Response:
    _refresh_gauges(db, request)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
