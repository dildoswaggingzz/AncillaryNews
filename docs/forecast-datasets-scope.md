# M6 Scope: Energinet datasets for a forecasting layer

**Date:** 2026-07-20
**Status:** Scoping — not yet built. Implementation is intended for a cheaper model; every
section below is meant to be executable without re-deriving the research.
**Method:** All dataset IDs, field names, history depths and retention windows below were
verified by direct query against `api.energidataservice.dk` on 2026-07-20, not taken from
documentation. Anything unverified is explicitly marked **[VERIFY]**.

---

## 0. Framing: what changes versus the current system

The README scopes v1 as explanatory ("we explain the past/present, we don't predict") and
names **mFRR EAM** as the primary market. A forecasting layer inverts both of those:

1. **Forecasting needs exogenous drivers, not just prices.** All 14 currently-ingested
   datasets (`shared/datasets.py`) are market outcomes — prices, cleared volumes, system
   state. There is **not one fundamentals dataset in the system**: no wind/solar forecast,
   no load, no production by fuel, no interconnector flow. A price model built on the
   current tables can only learn autocorrelation. This is the single largest gap.

2. **mFRR is not a forecast target here.** Per `bess-market-eligibility` and
   `shared/bess_simulator.py:EXCLUDED_MARKETS`, batteries cannot participate in
   `mFRR_capacity` / `mFRR_EAM`. mFRR EAM remains valuable as a **feature** (it drives the
   imbalance price and is the cleanest proxy for system stress), but forecasting it does not
   produce a tradable decision. Targets must be the markets a BSP can actually bid into:
   FCR, aFRR capacity, aFRR energy, day-ahead, imbalance.

3. **The useful forecast object is not a price — it's an allocation.** For each hour, a
   battery chooses one market. So the model should emit a *vector of hourly prices across
   eligible markets in a common currency*, and the decision layer takes the argmax net of
   cycling cost. Forecasting FCR-D alone, in isolation, does not answer any question the
   operator actually has.

---

## 1. Binding constraints (read before planning any model)

These four facts should determine the model class. They are the real output of this audit.

### 1.1 Short *current-dataset* history, but the predecessors splice cleanly

Taken at face value, the current dataset IDs look starved:

| Target dataset | Earliest record | Span |
|---|---|---|
| `MfrrEnergyActivationMarket` | 2025-03-03 | ~16 months |
| `ImbalancePrice` | 2025-03-04 | ~16 months |
| `DayAheadPrices` | 2025-09-30 | ~10 months |
| `AfrrReservesNordic` | 2022-12-07 | ~3.6 years |
| `mFRRCapacityMarket` | 2023-06-20 | ~3.1 years |
| `FcrNdDK2` | 2021-11-09 | ~4.7 years |

**But the "discontinued" predecessors still serve data, and they splice with no gap and no
overlap** (verified):

- `Elspotprices`: **1999-06-30 → 2025-09-30T21:00**. `DayAheadPrices` begins
  2025-09-30T**22:00**. Day-ahead price history is therefore continuous from 1999 across the
  two IDs. `Elspotprices` also carries `SYS` and `DE` price areas — useful for German
  coupling features on DK1.
- `RegulatingBalancePowerdata`: from **1999-06-30**, carrying `ImbalancePriceDKK/EUR`,
  `BalancingPowerPriceUp/DownDKK/EUR`, `mFRRUpActBal`/`mFRRDownActBal`, `ImbalanceMWh` — the
  predecessor to both `ImbalancePrice` and the balancing-energy price series.
  **[VERIFY]** its end date (rate-limited during the audit; expected ~2025-03-03 to splice
  with `ImbalancePrice`). Confirm the boundary before relying on it.

So the constraint is **not** a data shortage. It is a **regime break**: the mFRR EAM go-live
(Mar-2025) and the 15-min MTU cutover (Oct-2025) redesigned the market these prices come from.
The real choice is:

- **Long history across the break** — more data, but the pre-2025 rows describe different
  clearing rules, a different MTU, and a different product set. Include a regime dummy at
  minimum; expect the model to weight recent data anyway.
- **Post-Mar-2025 only** — regime-consistent but ~12,000 hourly observations.

**Recommendation:** train post-break, use pre-break history only for (a) sanity-checking
seasonal shape and (b) the two targets that did not break — `FcrNdDK2` (4.7 years) and
`AfrrReservesNordic` (3.6 years), whose auction designs were untouched by the EAM go-live.
That is also why §3 recommends starting with FCR-D DK2.

Consequences of the post-break sample size:
- **Gradient-boosted trees (LightGBM / XGBoost) or regularised linear models with explicit
  seasonal terms.** No LSTMs, no transformers — not enough regime-consistent data to justify
  them.
- **Barely one annual cycle post-break.** Seasonality cannot be validated on the broken
  targets, only assumed or borrowed from pre-break data. Any quarter/year-horizon output must
  stay qualitative — already the confirmed product decision in
  `shared/forecast_synthesizer.py`. That decision now has a hard data justification; record it.
- Walk-forward expanding-window CV only. A random train/test split on this data is invalid.

### 1.2 URGENT — 90-day rolling retention on the millisecond datasets

Three datasets return data starting **exactly 91 days before today**:

| Dataset | Earliest record (2026-07-20) |
|---|---|
| `AfrrEnergyActivation` | 2026-04-20T08:44 |
| `AfrrBorderAvailableTransferCapacity` | 2026-04-20T09:00 |
| `AfrrLfcActivationLimits` | 2026-04-20T08:45 |

This is a rolling window, not a start date. **Every day without archival permanently destroys
a day of aFRR training data — it cannot be backfilled later, at any price.** `AfrrEnergyActivation`
is the aFRR energy activation price: the revenue stream `shared/bess_simulator.py` already
models and an eligible market for the battery.

This makes P0 (§4) a prerequisite for everything else, and it should start before any
modelling design work. It is pure ingestion — cheap, mechanical, and the highest-value hour
of work in this entire scope.

**[VERIFY]** Confirm the window is rolling by re-querying earliest records in ~2 weeks and
checking the start date has advanced. If it has not, this is a fixed start date and the
urgency drops.

### 1.3 `ForecastCurrent` is leak-contaminated — never use it as a feature

`Forecasts_Hour` carries four horizon-specific columns plus a "current" column. Verified on
historical rows (DK1, 2026-06-10T03:00, Offshore Wind):

```
ForecastDayAhead=691.29  ForecastIntraday=515.92  Forecast5Hour=646.50
Forecast1Hour=615.83     ForecastCurrent=515.92   TimestampUTC=2026-06-10T03:00:22
```

Two things follow, and both matter:

- **Good news:** the horizon columns retain genuinely distinct vintages on historical rows.
  Leak-free features are available *directly from the current API* — no forecast-vintage
  archive needs building. This is unusually convenient and is why wind/solar is cheap to add.
- **Trap:** `ForecastCurrent` equals `ForecastIntraday` on settled rows — it is the
  last-revised value, i.e. it contains information unavailable at bid time. Using it as a
  feature produces a model that backtests beautifully and fails live. **Match the column to
  the decision horizon:** `ForecastDayAhead` for D-1 capacity auction models, `Forecast5Hour`
  / `Forecast1Hour` for intraday and activation models. Enforce this in code, not in a
  comment — see §4 P1.

### 1.4 The API rate-limits aggressively

Backfill hit `{"statusCode": 429, "message": "Rate limit is exceeded. Try again in 197 seconds."}`
after roughly 20-30 requests in quick succession. Cooldowns are ~200s. `scripts/backfill_history.py`
already chunks by date range but has no rate limiter; a multi-year, multi-dataset backfill
will stall on this. Add token-bucket throttling plus 429-aware retry (respect the advertised
retry delay rather than exponential backoff) to `shared/base_ingestor.py` before attempting
the §4 P0 backfill.

---

## 2. Datasets to add — the fundamentals gap

Ranked by forecast value. Field names verified against live samples.

### Tier 1 — add these first; the model is not credible without them

**`Forecasts_Hour`** — wind & solar forecast, hourly, per price area. *History from
2019-10-31.*
Fields: `HourUTC`, `PriceArea`, `ForecastType` (`Offshore Wind` / `Onshore Wind` / `Solar`),
`ForecastDayAhead`, `ForecastIntraday`, `Forecast5Hour`, `Forecast1Hour`, `ForecastCurrent`,
`TimestampUTC`.
Why: residual load (demand minus renewables) is the dominant driver of every balancing price
in DK. Forecast *error* and forecast *revision* between horizons are typically stronger
predictors of activation prices than the level itself — a large DA→1h downward wind revision
means the system is short, which is precisely when up-regulation clears high. Both are
derivable from a single row. Note the `ForecastCurrent` trap in §1.3.

**`ElectricityProdex5MinRealtime`** — realised production and exchange, 5-min. *History from
2014-12-31.*
Fields: `Minutes5UTC`, `PriceArea`, `ProductionLt100MW`, `ProductionGe100MW`,
`OffshoreWindPower`, `OnshoreWindPower`, `SolarPower`, `ExchangeGreatBelt`, `ExchangeGermany`,
`ExchangeNetherlands`, `ExchangeGreatBritain`, `ExchangeNorway`, `ExchangeSweden`, `BornholmSE4`.
Why: gives realised output to difference against `Forecasts_Hour` (→ live forecast error, the
key activation-price predictor) and every interconnector flow. Interconnectors at their limit
is the classic mechanism by which DK1 decouples and balancing prices spike.

**`AfrrBorderAvailableTransferCapacity`** — PICASSO cross-border capacity. *90-day retention —
see §1.2.*
Fields: `TimeMsUTC`, `Direction` (Import/Export), `BorderEIC`, `BorderName` (e.g. `DK1-DE`),
`Limit`, `Source`.
Why: the direct mechanical explanation for aFRR energy price spikes. When border ATC is
exhausted, DK cannot import cheap aFRR energy and the local price separates from the PICASSO
merit order. High-signal, near-causal feature — not a correlate.

### Tier 2 — clear value, add after Tier 1 proves out

**`GenerationProdTypeExchange`** — hourly generation by fuel type plus exchange. *From
2014-12-31.* Has a `Version` field (`Initial` → revised), which is a **real publication-revision
signal** — the thing every milestone since M1 has wanted and approximated with `fetched_at`.
Worth a look independent of forecasting.
Fields include `GrossCon` (consumption), `OffshoreWindPower`, `OnshoreWindPower`, `SolarPower`,
`Biomass`, `Biogas`, `Waste`, `FossilGas`, `FossilOil`, `FossilHardCoal`, exchange columns.
Why: thermal-versus-renewable mix proxies which units are online and therefore who can offer
reserve. Fossil share is a decent stand-in for available conventional flexibility.

**`CO2EmisProg`** — CO₂ emission *prognosis*, 5-min, forward-looking. *From 2016-12-31.*
Fields: `Minutes5UTC`, `PriceArea`, `CO2Emission`.
Why: genuinely forward-looking (samples extend ~1 day past now), so it is a legitimate
ex-ante feature. Encodes Energinet's own expectation of the thermal dispatch mix in one number.

**`FFRdemandDK2` / `FFRDK2`** — already in `shared/datasets.py`. No new ingestion; flagging for
feature design: FFR and FCR-D are partial substitutes in low-inertia hours. Cross-market
features (FFR demand, plus the already-ingested `InertiaNordicSyncharea`) should carry real
signal for FCR-D price — low Nordic inertia raises the value of fast reserve.

**`CountertradeIntraday_v2`** — `TimeUTC`, `Version`, `VolumeUpMW`, `VolumeDownMW`,
`PublicationDate`. *From 2025-10-14.* Internal congestion signal; also carries an explicit
`PublicationDate`.

### Tier 3 — demand side; lower priority

`ConsumptionGridAreaHour` (grid-area granularity, needs aggregation to price area) and
`ProductionConsumptionSettlement` (rich but settlement-lagged — latest record was 9 days
stale at audit time, so it is a training-time feature only, never available at bid time).

### Explicit non-recommendations

- **`ForeignExchange` is not currency data.** Despite the name it is physical cross-border
  power exchange in MWh (`ExchangeImportSE_MWh`, `ExchangeExportGE_MWh`, …). Given commit
  `ae42bde` just fixed a DKK/EUR conflation, this name is a live trap — do not wire it in as
  an FX source.
- **EDS publishes no FX rate.** Cross-currency comparison (DK2 FCR in EUR/MW/h versus DK2
  aFRR capacity in DKK/MW/h) needs an external source — Danmarks Nationalbank or ECB daily
  reference. DKK is ERM II pegged at 7.46 ±2.25%, so a pinned constant is defensible for
  backtesting, but it must be an explicit, logged constant, not a hardcoded literal. This
  matters directly for the §0 allocation output, which requires one common currency.
- **Discontinued predecessors: ingest them, but do not naively concatenate.** `Elspotprices`
  and `RegulatingBalancePowerdata` do still serve full history back to 1999 and splice
  cleanly with their successors (§1.1). Worth ingesting as separate, clearly-named series.
  But joining them into one continuous training frame across the Mar-2025 redesign is a
  regime-break hazard — see §1.1 for how to handle it.
- **Everything gas** (~20 datasets: `GasComposition`, `Gasflow`, `Storage*`, …), plus the
  `Declaration*` family (retrospective emissions accounting), plus municipality/DSO/DataHub
  datasets. Out of scope.
- **Not in EDS at all:** German FCR Cooperation auction results (regelleistung.net) — the
  clearing mechanism for DK1 FCR, and the README's own open question. `FcrDK1` gives realised
  prices only, not the auction inputs. Any serious DK1 FCR model needs regelleistung.net as an
  external dependency. Scope that separately; do not let it block DK2 work, where DK2 FCR-D is
  both the better-documented market and the larger BESS revenue pool.

---

## 3. Forecast targets

| # | Target | Source | Granularity | Decision it drives |
|---|---|---|---|---|
| 1 | FCR-D up/down capacity price DK2 | `FcrNdDK2` | Hourly, D-1 auction | Bid price and volume |
| 2 | aFRR capacity price DK1/DK2 | `AfrrReservesNordic` | Hourly, D-1 | Bid price and volume |
| 3 | Day-ahead price | `DayAheadPrices` | 15-min | Arbitrage; opportunity-cost anchor |
| 4 | aFRR energy activation price | `AfrrEnergyActivation` | ms → aggregate | Expected activation revenue |
| 5 | Imbalance price, and its spread to day-ahead | `ImbalancePrice` | 15-min | Imbalance exposure |
| 6 | FCR DK1 | `FcrDK1` | Hourly | Bid — *blocked on regelleistung.net, see §2* |

Start with **#1 and #3**. #1 is the largest DK BESS revenue pool with the longest label
history (4.7 years) — the only target where history is not the binding constraint. #3 anchors
opportunity cost for every other market, so it is needed regardless.

**Forecast distributions, not point estimates.** A capacity bid is a decision under an
asymmetric loss: bid too high and you clear nothing, bid too low and you leave money on the
table having committed the asset. What the bidder needs is P(clearing price > bid) — a
quantile function. Train quantile models (LightGBM `objective='quantile'` at, say, τ ∈ {0.1,
0.25, 0.5, 0.75, 0.9}) and evaluate with **pinball loss**. A point forecast scored by MAE
optimises for the wrong thing and will produce confidently wrong bids.

---

## 4. Phasing

### P0 — Archival ingestion (urgent, gates everything; ~1-2 days)
Start immediately for the §1.2 reason.
1. Rate limiter + 429-aware retry in `shared/base_ingestor.py` (§1.4).
2. Add `Forecasts_Hour`, `ElectricityProdex5MinRealtime`, `AfrrBorderAvailableTransferCapacity`
   to `shared/datasets.py` as `DatasetConfig` entries, following the existing pattern. Note
   `Forecasts_Hour` needs `filter_field="ForecastType"` per-series handling — the `FcrNdDK2`
   entry (`shared/datasets.py:393`) is the precedent to copy, since it already does exactly
   this with `ProductName`.
3. Backfill: `Forecasts_Hour` and `ElectricityProdex5MinRealtime` to 2025-01-01 (a buffer
   before the Mar-2025 regime break); the aFRR ms-datasets to their full 90-day window.
4. Extend the existing poller schedule so the 90-day-retention datasets are captured
   continuously from now on.
5. Lower priority within P0, but cheap and one-off (these are frozen, discontinued datasets —
   backfill once, never poll): `Elspotprices` and `RegulatingBalancePowerdata` as separate
   series, for the pre-break history in §1.1. Confirm the `RegulatingBalancePowerdata` end
   date splices with `ImbalancePrice` before trusting the join.

*Acceptance:* all three datasets present in `market_data_history`; row counts within 5% of
expected given granularity and window; `pytest tests/test_datasets.py` green; a re-run of
backfill is idempotent (existing `published_at` handling covers this).

### P1 — Feature store (~2-3 days)
One wide table keyed `(zone, mtu_start)` with a **leak-safe as-of join** as its defining
property: every feature column carries the horizon at which it was known, and the builder
takes a horizon parameter and physically cannot emit a column unavailable at that horizon.
`ForecastCurrent` should be droppable at load time.

Derived features worth building explicitly:
- Forecast revisions: `ForecastIntraday - ForecastDayAhead`, `Forecast1Hour - Forecast5Hour`
- Realised forecast error: `ElectricityProdex5MinRealtime.OffshoreWindPower - Forecasts_Hour.Forecast1Hour`
- Residual load: `GrossCon - (wind + solar)`
- Interconnector headroom and saturation flags per border
- Calendar: hour, day-of-week, holiday, and the D-1 auction gate time

*Acceptance:* a unit test that asserts a horizon-`h` feature frame contains no column
derived from data published after `mtu_start - h`. This test is the whole point of P1 — write
it first.

### P2 — Baselines (~1 day; do not skip)
Seasonal naive (same hour, previous day / previous week), and a day-ahead-anchored regression.
Publish their pinball loss as the bar. **Most published price-forecast work fails to beat
lagged persistence plus hour-of-day dummies.** If P3 does not clear these numbers, the answer
is to stop, not to add features.

### P3 — Models (~1 week per target)
Quantile LightGBM per target, walk-forward expanding-window CV, per-zone. Report pinball loss
by quantile against the P2 baseline.

### P4 — Economic evaluation
This is where the repo has an unusual advantage. `shared/bess_simulator.py` already backtests
dispatch and revenue across the eligible markets. Close the loop:
**forecast → bid policy → `bess_simulator` → realised revenue.**
The headline metric should be **DKK captured versus a perfect-foresight ceiling and versus the
P2 baseline**, not MAE. A model that improves MAE by 8% while losing money on bids is a failed
model, and only the economic metric surfaces that.

---

## 5. Open questions for the operator

1. **Which asset?** Bid sizing, cycling costs and the FCR-D-versus-aFRR trade-off all depend
   on MW/MWh rating and degradation cost. §0's allocation output cannot be specified without
   this.
2. **DK1, DK2, or both?** DK2 has better data (FCR-D via `FcrNdDK2`, 4.7 years) and no
   external-dependency blocker. DK1 FCR needs regelleistung.net. Recommend DK2 first.
3. **Is the decision D-1 auction bidding, or intraday activation positioning?** These need
   different horizons, different features, and different targets. D-1 is the more tractable
   starting point.
4. **Confirm the forecast layer stays separate from the M5 Morning Brief's qualitative
   outlook.** §1.1 says the quarter/year narrative forecasts should remain non-numeric; a
   numeric D-1 model should be a distinct product surface, not folded into that narrative.
