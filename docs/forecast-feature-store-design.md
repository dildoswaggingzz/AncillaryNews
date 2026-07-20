# M6 P1 Design: the leak-safe feature store

**Date:** 2026-07-20
**Status:** Design, ready to build. Companion to `docs/forecast-datasets-scope.md` (M6 scope)
and `docs/forecast-allocation-design.md` (M6 allocation design). Read §0 of this document,
then build; the two companions are context, not prerequisites.
**Prerequisite:** M6 P0 is complete. All source data below was verified present in
`market_data_history` on 2026-07-20 by direct query, not assumed.

---

## 0. The one property that matters

A feature store for price forecasting has exactly one job it can fail at silently:

> **Every feature must have been knowable at the time the decision was made.**

A model trained on a feature that leaked future information backtests beautifully and loses
money live. Nothing else in this milestone is as expensive to get wrong, and nothing else is
as hard to notice after the fact — the failure has no symptom until real money is committed.

So the defining property of `build_features` is: **it takes a horizon parameter and is
structurally incapable of emitting a column unavailable at that horizon.** Not "we're careful
not to include leaky columns" — incapable.

**Build order is not negotiable: write the horizon test first (§2), then the builder.** The
test is the deliverable. The builder is the thing that makes it pass.

---

## 1. Interface

```python
def build_features(
    db: DatabaseManager,
    zone: str,                    # "DK1" | "DK2"
    start: datetime,
    end: datetime,
    horizon: timedelta,           # how far before mtu_start the decision is made
) -> list[dict]                   # one row per (zone, mtu_start), sorted by mtu_start
```

Where it lives: `shared/feature_store.py`, following the module conventions of
`shared/bess_simulator.py` (module docstring explaining *why*, dataclass config where there
are knobs, no hidden globals).

`horizon` is the contract. `horizon=timedelta(hours=12)` means "the decision is made 12h
before `mtu_start`", so every feature in the returned row must have been publishable by
`mtu_start - 12h`. **A caller must not be able to get a leaky frame by passing an unusual
horizon** — the guarantee holds for all horizons, not just the ones we test.

Two canonical horizons drive the M6 targets (see the allocation design §2c):
- **D-1 auction:** the decision is made at the day-ahead gate, so ~12–37h depending on hour.
  Start with a flat `timedelta(hours=12)`; a gate-time-aware horizon is a later refinement,
  recorded in §7.
- **Intraday:** deferred (allocation design §2c.4). Do not build it, but do not make it
  impossible either — that is what the `horizon` parameter is for.

---

## 2. The horizon test — write this first

`tests/test_feature_store.py`. The test asserts the property directly, not by inspecting a
hardcoded column list (a list rots the moment someone adds a feature).

Required cases:

1. **Leaky column is absent at every horizon.** No `*_current_leaky_do_not_use_as_feature`
   product ever appears in output, at any horizon. See §4.1.
2. **Horizon monotonicity.** A feature present at `horizon=1h` must not vanish at
   `horizon=12h` *unless* its source publication time genuinely falls between them — i.e.
   the feature set at a longer horizon is a subset of the shorter one. This catches
   accidental horizon-independence, where a builder ignores the parameter entirely.
3. **Source-time bound.** Seed `market_data_history` with a row whose `time` is *after*
   `mtu_start - horizon` and assert no feature in that MTU's row is derived from it. This is
   the direct statement of the §0 property. Use a synthetic fixture, not live data.
4. **Realised-value features respect the lag.** `realtime_production_exchange` at
   `mtu_start` is *not* knowable at `mtu_start - 12h`; only values at or before
   `mtu_start - horizon` are. Assert this explicitly — it is the easiest one to get wrong,
   because the data is in the table and joins cleanly.

If a case is hard to write, that is a signal the interface is wrong. Fix the interface.

---

## 3. Source data — verified present

Every market/product/zone below was confirmed in `market_data_history` on 2026-07-20.

| market | products | zones |
|---|---|---|
| `wind_solar_forecast` | `{offshore_wind,onshore_wind,solar}_{day_ahead,intraday,5hour,1hour}` | DK1, DK2 |
| `realtime_production_exchange` | `offshore_wind`, `onshore_wind`, `solar`, `production_lt100mw`, `production_ge100mw`, `exchange_{germany,sweden,norway,netherlands,great_britain,great_belt,bornholm_se4}` | DK1, DK2 |
| `aFRR_border_atc` | `import`, `export` | **DK1-DE, DK1-NL, DK2-DE, DK2-DK1, SE4-DK2** |
| `aFRR_lfc_limits` | `up`, `down` | DK1, DK2 |
| `day_ahead` | `price` | DK1, DK2, **DE**, SE3, SE4, NO2 |
| `FCR` | `up`, `down`, `price`, `d1_up`, `d1_down`, `d1_price`, volumes | DK2, SE1–SE4 |
| `aFRR_capacity` | `up`, `down`, `up_eur`, `down_eur`, demand/procured volumes | DK1, DK2, FI, NO1–5, SE1–4 |
| `imbalance` | `imbalance_price`, `afrr_vwa_up`, `afrr_vwa_down` | DK1, DK2 |
| `inertia` | `nordic`, `dk2`, `se`, `no`, `fi` | ALL, DK2, … |

Cross-zone note: `day_ahead` carries **DE** (the DK1 coupling partner) and SE3/SE4 (DK2's).
Both are legitimate features. `inertia` and Nordic FCR zones are DK2-relevant only — see the
allocation design §2b.2 on why the market taxonomy is zone-dependent.

---

## 4. Four hazards, all verified live

These are not hypothetical. Each was found by querying the real database.

### 4.1 `ForecastCurrent` is leak-contaminated

Ingested deliberately under the product name
`{offshore_wind,onshore_wind,solar}_current_leaky_do_not_use_as_feature`. On settled rows it
equals the last-revised value — information unavailable at bid time.

**The builder must filter these out structurally**, not by omission. A deny-rule on the
product-name suffix `_current_leaky_do_not_use_as_feature`, asserted by test case §2.1. If
someone later adds another leaky column with that suffix, it is excluded automatically.

Match the horizon column to the decision:
- `*_day_ahead` → D-1 models
- `*_5hour` / `*_1hour` → intraday models (deferred, but the mapping belongs in code now)

### 4.2 `fetched_at` is NOT a publication time for backfilled rows

`shared/db_manager.py:123` sets `fetched_at = datetime.now(UTC)` at insert. For rows written
by the P0 backfill, that is *the time the backfill ran* — it says nothing about when Energinet
published the figure. The schema comment in `init-db/01-init.sql` calls `fetched_at` a proxy
for `published_at`; **that proxy is valid for live-polled rows and invalid for backfilled
ones.**

**Consequence: the as-of join must not key on `fetched_at`.** Vintage comes from the
dataset's own horizon columns (§4.1) and from `time` itself. This is the single most
important correctness constraint in this document, and it is the one that would otherwise
produce a model that backtests well and fails live.

### 4.3 Duplicate revisions exist

The P0 backfill is append-only and re-running it appends rather than dedupes (verified:
`aFRR_lfc_limits` holds ~53k rows over ~33k distinct `(time, zone, product)` points).

**Every query must dedupe explicitly** — `DISTINCT ON (time, zone, product) ... ORDER BY
time, zone, product, fetched_at DESC`, the pattern `fetch_market_data`
(`shared/db_manager.py:253`) already uses. A naive `SELECT` double-counts silently.

Note the tension with §4.2: `fetched_at DESC` is the right *dedupe* key (take the latest
revision) while being the wrong *vintage* key (it does not tell you when the value was
publishable). Both statements are true; keep them separate in your head and in the code
comments.

### 4.4 `aFRR_border_atc` is keyed by corridor, not bidding zone

Its `zone` column holds `DK1-DE`, `DK1-NL`, `DK2-DE`, `DK2-DK1`, `SE4-DK2` — corridor
identifiers, not bidding zones. **`WHERE zone = 'DK1'` returns nothing for this market.**

Derive membership from the corridor string. Note `SE4-DK2` and `DK2-DK1` put the relevant
zone *second*, so a naive `startswith` misses them: a corridor is relevant to zone Z if
either endpoint is Z. Build the border feature set per zone accordingly.

### 4.5 `wind_solar_forecast` has an `UNDEFINED` zone pocket

482 rows, confined to 2026-05-27 → 2026-05-29 (0.3% of that market). Cause not investigated.

**Filter to an explicit zone allow-list (`{"DK1", "DK2"}`) rather than accepting whatever zone
strings appear**, and log a warning with a count when rows are dropped. Silently trusting the
`zone` column is what would let this pocket — or the next one — into a training set.

---

## 5. Derived features

Raw columns are the input, not the output. These derived features are where the signal is —
per the scope doc §2, forecast *error* and *revision* are typically stronger predictors of
balancing prices than levels.

**Forecast revisions** (both endpoints from the same MTU's forecast row, so no leak risk):
- `wind_revision_da_to_intraday` = `*_intraday − *_day_ahead`
- `wind_revision_5h_to_1h` = `*_1hour − *_5hour`
- Per forecast type, and summed across wind types.

**Realised forecast error** — *lagged, see §2.4*. At `mtu_start − horizon` you know the error
for MTUs already settled, not the current one:
- `wind_forecast_error_lag_{k}` = realised `offshore_wind + onshore_wind` minus the
  corresponding forecast, for the most recent MTU at or before `mtu_start − horizon`.

**Residual load:**
- `residual_load` = total production − (wind + solar). Note `GrossCon` (consumption) is *not*
  currently ingested — see §7; until it is, use production as the base and name the column
  honestly (`residual_production`, not `residual_load`).

**Interconnector saturation:**
- Per relevant corridor (§4.4): `atc_import`, `atc_export`, and a `saturated` flag.
- Realised flow from `realtime_production_exchange.exchange_*`, lagged per §2.4.

**Cross-zone prices:** `day_ahead` for DE (DK1) and SE3/SE4 (DK2), lagged appropriately.

**Nordic system state (DK2 only):** `inertia.nordic`, `aFRR_lfc_limits.{up,down}` and headroom
against realised activation.

**Calendar:** hour-of-day, day-of-week, month, Danish public holiday, and an
`is_after_d1_gate` flag.

---

## 6. Acceptance

- `tests/test_feature_store.py` passes, containing all four §2 cases. **These were written
  before the builder.**
- Full suite green (`poetry run pytest`, currently 536 passing). Report pre-existing failures
  separately from new ones.
- A smoke run over a real 7-day DK2 window returns one row per hour with no all-null columns,
  and logs the count of rows dropped by the §4.5 zone filter.
- Lint/format per `.pre-commit-config.yaml`.
- No changes to `market_data_history`'s schema, to `shared/datasets.py`, or to ingestion. P1
  is read-only over what P0 landed.

---

## 7. Deliberately out of scope

- **Models.** P1 is the feature store only. No LightGBM, no targets, no training. P2
  (baselines) and P3 (models) follow.
- **Gate-time-aware horizons.** A flat `timedelta` is the v1 contract; the real D-1 gate is a
  wall-clock time, making the true horizon vary by delivery hour. Recorded as the first
  refinement once the flat version is proven.
- **`GrossCon` / consumption.** Not currently ingested (`GenerationProdTypeExchange` is scope
  Tier 2). Real residual load needs it; until then §5's naming stays honest.
- **The `UNDEFINED` zone root cause** (§4.5). Filter and log now; investigate separately.
- **Intraday features.** Deferred per the allocation design §2c. The `horizon` parameter keeps
  the door open; nothing else is built.
