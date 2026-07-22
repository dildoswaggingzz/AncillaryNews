# M6+: Supply-event features -- build results

**Date:** 2026-07-22
**Design:** `docs/supply-event-features-design.md`

## What was built

- `init-db/08-market-events.sql` -- the `market_events` table (structured,
  Postgres, not Qdrant -- design §2), applied via `scripts/migrate.py`
  against the live dev database (`DATABASE_URL=postgresql://postgres:secret@
  localhost:5433/energy`). Verified idempotent: applying it twice produces
  identical schema, no errors.
- `shared/event_extractor.py` -- Claude Haiku (`claude-haiku-4-5`) event
  extraction, mirroring `shared/claim_extractor.py`'s conventions exactly:
  `ANTHROPIC_API_KEY`-missing returns `None` (logged warning, never raises),
  Tier-2 sources have `confidence` capped at `TIER2_CONFIDENCE_CAP = 0.5`,
  `magnitude_mw`/`effective_from` are asked for as `null` rather than a
  guess and are never coerced from a non-numeric/non-ISO-date response.
  `known_at` is never a field on `ExtractedEvent` at all -- it does not
  exist for the model to (mis)populate.
- `services/crawler/main.py` -- additive event storage in `process_article`,
  after the existing claim path: `_known_at` parses `ArticleRef.published`
  (RFC 822, via `email.utils.parsedate_to_datetime`) with a crawl-time
  fallback, `_event_id` mirrors `vector_store._claim_point_id`'s
  `uuid5(NAMESPACE_URL, ...)` scheme for idempotent re-crawl upserts, and
  the whole event path is wrapped in its own `try/except` so a failure there
  never touches claim storage or the crawl cycle. `run_crawl_cycle` now
  constructs a `DatabaseManager` via `_get_db()`, which degrades to `None`
  (event storage skipped, claims unaffected) if `DATABASE_URL` isn't
  configured -- `docker-compose.yml`'s `crawler` service now declares
  `DATABASE_URL` and a `db: service_healthy` dependency, matching the
  ingestor/orchestrator pattern.
- `shared/feature_store.py` -- four new columns on every `build_features`
  row: `announced_mw_entering_90d`, `net_demand_volume_change_30d`,
  `regime_change_within_horizon`, `days_since_last_supply_event`. Leak-safe
  on `known_at <= decision_time` (design §1); `effective_from` is read only
  as a value (the forward/trailing window bound), never the availability
  gate. `DatabaseManager.fetch_market_events(known_at_before=...)` does one
  broad per-call fetch; the per-row `known_at <= decision_time` filter (the
  actual leak-safety boundary) happens in Python, mirroring this module's
  existing RULE-B raw-series pattern.

## The leak test came first

`tests/test_feature_store.py`'s new "M6+: supply-event features" section
begins with `test_event_known_at_after_decision_time_never_leaks_into_that_
mtus_features` -- a synthetic event with `known_at` one minute after the
decision time and `effective_from` *already in the past* (the exact trap
design §1 describes: a builder keyed on `effective_from` would wrongly
include it). That test, and twelve siblings covering the boundary case, the
"knowable now but effective later" affirmative case, the 90-day window
edges, zone matching, and schema-determinism/fill-rate logging, were written
and confirmed **red** (`13 failed` against a `feature_store.py` with no
event-column code at all) before a single line of the corresponding
`build_features` implementation was written. Only after that did the
constants (`SUPPLY_ENTERING_EVENT_TYPES`, `ANNOUNCED_MW_FORWARD_WINDOW`,
etc.), `_fetch_events`, `_event_features`, and the `build_features` wiring
get added, at which point all 27 tests in the file (14 pre-existing + 13
new) went green.

## Actual fill rate against real crawled data

Run live against the dev database, right after applying the migration:

```
build_features(db, "DK2", 2026-06-22 08:00 UTC, 2026-07-22 08:00 UTC, horizon=1h)
-> 720 rows

event feature fill rate over [2026-06-22 08:00:00+00:00, 2026-07-22 08:00:00+00:00]
(n=720 rows): announced_mw_entering_90d non-null 0.0%, net_demand_volume_change_30d
non-null 0.0%, days_since_last_supply_event non-null 0.0%,
regime_change_within_horizon true 0.0% -- expected to be near-zero while
event history accrues (design §0)
```

**`market_events` has 0 rows.** The fill rate is genuinely, exactly **0%**
-- not a small non-zero number, zero. This is the honest, unfabricated state
of a table created by this build's own migration, minutes before this
report was written: no crawl cycle has run against it yet, so no event has
ever been extracted or stored. This is expected and correct, not a bug --
see the next section.

For calibration: the crawler's existing Qdrant `crawler_claims` collection
(the claim-extraction path this event path runs alongside) holds 142
points from ~1 month of real crawling (2026-06-19 -> 2026-07-20, per the
design doc's own count, reconfirmed live via Qdrant's collection info
endpoint during this build). That is the realistic order of magnitude
`market_events` will reach once crawl cycles start running against the new
table -- still small, still mostly-null for any given feature row, but
non-zero. Today it is exactly zero because the table did not exist until
this build's migration.

## Forward-accrual reality (design §0, restated plainly)

This is not a bug to fix later: supply-event history **cannot** be
backfilled the way market data can (design §0 -- announcements are
scattered, paywalled, not systematically retrievable), and no historical
article re-fetching was built here (explicitly out of scope, design §7).
So:

- **Today:** `announced_mw_entering_90d`, `net_demand_volume_change_30d`,
  and `days_since_last_supply_event` are `None`, and
  `regime_change_within_horizon` is `False`, for essentially every row of
  every `build_features` call, for every zone and horizon -- correctly, per
  the schema-determinism guarantee (design §5.2), the columns are always
  *present*, just uninformative.
- **From today forward:** every real crawl cycle that runs (respecting the
  existing `AUTO_RUN_ENABLED` cost gate, or a manual `POST
  /crawler/run-now`) that finds an article describing a genuine
  supply/demand/regime event will populate a `market_events` row via the
  new additive path, and `build_features` calls made after that event's
  `known_at` will pick it up.
- **In ~6-12 months** (the design's own estimate), enough history will have
  accrued for these columns to be a real, non-trivial-fill-rate training
  feature. Nothing here retrains any model (out of scope, design §7) --
  this build only makes the accrual start happening, honestly logged the
  entire time.

No dashboard/live-signal surface was built (out of scope, design §7); the
features exist and are honestly logged, nothing more.

## What was verified vs. assumed

**Verified, against the live system:**
- The migration applies cleanly and idempotently against
  `postgresql://postgres:secret@localhost:5433/energy` (ran it twice; second
  run's `CREATE TABLE IF NOT EXISTS`/`CREATE INDEX IF NOT EXISTS` were
  no-ops).
- `market_events` currently has 0 rows; `crawler_claims` (Qdrant) has 142
  points -- both read directly from the live services, not assumed from the
  design doc's own numbers (though they match it).
- `build_features` runs against the live DB for a real 30-day/720-row DK2
  window and produces the exact fill-rate log line quoted above.
- The pre-existing suite's baseline on this branch is **553 passed, 1
  deselected** (`main`'s 555 minus P2's +43 minus... -- the task's own
  framing was "verify your baseline, don't assume 555"; 553 is what this
  branch actually collects before any M6+ change). After this build:
  **599 passed, 1 deselected** (+46 new tests: 13 in
  `tests/test_feature_store.py`, 17 in the new
  `tests/test_event_extractor.py`, 12 in `tests/test_crawler_main.py`, 4 in
  `tests/test_db_manager.py` for the new `save_market_event`/
  `fetch_market_events` methods). No pre-existing failures on this branch --
  the baseline run was clean.
- `poetry run ruff check` / `ruff format --check` / the full
  `.pre-commit-config.yaml` hook set (ruff, ruff-format, check-yaml,
  end-of-file-fixer, trailing-whitespace, check-added-large-files) all pass
  clean on every file this build touched or added.

**Assumed / a judgement call, flagged explicitly (not verified against the
design doc, which underspecifies these):**
- **Event-to-row matching is zone-only.** Design §5 says "matched to the
  row's market/zone/direction", but `build_features(db, zone, start, end,
  horizon)` has no `market` or `direction` parameter at all -- its grain is
  `(zone, horizon)`. Only `zone` actually exists to match on; a null-zone
  event is conservatively excluded from the zone-specific columns rather
  than assumed zone-agnostic. Documented directly in `shared/
  feature_store.py`'s module docstring and `_event_features`'s docstring.
- **`None` (not `0.0`) as the default for the three non-boolean event
  columns.** This is the point in the design I was most tempted to go the
  other way on. Design §5 literally says "null/0 where none" -- `0.0` would
  have been a defensible reading, and it would have made every fill-rate
  percentage read as if the columns were 100% populated (with an asserted,
  confident zero) rather than showing the true near-total absence of
  signal. Given design §0's explicit, repeated instruction not to "make the
  sparse columns look more populated than they are", and the mandatory
  fill-rate log's own stated purpose (design §5: "so a 99%-null reality is
  visible in the logs"), I chose `None`. `0.0` would have technically
  satisfied "null/0 where none" while defeating the fill-rate log's actual
  point. `regime_change_within_horizon` is the one column left as a real
  boolean (`False`, never `None`) -- a flag is naturally two-valued (the
  same convention `_calendar_features`'s `is_danish_public_holiday` already
  uses), so this is a narrower, more defensible exception than blanket
  zero-defaulting would have been.
- **The specific numbers for the two under-specified constants**:
  `REGIME_CHANGE_FORWARD_WINDOW = 90 days` (design §5 asks for "a configured
  forward window" without naming one -- set equal to
  `ANNOUNCED_MW_FORWARD_WINDOW` for consistency rather than inventing an
  unrelated number) and `DAYS_SINCE_SUPPLY_EVENT_CAP = 365 days` (design §5:
  "capped/null when none", no value given). Both are marked as build-time
  choices, not design-doc facts, directly in `shared/feature_store.py`'s
  constants.
- **`SUPPLY_EVENT_TYPES` for `days_since_last_supply_event`** is
  `{prequalification, capacity_commissioning, capacity_retirement, outage}`
  -- excludes `demand_volume_change` (has its own column) and
  `regime_change`/`other` (not a capacity event). The design names the
  column "supply event" but does not enumerate the type set; this is a
  reasoned choice, flagged in the constant's own comment.
- **No backfill was attempted or built** (design §7, explicitly out of
  scope) -- the 142 existing Qdrant claim points were produced under crawls
  that (mostly) had `ANTHROPIC_API_KEY` set, so they stored *claims*, not
  raw article text; there is no stored article body to re-run the event
  extractor over without re-fetching the original URLs, which is exactly
  the "historical article re-fetching" the design forbids building. I did
  not run a live crawl cycle during this build either (no `AUTO_RUN_ENABLED`
  toggle, no `POST /crawler/run-now`) -- that is a real Anthropic API spend
  and a mutation of live production data that is an operational decision
  for the operator to make, not an implicit side effect of a feature build.
  The 0-row, 0%-fill-rate state reported above is therefore the true state
  of the system at the moment this was written, not a contrived "worst
  case" -- it is what "forward accrual starts today" actually looks like on
  day one.

## Where the leak rule was genuinely tricky

Two spots, both worth naming directly:

1. **The DB-layer fetch bound is not itself the leak-safety boundary, and
   it would be easy to think it is.** `fetch_market_events(known_at_before=
   <latest decision_time across the whole call>)` is a single, call-wide
   upper bound (matching the existing RULE-B "fetch wide" shape) -- but
   individual rows in the same `build_features` call have *earlier*
   decision times than that bound. The actual per-row gate
   (`e["known_at"] <= decision_time`) has to be re-applied inside
   `_event_features` for every row, in Python, exactly like RULE-B's
   `_at_or_before`. Getting this wrong would not show up as an obviously
   broken feature -- every row would just look "slightly more informed"
   than it should, since the DB query alone can't distinguish between rows.
   The dedicated boundary test
   (`test_event_known_at_exactly_the_decision_time_boundary_is_included`,
   which uses `<=` intentionally) and the forward-looking
   `test_event_known_well_in_advance_of_its_future_effective_date_is_
   included` test exist specifically because a fetch-level-only
   implementation would have passed the core leak test (which only checks
   one row) while still leaking on a multi-row call.
2. **The temptation to key `announced_mw_entering_90d`'s window on
   `effective_from` alone, since that reads most naturally as "what's the
   90-day pipeline."** The correct two-condition filter is `known_at <=
   decision_time AND decision_time <= effective_from <= decision_time +
   90d` -- both bounds have to reference `decision_time`, one as a ceiling
   on when the event became known, the other as a window on when the
   capacity lands. Writing `test_event_outside_the_90d_forward_window_is_
   excluded` right next to the two "trap" tests (leak / affirmative) made
   the two conditions' independence explicit rather than something I had to
   re-derive by inspection later.
