# AncillaryNews — "EnergySignals Agent"

An autonomous news agent that monitors the Danish ancillary services (balancing) markets, detects abnormal price movements, and **explains why they happened** — by correlating hard system data from Energinet/ENTSO-E with soft data (news, analyst commentary, TSO announcements).

**Primary focus:** the Nordic **mFRR Energy Activation Market (EAM)** and, more broadly, **the prices paid to Danish actors (BSPs)** across all ancillary markets — both reservation (capacity) payments and activation (energy) payments.

---

## 1. Mission & Scope

### The question the agent must answer

> *"The price paid to Danish balancing providers moved abnormally in market X, bidding zone Y, at time T. What caused it — and how confident are we?"*

### Markets in scope

Denmark spans two synchronous areas, so the market landscape differs per bidding zone:

| Market | DK1 (Continental Europe) | DK2 (Nordic) | Payment type |
|---|---|---|---|
| FCR | FCR Cooperation (regelleistung.net auctions) | FCR-N, FCR-D up/down (Nordic market) | Capacity (DKK/MW/period) |
| aFRR capacity | Nordic aFRR capacity market | Nordic aFRR capacity market | Capacity |
| aFRR energy | PICASSO platform | PICASSO platform | Activation (DKK/MWh) |
| mFRR capacity | Daily Energinet auctions | Daily Energinet auctions | Capacity |
| **mFRR energy — EAM** | **Nordic mFRR Energy Activation Market** (15-min MTU, marginal pricing) | **Nordic mFRR EAM** | **Activation — primary focus** |
| Context markets | Day-ahead / intraday prices, imbalance prices, interconnector flows & outages | same | Reference only |

"Prices paid to Danish actors" therefore means the sum of:
1. **Reservation payments** — what BSPs are paid for standing ready (capacity auctions).
2. **Activation payments** — what BSPs are paid when energy is actually activated (mFRR EAM balancing energy prices, aFRR/PICASSO prices, special regulation).
3. **Imbalance settlement effects** — how the above feed back into the imbalance price that BRPs face.

### Out of scope (v1)

- Trading advice or price forecasting (we explain the past/present, we don't predict).
- Settlement-level per-actor payment reconstruction (we work with published market prices/volumes, not confidential settlement data).
- Non-Nordic markets except as explanatory context (e.g. German FCR prices affecting DK1).

---

## 2. Output Contract — what an "explanation" looks like

Every triggered event produces a structured **Event Report** (JSON → Slack/dashboard):

```json
{
  "event_id": "2026-07-14T17:15Z-DK1-mFRR-EAM-up",
  "market": "mFRR EAM", "zone": "DK1", "direction": "up",
  "observation": "Balancing energy price hit 4,850 DKK/MWh vs 30-day P95 of 1,200",
  "hard_data_correlates": [
    {"signal": "imbalance", "value": "-820 MW", "source": "Energinet EDS"},
    {"signal": "DE-DK1 interconnector outage", "source": "ENTSO-E UMM"}
  ],
  "market_theories": [
    {"claim": "Analysts point to low wind + Karlshamn unavailability",
     "source": "EnergiWatch, 2026-07-14", "type": "theory"}
  ],
  "synthesis": "…LLM-generated explanation citing every claim…",
  "confidence": "medium",
  "data_maturity": "provisional — figures may be revised by Energinet"
}
```

Rules baked into the contract:
- **Facts vs. theories are always labelled.** Numbers only from Energinet/ENTSO-E; market commentary always attributed ("Kilde: Analyst X").
- **Every claim carries a source.**
- **Data maturity is declared** (real-time vs. revised figures — see §6).

---

## 3. Architecture — three-layer pipeline

Three Docker microservices plus storage, composable via `docker-compose up --build`.

### A. Ingestion Engine (hard data)

- **Sources:**
  - **Energinet Energi Data Service** (`api.energidataservice.dk`, REST/JSON): ancillary capacity prices (FCR/aFRR/mFRR per zone), mFRR EAM balancing energy prices and activated volumes, imbalance prices, day-ahead/intraday prices, system state ("Power System Right Now"), transmission outage/congestion data. *(Exact dataset IDs are catalogued in milestone M0 — see §9 — since Energinet renames datasets as markets evolve, e.g. the mFRR EAM go-live replacing the old regulating-power datasets.)*
  - **ENTSO-E Transparency Platform** (REST, security token): cross-border flows, UMMs/outages, balancing data for neighbouring zones — essential for explaining import/export-driven price moves.
  - **Nordic Balancing Model (nordicbalancingmodel.net)**: operational messages about EAM platform issues (pricing incidents on the platform itself are a known event class).
- **Logic:** async Python service (HTTPX), scheduled polling via **APScheduler** (Airflow is overkill for v1), **tenacity** for exponential backoff + circuit breaking in a shared `BaseIngestor` class.
- **KPI:** 100% polling uptime; every fetch idempotent and re-runnable.
- **Storage:** **TimescaleDB** hypertables keyed on `(market, zone, product, mtu_start, published_at)` — the `published_at` dimension is what makes revision handling possible.

### B. Insight Crawler (soft data)

- **Sources:** RSS feeds from sector media (EnergiWatch, Montel, Energy Supply DK), Energinet press releases and market messages, Nordic Balancing Model news, selected analyst posts.
- **Logic:** scraper container (**Playwright** for JS-heavy sites, plain HTTP+feedparser for RSS) + a reader step that converts pages to LLM-ready Markdown (Firecrawl or trafilatura).
- **Function:** summarise each article and extract **market theses** — e.g. *"Analyst X expects wind shortfall to lift DK1 balancing prices tonight"* — each stored with source, author, timestamp, and claim type (`fact | theory | forecast`).
- **KPI:** unstructured source → structured, attributed Markdown within one crawl cycle.
- **Storage:** **Qdrant** (vector DB, self-hostable in compose — chosen over managed Pinecone to keep the whole stack reproducible locally) with timestamp + source metadata on every embedding.

### C. Intelligence Orchestrator (reasoning layer)

Decision flow on every evaluation tick:

1. **Anomaly check** (rule engine, §5) on fresh time-series data.
2. On trigger: pull the hard-data context window (prices, volumes, imbalances, flows, outages ±N hours).
3. **RAG retrieval:** semantic + time-filtered search in Qdrant for recent analyses/theories relevant to the market/zone/direction.
4. **LLM synthesis** with a prompt that enforces the output contract: *"Use only factual numbers from Energinet/ENTSO-E; clearly mark any market-actor theories as attributed claims."*
5. Emit the Event Report as JSON → Slack webhook / dashboard.

- **Orchestration:** LangChain or LlamaIndex — or a thin hand-rolled pipeline; the retrieval/synthesis flow above is simple enough that a framework is optional, not required.
- **LLM:** Claude Opus 4.8 (`claude-opus-4-8`) for synthesis — strong at correlating time series with textual context; Claude Haiku 4.5 (`claude-haiku-4-5`) for cheap bulk summarisation/extraction in layer B.

---

## 4. Trigger events — the rule engine

Don't watch prices alone; the real market shifts show up in reserves before spot follows. Trigger classes (each with per-market/zone thresholds, tuned against historical distributions):

| Trigger | Example rule |
|---|---|
| Activation price spike (EAM) | mFRR EAM price > rolling 30-day P95, or > k × day-ahead price |
| Capacity price anomaly | FCR/aFRR/mFRR capacity clearing price outside seasonal band |
| Abnormal pricing pattern | Negative/zero prices where unusual; price divergence DK1 vs DK2 |
| Volume anomaly | Activated mFRR volume ≫ normal for the hour; sustained one-directional activation |
| Structural events | Interconnector outage (UMM), EAM platform incident message, market-rule change announcement |
| Revision alert | Previously published figure revised beyond tolerance (see §6) |

---

## 5. Data quality & revision handling

Energinet's data has varying maturity: real-time figures are provisional and revised over time. To avoid the agent generating **false news from later-corrected numbers**:

- Every ingested row stores `published_at` alongside the market time unit — the DB is bitemporal-lite.
- Re-polling updates rows and records a revision delta; reports generated from provisional data are labelled as such.
- If a revision invalidates an already-published Event Report beyond tolerance, the agent emits a **correction event** referencing the original report — it never silently rewrites history.

---

## 6. Source validation

- Metadata on everything: `source`, `author`, `retrieved_at`, `claim_type`.
- Two-tier trust model: **Tier 1** (Energinet, ENTSO-E, NBM — citable as fact) vs **Tier 2** (media, analysts — citable only as attributed theory).
- The synthesis prompt hard-requires: numbers exclusively from Tier 1; Tier 2 content always framed as *"according to …"*.
- Reports failing citation validation (any unattributed claim) are rejected before publication.

---

## 7. Tech stack

| Component | Tool |
|---|---|
| Language | Python 3.12+ (Pandas/Polars for data manipulation) |
| API integration | HTTPX (async) + tenacity (retry/circuit-breaking) |
| Scheduling | APScheduler (Airflow later if job graph grows) |
| Time-series DB | PostgreSQL + TimescaleDB |
| Vector DB | Qdrant (self-hosted, in compose) |
| Scraping | Playwright + feedparser; Firecrawl/trafilatura for Markdown conversion |
| Agent logic | LangChain / LlamaIndex (or thin custom pipeline) |
| LLM | Claude Opus 4.8 (synthesis), Claude Haiku 4.5 (bulk extraction) |
| Alerting | JSON → Slack webhook; dashboard later |
| Monitoring | Prometheus + Grafana (poller health, trigger rates, LLM latency/cost) |
| Packaging | Poetry monorepo; docker-compose with `ingestor`, `crawler`, `orchestrator` + DBs |

---

## 8. Success criteria

1. **Latency:** Early-warning Event Report within **15 minutes** of an anomaly appearing in the data.
2. **Traceability:** every report cites its sources and labels facts vs. theories; zero unattributed claims.
3. **No false news:** revisions handled explicitly; provisional data labelled; corrections published, never silent edits.
4. **Reproducibility:** the entire system rebuilds from scratch with `docker-compose up --build`.

---

## 9. Roadmap

- **M0 — Data audit (1 sprint):** catalogue the exact Energi Data Service dataset IDs for every market in §1 (via `api.energidataservice.dk/meta/datasets`), ENTSO-E endpoints, and RSS feeds; document schemas and revision behaviour. *This gates everything else — dataset names change as markets evolve (e.g. the 2025 mFRR EAM go-live).*
- **M1 — Ingestion:** monorepo + compose skeleton; TimescaleDB schema (hypertables, bitemporal columns); `BaseIngestor` with tenacity; pollers for EAM prices, capacity auctions, imbalance, day-ahead.
- **M2 — Rule engine:** trigger classes from §4 with backtested thresholds; Slack alerting of raw triggers (no LLM yet — validates signal quality early).
- **M3 — Soft data:** crawler + Markdown reader + Qdrant embedding pipeline with claim-type extraction.
- **M4 — Reasoning:** RAG retrieval + LLM synthesis + output-contract validation; end-to-end Event Reports; Grafana dashboards.

---

## Brainstorming / open questions

- Should DK1 FCR analysis include German FCR Cooperation auction results as a first-class input (prices clear jointly)?
- How to weight analyst theories that later prove wrong — feedback loop for source credibility scoring?
- Per-actor payment estimation (volumes × marginal price) as a v2 feature, clearly labelled as an estimate?
