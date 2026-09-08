# Deployment

Two supported paths: a container (Docker) or a systemd unit running
directly on a host. Either way, the service needs:

- A writable directory for `data/` (SQLite DB + file blobs).
- A `config.yaml` (copy `config/config.example.yaml` and edit) --
  notably `admin_token` (prefer `BATCHSVC_ADMIN_TOKEN` env var instead
  of putting it in the file) and `nodes:` (your llama-server machines).
- Network access to those llama-server nodes' HTTP ports.

The service itself is stateless compute + one SQLite file -- it can run
on either of the two inference boxes themselves or on a separate small
machine; it doesn't need much CPU/RAM of its own.

## Docker

```bash
docker build -t batchsvc .

mkdir -p config data
cp config/config.example.yaml config/config.yaml
$EDITOR config/config.yaml   # set nodes:, etc.

docker run -d \
  --name batchsvc \
  -p 8000:8000 \
  -e BATCHSVC_ADMIN_TOKEN="$(openssl rand -hex 32)" \
  -v "$(pwd)/config:/app/config" \
  -v "$(pwd)/data:/app/data" \
  batchsvc
```

The container runs as a non-root user (uid 1000) and expects
`/app/config` and `/app/data` to be writable by it -- if you bind-mount
host directories, make sure their ownership allows that (e.g. `chown
1000:1000 data config` on the host, or run the container with
`--user $(id -u):$(id -g)` and matching host permissions).

Logs go to stdout as JSON lines (`docker logs -f batchsvc`); redirect
those to whatever log aggregation you already use.

## systemd (bare host)

See [`deploy/batchsvc.service`](../deploy/batchsvc.service) -- the
comments at the top of that file are the full install sequence
(create a service user, clone, `uv venv` + `uv pip install -e .`,
copy the unit file, `systemctl enable --now`). Logs land in the
journal (`journalctl -u batchsvc -f`), still as JSON lines.

## Either way, after it's running

```bash
curl -s http://<host>:8000/healthz
batchsvc-admin create-user alice --with-key --grant 100000   # run on the host/in the container
```

Point Prometheus at `GET /metrics` with the admin token as a bearer
credential if you want the operational gauges/counters (see
`batchsvc/routers/metrics.py`); it's the same auth as `/admin/*`.

## Upgrading

This project has no migration tooling (no Alembic) -- schema changes
between versions are applied via `Base.metadata.create_all`, which only
creates *missing* tables/columns, not `ALTER TABLE`s on existing ones.
In practice: back up `data/batchsvc.db` before upgrading, and if a
release note mentions a schema change, expect to need a fresh database
(export what you need from the old one via SQL first) rather than an
in-place upgrade.
