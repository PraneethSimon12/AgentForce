# Single image for both the API and the Celery workers. They differ only by command
# (see docker-compose.yml), which keeps the runtime identical on both sides — a tool
# must behave the same whether it runs INLINE in the API or DURABLE in a worker.

FROM python:3.12-slim

# Build tools are needed for some wheels; removed in the same layer to keep the image small.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# Dependency layer first: application code changes far more often than dependencies,
# so this ordering keeps the (slow, torch-carrying) install cached across code edits.
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -e ".[dev]"

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus

RUN mkdir -p /tmp/prometheus

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
