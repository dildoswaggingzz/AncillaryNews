# Stage 1: Builder
FROM python:3.12-slim AS builder
WORKDIR /app

# lxml (a trafilatura dependency) needs build tooling + headers to build from source.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libxml2-dev libxslt1-dev && \
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

RUN apt-get update && apt-get install -y --no-install-recommends libxml2 libxslt1.1 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH=/app

COPY shared ./shared
COPY services/crawler ./services/crawler

# Create a non-root user for security
RUN useradd -m agentuser && chown -R agentuser:agentuser /opt/venv /app
USER agentuser

EXPOSE 9101

CMD ["python", "services/crawler/main.py"]
