# Stage 1: Builder
FROM python:3.12-slim AS builder
WORKDIR /app

# Installer dependencies nødvendige for at bygge hjul (som psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev && \
    rm -rf /var/lib/apt/lists/*

RUN pip install poetry
COPY pyproject.toml poetry.lock* ./
RUN poetry export -f requirements.txt --output requirements.txt --without-hashes
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Runtime
FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends libpq5 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
ENV PYTHONPATH=/app

# Opret bruger af sikkerhedshensyn
RUN useradd -m agentuser
USER agentuser

CMD ["python", "services/ingestor/main.py"]