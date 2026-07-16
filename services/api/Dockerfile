# Stage 1: Builder
FROM python:3.12-slim AS builder
WORKDIR /app

# libpq-dev for psycopg2 (DatabaseManager).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev && \
    rm -rf /var/lib/apt/lists/*

RUN pip install poetry poetry-plugin-export
COPY pyproject.toml poetry.lock ./
RUN poetry export -f requirements.txt --output requirements.txt --without-hashes --only main

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2: Runtime
FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends libpq5 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH=/app

COPY shared ./shared
COPY services/api ./services/api

# Opret bruger af sikkerhedshensyn
RUN useradd -m agentuser && chown -R agentuser:agentuser /opt/venv /app
USER agentuser

EXPOSE 8000

CMD ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
