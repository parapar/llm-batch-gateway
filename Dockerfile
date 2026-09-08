# Batch inference gateway. Build: docker build -t batchsvc .
# Run:   docker run -p 8000:8000 -v $(pwd)/config:/app/config -v batchsvc-data:/app/data batchsvc

FROM python:3.11-slim

RUN pip install --no-cache-dir uv==0.5.*

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN uv venv /opt/venv && \
    . /opt/venv/bin/activate && \
    uv pip install --no-cache -e .
ENV PATH="/opt/venv/bin:$PATH"

COPY config/config.example.yaml ./config/config.example.yaml

RUN useradd --create-home --uid 1000 batchsvc && \
    mkdir -p /app/data /app/config && \
    chown -R batchsvc:batchsvc /app
USER batchsvc

# No default admin token or nodes baked in -- mount a real config.yaml
# (see config/config.example.yaml) or set env vars at `docker run` time.
ENV BATCHSVC_CONFIG=/app/config/config.yaml
EXPOSE 8000

CMD ["uvicorn", "batchsvc.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
