# M6 P3b day-ahead model results: quantile LightGBM vs the P2/P3-style bar

Generated 2026-07-22T09:29:04.281355+00:00 by `scripts/generate_day_ahead_forecast_report.py` against the
live database (`docs/forecast-day-ahead-design.md`). DK2 day-ahead energy price, a single
series (not directional) -- retargeted through the same fold generator/pinball loss/
coverage gate/baselines `shared/baselines.py`'s FCR-D report uses, generalised via
`shared.baselines.TargetConfig`/`DAY_AHEAD_TARGET`, never forked. Source is 15-minute,
aggregated to hourly by mean before anything downstream reads it (design §1's one new
data-handling step -- a v1 simplification: hourly loses day-ahead's intraday shape).

**Unit note:** `shared/datasets.py`'s registry (and `shared/units.py`'s derived index)
declares day-ahead DK2 price DKK/MWh (`DayAheadPriceDKK`), not EUR/MWh as
`docs/forecast-day-ahead-design.md` §1 states -- verified live (typical values here are
in the ~300-900 range, the DKK/MWh magnitude, not EUR/MWh's ~30-100). No currency
conversion is applied (out of scope for this retarget); every number below is DKK/MWh.

**Small-sample caveat (design §2, stated plainly):** 6 walk-forward fold(s),
not 12 -- day-ahead's usable feature history reaches back only to ~2025-09-25, so this
verdict rests on materially weaker evidence than `docs/forecast-model-results.md`'s FCR-D
result (12 headline folds). Read the verdict below with that in mind.

**Window**: headline folds span `[2025-10-01, 2026-06-28]`, 6 fold(s). Feature fetch window: `[2025-10-01, 2026-07-22]`. Target fetch window (for B1's own expanding
fit, and for the model's dataset): `[2025-10-01, 2026-07-22]`.
Lookback: 12mo (design §2 -- single declared lookback, ~all available
history; no second window scheme).

## Headline: model vs the bar (`min(B1 t-24h, B1 t-168h, B2-rolling)`), per τ

`beats_bar` is `yes` only if the model's pinball loss is strictly lower than the
strongest of the three bar baselines, for that exact τ. No B2-expanding, no B3
(design §3: day-ahead-anchored regression is meaningless when day-ahead IS the target).

| τ | model pinball | bar baseline | bar pinball | beats_bar |
|---|---|---|---|---|
| 0.1 | 60.5862 | B1 seasonal-naive (t-24h) | 64.2938 | yes |
| 0.25 | 97.4303 | B1 seasonal-naive (t-24h) | 100.2295 | yes |
| 0.5 | 125.7180 | B1 seasonal-naive (t-24h) | 117.3785 | no |
| 0.75 | 118.3004 | B1 seasonal-naive (t-24h) | 103.1739 | no |
| 0.9 | 73.6360 | B1 seasonal-naive (t-24h) | 67.0090 | no |

**Which baseline wins the bar, by τ** (worth stating explicitly, not just implied by the
table): for FCR-D (`docs/forecast-model-results.md`), B2 conditional climatology (rolling
180d) wins the bar almost everywhere. For day-ahead here, B1 seasonal-naive (t-24h) wins
every τ instead -- day-ahead has a strong, persistent day-over-day seasonal pattern
(driven by the same load/wind/solar diurnal cycle the fundamentals features also carry)
that yesterday's price already captures well, whereas FCR-D's structural collapse makes
"yesterday's price" a poor anchor and a recent conditional average the safer bet. This
is a real difference between the two markets' baseline dynamics, not a tuning artefact --
no baseline parameter here was chosen against this run's own data.

## Verdict

- **price / 12mo lookback**: does NOT beat the bar (2/5 τ)

## Baselines, full detail

| baseline | τ=0.1 | τ=0.25 | τ=0.5 | τ=0.75 | τ=0.9 | fold count |
|---|---|---|---|---|---|---|
| B1 seasonal-naive (t-24h) | 64.2938 | 100.2295 | 117.3785 | 103.1739 | 67.0090 | 6 |
| B1 seasonal-naive (t-168h) | 72.3175 | 122.8122 | 144.5639 | 126.6423 | 79.9584 | 6 |
| B2 conditional climatology (rolling 180d) | 72.5038 | 126.8778 | 149.7744 | 122.5835 | 75.3154 | 6 |

## Comparison to P3's FCR-D result (`docs/forecast-model-results.md`)

P3 found FCR-D DK2 capacity price does NOT beat `min(B1, B2-rolling)`: 7/20
(product, τ, lookback) cells beat the bar, and every one of the 4 (product, lookback)
verdicts read "does NOT beat the bar". The day-ahead design's premise (design §0) was
that FCR-D's loss is plausibly specific to its own cannibalisation by battery entry (a
capacity price collapsing under supply the fundamentals can't see) -- so this run, on a
normal fundamentals-driven energy market with no such collapse, is the cleaner test of
the modelling approach itself. See the verdict above for which way it went here.

