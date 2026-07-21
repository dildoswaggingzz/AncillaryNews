# M6 P3 model results: quantile LightGBM vs the P2 bar

Generated 2026-07-21T22:03:23.190003+00:00 by `scripts/generate_forecast_report.py` against the live
database (`docs/forecast-model-design.md` §6). FCR-D DK2 capacity price, both
directions, walk-forward CV -- identical fold generator/config to P2
(`shared/baselines.py`'s `walk_forward_folds`: 90-day minimum initial train span,
30-day test folds, 30-day step), evaluated on the trailing-12-month headline window
(`trailing_folds(folds, timedelta(days=365))`) -- the same window as
`docs/forecast-baseline-results.md`. Per-fold refit throughout: the model is a fresh
LightGBM quantile fit per fold, on that fold's own bounded trailing lookback window
only (design §3), never global.

**Window**: headline folds span `[2021-12-31, 2026-07-08]`, 12 folds. Feature fetch
window: `[2024-01-12, 2026-07-21]` (bounded to the longest declared
lookback before the earliest headline fold). Target fetch window (for B1's own
expanding fit, and for the model's dataset): the FULL common window, `[2021-12-31, 2026-07-21]` -- see module docstring.

## Headline: model vs the bar (`min(B1 t-24h, B1 t-168h, B2-rolling)`), per (product, τ)

`beats_bar` is `yes` only if the model's pinball loss is strictly lower than the
strongest of the three bar baselines, for that exact (product, τ, lookback).

| product | τ | lookback | model pinball | bar baseline | bar pinball | beats_bar |
|---|---|---|---|---|---|---|
| up | 0.1 | 12mo | 0.4737 | B2 conditional climatology (rolling 180d) | 0.5123 | yes |
| up | 0.1 | 18mo | 0.4461 | B2 conditional climatology (rolling 180d) | 0.5123 | yes |
| up | 0.25 | 12mo | 1.1141 | B2 conditional climatology (rolling 180d) | 1.0891 | no |
| up | 0.25 | 18mo | 1.0631 | B2 conditional climatology (rolling 180d) | 1.0891 | yes |
| up | 0.5 | 12mo | 1.8434 | B2 conditional climatology (rolling 180d) | 1.7213 | no |
| up | 0.5 | 18mo | 1.7279 | B2 conditional climatology (rolling 180d) | 1.7213 | no |
| up | 0.75 | 12mo | 1.9701 | B1 seasonal-naive (t-24h) | 1.8223 | no |
| up | 0.75 | 18mo | 1.8502 | B1 seasonal-naive (t-24h) | 1.8223 | no |
| up | 0.9 | 12mo | 1.4899 | B1 seasonal-naive (t-24h) | 1.3905 | no |
| up | 0.9 | 18mo | 1.4867 | B1 seasonal-naive (t-24h) | 1.3905 | no |
| down | 0.1 | 12mo | 0.3010 | B2 conditional climatology (rolling 180d) | 0.3685 | yes |
| down | 0.1 | 18mo | 0.3194 | B2 conditional climatology (rolling 180d) | 0.3685 | yes |
| down | 0.25 | 12mo | 0.7028 | B2 conditional climatology (rolling 180d) | 0.7251 | yes |
| down | 0.25 | 18mo | 0.7575 | B2 conditional climatology (rolling 180d) | 0.7251 | no |
| down | 0.5 | 12mo | 1.2314 | B2 conditional climatology (rolling 180d) | 1.1669 | no |
| down | 0.5 | 18mo | 1.4126 | B2 conditional climatology (rolling 180d) | 1.1669 | no |
| down | 0.75 | 12mo | 1.3985 | B2 conditional climatology (rolling 180d) | 1.3269 | no |
| down | 0.75 | 18mo | 1.6559 | B2 conditional climatology (rolling 180d) | 1.3269 | no |
| down | 0.9 | 12mo | 1.1778 | B2 conditional climatology (rolling 180d) | 1.1972 | yes |
| down | 0.9 | 18mo | 1.4939 | B2 conditional climatology (rolling 180d) | 1.1972 | no |

**Overall**: 7/20 (product, τ, lookback) cells beat the bar.

## Verdict

- **up / 12mo lookback**: does NOT beat the bar (1/5 τ)
- **up / 18mo lookback**: does NOT beat the bar (2/5 τ)
- **down / 12mo lookback**: does NOT beat the bar (3/5 τ)
- **down / 18mo lookback**: does NOT beat the bar (1/5 τ)

## Model, full detail (both lookbacks)

| lookback | product | τ=0.1 | τ=0.25 | τ=0.5 | τ=0.75 | τ=0.9 | fold count |
|---|---|---|---|---|---|---|---|
| 12mo | up | 0.4737 | 1.1141 | 1.8434 | 1.9701 | 1.4899 | 12 |
| 12mo | down | 0.3010 | 0.7028 | 1.2314 | 1.3985 | 1.1778 | 12 |
| 18mo | up | 0.4461 | 1.0631 | 1.7279 | 1.8502 | 1.4867 | 12 |
| 18mo | down | 0.3194 | 0.7575 | 1.4126 | 1.6559 | 1.4939 | 12 |

## Baselines, recomputed fresh on this run's exact headline folds (for reference)

B1 (both lags) and B2-rolling only -- the three bar candidates (design §0/§6). B2-
expanding is not recomputed here: P2 already established it as a strawman (trained
across FCR-D DK2's ~92% price collapse) and it never contributes to `beats_bar`; see
`docs/forecast-baseline-results.md` for its numbers on the full historical window.

| baseline | product | τ=0.1 | τ=0.25 | τ=0.5 | τ=0.75 | τ=0.9 | fold count |
|---|---|---|---|---|---|---|---|
| B1 seasonal-naive (t-24h) | up | 1.3672 | 1.7954 | 1.9690 | 1.8223 | 1.3905 | 12 |
| B1 seasonal-naive (t-24h) | down | 1.3893 | 1.4250 | 1.3578 | 1.3619 | 1.4262 | 12 |
| B1 seasonal-naive (t-168h) | up | 1.6751 | 2.1496 | 2.2898 | 2.0645 | 1.7574 | 12 |
| B1 seasonal-naive (t-168h) | down | 2.2211 | 2.0158 | 1.5354 | 1.5514 | 2.2618 | 12 |
| B2 conditional climatology (rolling 180d) | up | 0.5123 | 1.0891 | 1.7213 | 1.8630 | 1.6893 | 12 |
| B2 conditional climatology (rolling 180d) | down | 0.3685 | 0.7251 | 1.1669 | 1.3269 | 1.1972 | 12 |

