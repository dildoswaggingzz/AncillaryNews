# M6+: Supply-event features — the differentiated input

**Date:** 2026-07-22
**Status:** Design, ready to build. Branches off `main` — independent of the P2/P3 model stack
(it touches the news pipeline and feature store, both already in `main`; it does *not* need
`forecast_model.py` or `baselines.py`).
**Depends on:** P1 (`shared/feature_store.py`), the crawler (`services/crawler/main.py`,
`shared/claim_extractor.py`, `shared/vector_store.py`).

---

## 0. Why this exists, and the honest constraint

P3 established empirically that gradient boosting on 34 fundamentals does **not** beat a rolling
climatology on FCR-D DK2. The mechanistic reason: the dominant driver of that market is *supply
entering* — batteries prequalifying into a fixed-demand auction — and that signal is **not in
any price, weather, or load series.** It lives in prequalification registers, TSO procurement
notices, and project announcements. The news pipeline is the only component that can see it.

**The constraint, stated up front so nothing is built on a false premise:** the crawler has ~1
month of history (142 claims, 2026-06-19 → 07-20), and news history **cannot be backfilled** the
way market data can — historical announcements are scattered, paywalled, and not systematically
retrievable. So supply-event features **cannot be a training feature for a walk-forward model
today.** They are ~entirely null before mid-2026.

This is therefore a **forward-accrual build**, decided deliberately (operator, 2026-07-22):

1. Build the typed-event extractor and the leak-safe feature integration **now**, so event
   history begins accumulating from today and becomes a real training feature in ~6–12 months.
2. The same features can drive a **live signal today** (a nowcast: "240 MW prequalified into
   FCR-D DK2 last week") even while they are useless for a historical backtest.

Everything below must be honest about this. A feature column that is 99% null historically is
correct here, not a bug — but it **must be logged and documented as such**, never quietly
shipped as if it carried historical signal.

---

## 1. The leak rule — different from P1's, and the thing most likely to be got wrong

P1's leak rule is about *market data* publication time. This one is about *when information
entered the public domain*.

> An event's **`known_at`** is when the information became public — the article's `published`
> timestamp (`ArticleRef.published`), falling back to crawl time when `published` is null. An
> event is usable as a feature for a decision made at time `d` **only if `known_at <= d`.**

For a feature row at `mtu_start` with decision horizon `h`, the decision time is
`mtu_start - h`. So an event contributes only if `known_at <= mtu_start - h`.

**The trap:** an event about capacity *effective* on 2026-09-01 but *reported* on 2026-07-15 is
knowable from 2026-07-15. Keying the feature on `effective_from` instead of `known_at` leaks the
future into the past — the model would "know" about capacity before it was announced. `known_at`
gates availability; `effective_from` is a *value* inside the feature (how soon the announced
capacity lands), never the availability key.

A synthetic-fixture test asserting an event with `known_at > decision_time` never appears in that
MTU's features is the core deliverable, exactly as the horizon test was for P1.

---

## 2. Typed-event schema

New Postgres table (`init-db/08-market-events.sql`). **Postgres, not Qdrant** — events need
structured numeric/date queries for the feature join, not semantic retrieval. Qdrant stays the
home for free-text claims (RAG); this is its structured complement.

| column | type | notes |
|---|---|---|
| `event_id` | text PK | `uuid5(NAMESPACE_URL, f"{url}#event:{i}")` — re-crawl upserts, never duplicates (mirrors `vector_store._claim_point_id`) |
| `event_type` | text | `prequalification` \| `capacity_commissioning` \| `capacity_retirement` \| `demand_volume_change` \| `outage` \| `regime_change` \| `other` |
| `market` | text | `FCR` \| `aFRR` \| `mFRR` \| `FFR` \| `day_ahead` \| null |
| `zone` | text | `DK1` \| `DK2` \| `SE4` \| … \| null |
| `direction` | text | `up` \| `down` \| null (FCR-D is directional) |
| `magnitude_mw` | double | null when not stated — do not invent |
| `effective_from` | date | when the change takes effect; null if unknown. A *value*, not the availability key (§1) |
| `known_at` | timestamptz NOT NULL | the leak-safe availability key (§1) |
| `confidence` | double | model's 0–1 confidence; Tier-2 sources capped per the trust model (§3) |
| `source_url`, `source_title`, `source_tier` | text | traceability |
| `raw_excerpt` | text | the sentence the event came from — every event must be traceable to source text |
| `extracted_at` | timestamptz | crawl time (audit; distinct from `known_at`) |

Index `known_at`, `(market, zone)`, `effective_from`.

---

## 3. Extractor — `shared/event_extractor.py`

A new module beside `claim_extractor.py`, same conventions:

- **Model: `claude-haiku-4-5`** (`MODEL` in `claim_extractor.py`) — this is structured
  extraction at volume, the exact Haiku use-case, and consistent with the claim path. Do not use
  a larger model.
- **`ANTHROPIC_API_KEY`-missing precedent, verbatim from `claim_extractor.py`:** a missing key or
  failed call logs a warning and returns `None` (distinct from "zero events found"), never
  raises. The crawler must degrade to claims-only, never crash on the event path.
- **Two-tier trust model, as in `claim_extractor.py`:** a Tier-2 (sector media/analyst) source
  never yields a high-confidence event — cap Tier-2 `confidence` (e.g. ≤ 0.5) so the feature
  layer can weight or filter on it. Tier-1 (Energinet/ENTSO-E/TSO) events may carry full
  confidence.
- Prompt extracts *only* supply/demand/regime events with the §2 fields, and must return
  `magnitude_mw` and `effective_from` as null rather than guessing — a hallucinated MW figure is
  worse than a null, because it becomes a numeric feature.
- Returns structured events; the crawler assigns `known_at` from `article.published` (fallback
  crawl-time) — **not the model**, which must never be trusted to date events.

---

## 4. Crawler integration

In `services/crawler/main.py:process_article`, after the existing claim path, extract events from
the same `text` and store them in Postgres. Requirements:

- **Additive and non-fatal:** an event-path failure must not affect claim storage or the crawl
  cycle. Same swallow-and-log discipline the article path already uses.
- Idempotent on re-crawl (deterministic `event_id`, upsert).
- The crawler needs a Postgres handle (`DatabaseManager`) alongside its Qdrant store — wire it in
  following the existing dependency pattern.
- Backfill of events over the *existing* 142 claims / raw articles is optional and low-value
  (one month), but if cheap, running the extractor over already-crawled articles seeds the table.
  Do not build historical article re-fetching for it.

---

## 5. Feature-store integration — leak-safe, schema-stable, honestly sparse

Extend `build_features` (`shared/feature_store.py`) with event-derived columns, under the **same
two guarantees P1 already enforces** — do not weaken either:

1. **Leak-safe as-of join** on `known_at <= mtu_start - horizon` (§1).
2. **Schema determinism:** the event columns appear for *every* row regardless of whether any
   event exists in the window — null/0 where none, never dropped. This is P1's train/serve-skew
   lesson; the feature set must depend only on `(zone, horizon)`, never on the data present.

Derived columns (all as-of `decision_time = mtu_start - horizon`):

- `announced_mw_entering_90d` — Σ `magnitude_mw` of prequalification/commissioning events with
  `known_at <= decision_time` and `effective_from ∈ [decision_time, decision_time + 90d]`,
  matched to the row's market/zone/direction.
- `net_demand_volume_change_30d` — signed Σ of `demand_volume_change` magnitudes known in the
  trailing 30 days.
- `regime_change_within_horizon` — flag: any `regime_change` event known, effective within a
  configured forward window.
- `days_since_last_supply_event` — recency of the most recent relevant event (capped/null when
  none).

**Weight by `confidence`** where summing magnitudes, so a Tier-2 rumour contributes less than a
Tier-1 TSO notice.

**Mandatory honesty:** `build_features` must **log the fill rate** of the event columns over the
requested window (e.g. "event features non-null in 2.1% of rows — expected while history
accrues"). And `docs/` must state plainly that these columns carry no historical signal yet.

---

## 6. Acceptance

- `init-db/08-market-events.sql` migration; `scripts/migrate.py` applies it.
- `shared/event_extractor.py` with the `ANTHROPIC_API_KEY`-missing and Tier-2-cap tests, using
  synthetic responses (no live API in tests).
- Crawler stores events additively; a test that an event-path failure leaves claim storage
  intact.
- `build_features` event columns: **leak-safe test first** (an event with `known_at` after
  decision time never appears), plus the schema-stability test extended to event columns (P1's
  pattern), plus a fill-rate log.
- Full suite green (`poetry run pytest`; `main` is at 555 — this branch does **not** include P2's
  +43 tests). Report pre-existing failures separately.
- Lint/format per `.pre-commit-config.yaml`.
- A short `docs/supply-event-features-results.md` stating the current fill rate against real
  crawled data and the forward-accrual reality.

---

## 7. Out of scope

- **Any model change.** This produces features; it does not retrain P3. Re-running P3 with event
  features is pointless today (§0) and is revisited when history accrues.
- **Historical news backfill / article re-fetching.** Impossible at useful depth (§0); do not
  attempt it.
- **A live dashboard/alert surface.** The features enable it, but building the nowcast UI is a
  separate piece — this build stops at the feature and its honest logging.
- **Day-ahead target** — the parallel track, built separately on the P2/P3 stack.
