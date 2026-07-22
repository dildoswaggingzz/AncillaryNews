# M6 P2 baseline results: the pinball-loss bar P3 must clear

Generated 2026-07-21T21:28:11.473721+00:00 by `scripts/generate_baseline_report.py` against the live
database (`docs/forecast-baseline-design.md` §6). FCR-D DK2 capacity price, both
directions, walk-forward CV (90-day minimum initial train span, 30-day test folds,
30-day step -- design §4). Every baseline's parameters are fit per fold, on that
fold's training window only (`shared/baselines.py`).

## Window

**Common window for both products**: `[2021-12-31, 2026-07-21]`,
52 walk-forward folds, identical for `up` and `down`. `down` has no data for
2021-12-01..2021-12-30 inclusive (the design doc's §1 claim of "0 missing days of 1693"
is true for `up`, false for `down` -- caught live by this module's own §5 coverage gate,
run strictly against this window before anything below was fit). The window starts at
`down`'s true first day rather than `up`'s, costing `up` ~30 days (1.8% of its history),
so every number below is directly comparable fold-for-fold across both products.

## FCR-D DK2 has undergone an order-of-magnitude structural decline

Battery-fleet growth has been cannibalising a market with a fixed TSO demand volume.
Mean clearing price by year, computed directly from the series scored below (not a
separate query):

| year | `up` mean (n) | `down` mean (n) |
|---|---|---|
| 2021 | €55.15 (n=24) | €34.97 (n=1) |
| 2022 | €63.16 (n=8755) | €32.15 (n=8755) |
| 2023 | €38.47 (n=8755) | €70.47 (n=8754) |
| 2024 | €10.46 (n=8778) | €26.97 (n=8778) |
| 2025 | €6.07 (n=8755) | €5.88 (n=8754) |
| 2026 | €5.30 (n=4845) | €3.22 (n=4845) |

(n) is the point count backing each mean -- 2021 is a single partial day
(2021-12-31 only, since that's the common window's start) and
2026 is partial too (through 2026-07-21); neither is a full-year figure.

`up` fell 92% from 2022 (first full year in this window) to
2026. This means **long history is not straightforwardly an asset for level
prediction** here -- a baseline (or a P3 model) that trains on the full history without
accounting for the trend will be biased high in recent, deployment-relevant conditions.
This motivates both the headline/full-history split and the B2 expanding/rolling split
below.

## Headline bar (trailing 12 months of folds) -- the number P3 must clear

Scored on `trailing_folds(folds, timedelta(days=365))` only -- see `shared/baselines.py`.
This is the deployment-relevant bar; the full-history table further down is NOT the bar.

| baseline | product | τ=0.1 | τ=0.25 | τ=0.5 | τ=0.75 | τ=0.9 | fold count |
|---|---|---|---|---|---|---|---|
| B1 seasonal-naive (t-24h) | up | 1.3672 | 1.7954 | 1.9690 | 1.8223 | 1.3905 | 12 |
| B1 seasonal-naive (t-24h) | down | 1.3893 | 1.4250 | 1.3578 | 1.3619 | 1.4262 | 12 |
| B1 seasonal-naive (t-168h) | up | 1.6751 | 2.1496 | 2.2898 | 2.0645 | 1.7574 | 12 |
| B1 seasonal-naive (t-168h) | down | 2.2211 | 2.0158 | 1.5354 | 1.5514 | 2.2618 | 12 |
| B2 conditional climatology (expanding) | up | 0.7162 | 2.2700 | 9.3724 | 11.7978 | 6.7493 | 12 |
| B2 conditional climatology (expanding) | down | 0.8149 | 2.8366 | 7.8321 | 13.1261 | 8.2365 | 12 |
| B2 conditional climatology (rolling 180d) | up | 0.5123 | 1.0891 | 1.7213 | 1.8630 | 1.6893 | 12 |
| B2 conditional climatology (rolling 180d) | down | 0.3685 | 0.7251 | 1.1669 | 1.3269 | 1.1972 | 12 |

## Secondary: full-history average (2021-12-31 to present) -- NOT the bar

Spans the regime change documented above -- dominated by a market that no longer exists.
Reported for completeness/context only; a P3 model should not be judged against this row.

| baseline | product | τ=0.1 | τ=0.25 | τ=0.5 | τ=0.75 | τ=0.9 | fold count |
|---|---|---|---|---|---|---|---|
| B1 seasonal-naive (t-24h) | up | 1.5684 | 1.9947 | 2.1958 | 2.1100 | 1.7173 | 52 |
| B1 seasonal-naive (t-24h) | down | 4.5867 | 5.2343 | 5.4782 | 5.4062 | 4.9003 | 52 |
| B1 seasonal-naive (t-168h) | up | 2.4446 | 3.3306 | 3.7876 | 3.6352 | 2.8459 | 52 |
| B1 seasonal-naive (t-168h) | down | 6.5620 | 7.9263 | 8.3733 | 8.2273 | 7.2511 | 52 |
| B2 conditional climatology (expanding) | up | 6.7692 | 10.1908 | 14.8142 | 13.1801 | 7.8491 | 52 |
| B2 conditional climatology (expanding) | down | 4.2125 | 9.1707 | 16.3902 | 19.9422 | 15.7940 | 52 |
| B2 conditional climatology (rolling 180d) | up | 2.3832 | 4.6352 | 6.6695 | 6.1974 | 4.4721 | 52 |
| B2 conditional climatology (rolling 180d) | down | 3.3618 | 7.3405 | 11.7913 | 12.5960 | 9.6212 | 52 |

## Per-fold pinball loss over time (quantile-averaged)

One row per walk-forward fold's test window, so the regime shift documented above is
visible directly in the loss numbers, not only in the yearly-means table. The last 12
rows are exactly the folds the headline bar above is computed from.

| fold test_start | B1 seasonal-naive (t-24h) / up | B1 seasonal-naive (t-24h) / down | B1 seasonal-naive (t-168h) / up | B1 seasonal-naive (t-168h) / down | B2 conditional climatology (expanding) / up | B2 conditional climatology (expanding) / down | B2 conditional climatology (rolling 180d) / up | B2 conditional climatology (rolling 180d) / down |
|---|---|---|---|---|---|---|---|---|
| 2022-03-31 | 2.1335 | 2.6596 | 4.1664 | 6.1818 | 3.6846 | 6.3498 | 3.6846 | 6.3498 |
| 2022-04-30 | 3.8619 | 7.5523 | 7.7460 | 12.8731 | 28.3161 | 16.7957 | 28.3161 | 16.7957 |
| 2022-05-30 | 3.9390 | 2.7020 | 9.8568 | 9.5839 | 35.5091 | 12.2907 | 35.5091 | 12.2907 |
| 2022-06-29 | 1.0520 | 0.8994 | 4.5635 | 2.0445 | 10.1108 | 3.8904 | 10.1108 | 3.8904 |
| 2022-07-29 | 2.1018 | 0.8596 | 4.5012 | 1.7253 | 7.9719 | 3.7461 | 7.6464 | 3.8226 |
| 2022-08-28 | 3.7102 | 4.1122 | 5.0019 | 4.5985 | 11.2859 | 3.8492 | 9.9675 | 4.0169 |
| 2022-09-27 | 5.3251 | 12.5564 | 9.2008 | 30.3325 | 9.8144 | 28.8265 | 7.5027 | 25.9081 |
| 2022-10-27 | 2.0326 | 3.9799 | 2.6582 | 5.6906 | 5.6710 | 9.3472 | 7.2964 | 7.4660 |
| 2022-11-26 | 3.9839 | 1.2398 | 6.8598 | 2.7067 | 7.9511 | 4.8726 | 6.7765 | 5.8799 |
| 2022-12-26 | 1.8125 | 18.9330 | 2.2870 | 22.9272 | 3.6952 | 15.7197 | 8.5454 | 15.5045 |
| 2023-01-25 | 0.9548 | 2.4905 | 2.3652 | 3.3552 | 2.9301 | 4.8369 | 12.2154 | 4.3226 |
| 2023-02-24 | 2.0437 | 3.8993 | 3.6227 | 3.7996 | 7.4683 | 6.5491 | 7.3520 | 4.6479 |
| 2023-03-26 | 1.3155 | 6.4870 | 3.2695 | 9.7643 | 6.9503 | 11.3334 | 3.2840 | 9.0659 |
| 2023-04-25 | 1.7035 | 10.2846 | 2.8297 | 14.2442 | 6.7251 | 21.3890 | 9.0382 | 27.9239 |
| 2023-05-25 | 0.9631 | 13.8975 | 2.4373 | 20.8604 | 24.3391 | 28.0267 | 3.5294 | 30.2728 |
| 2023-06-24 | 0.6011 | 11.2511 | 1.4488 | 22.5307 | 13.0956 | 76.4027 | 3.2867 | 49.4118 |
| 2023-07-24 | 1.3550 | 9.8776 | 4.1653 | 18.8037 | 6.9462 | 57.5702 | 3.8873 | 18.7309 |
| 2023-08-23 | 0.7066 | 4.0840 | 1.9733 | 7.1218 | 21.2591 | 17.8038 | 3.8698 | 15.7953 |
| 2023-09-22 | 0.4291 | 1.9905 | 1.3565 | 4.4371 | 22.3188 | 10.7458 | 9.2513 | 15.5591 |
| 2023-10-22 | 1.4819 | 2.1583 | 2.2914 | 3.8413 | 14.3801 | 14.0314 | 4.3266 | 25.0572 |
| 2023-11-21 | 5.3426 | 4.7483 | 13.1984 | 7.1976 | 11.2267 | 8.3338 | 7.6694 | 16.1124 |
| 2023-12-21 | 2.0627 | 3.2460 | 3.6605 | 7.5113 | 11.5459 | 5.8707 | 6.8163 | 12.3503 |
| 2024-01-20 | 4.1280 | 10.3079 | 4.5919 | 17.8875 | 6.1107 | 11.5899 | 3.7849 | 10.0154 |
| 2024-02-19 | 1.2971 | 5.7462 | 3.2964 | 6.9517 | 3.1515 | 4.9041 | 2.9728 | 6.5949 |
| 2024-03-20 | 1.4181 | 40.0553 | 1.8092 | 45.3592 | 6.8200 | 41.2154 | 1.5491 | 46.5392 |
| 2024-04-19 | 3.1709 | 24.9659 | 4.6922 | 32.5085 | 16.0532 | 20.2877 | 5.0018 | 18.3115 |
| 2024-05-19 | 0.7444 | 5.3087 | 2.0410 | 5.8472 | 24.6557 | 16.1116 | 4.0785 | 5.8124 |
| 2024-06-18 | 1.3519 | 3.5340 | 2.1700 | 4.5961 | 16.5263 | 18.5399 | 2.1474 | 6.6425 |
| 2024-07-18 | 0.4987 | 1.4830 | 1.2036 | 2.8824 | 16.9940 | 13.9794 | 1.8163 | 4.5233 |
| 2024-08-17 | 0.8157 | 1.1147 | 1.2748 | 2.4454 | 15.1437 | 11.0560 | 1.0640 | 3.5877 |
| 2024-09-16 | 1.0370 | 1.5950 | 1.4682 | 2.4219 | 11.2188 | 12.5032 | 1.0266 | 2.7400 |
| 2024-10-16 | 1.2771 | 1.1931 | 1.5472 | 2.3929 | 9.6880 | 9.2385 | 1.1456 | 1.7915 |
| 2024-11-15 | 2.4124 | 1.0412 | 2.8052 | 2.0857 | 11.5141 | 5.7522 | 1.5907 | 0.9356 |
| 2024-12-15 | 1.7351 | 1.3875 | 1.5703 | 2.1535 | 9.5671 | 5.9440 | 1.0473 | 0.7324 |
| 2025-01-14 | 2.3805 | 1.3823 | 2.5983 | 2.1244 | 7.0434 | 5.4500 | 1.6070 | 0.7161 |
| 2025-02-13 | 1.6072 | 1.5767 | 2.5780 | 2.3487 | 4.7314 | 3.5094 | 2.0350 | 1.0921 |
| 2025-03-15 | 0.7597 | 3.5430 | 2.4482 | 4.2481 | 7.0887 | 7.8255 | 1.1538 | 3.0751 |
| 2025-04-14 | 4.5878 | 4.4475 | 4.9790 | 5.3955 | 11.2552 | 16.0618 | 3.2249 | 2.8872 |
| 2025-05-14 | 0.7777 | 9.5565 | 1.3860 | 9.9434 | 14.3856 | 16.5428 | 1.0001 | 5.4104 |
| 2025-06-13 | 0.7593 | 1.5947 | 1.1293 | 2.3688 | 9.8902 | 13.8177 | 0.6726 | 1.0972 |
| 2025-07-13 | 0.7560 | 0.9696 | 1.1971 | 1.7897 | 7.2537 | 11.6018 | 0.7980 | 0.6407 |
| 2025-08-12 | 1.2023 | 1.0915 | 1.6404 | 1.8480 | 7.7359 | 6.6823 | 1.1377 | 1.0306 |
| 2025-09-11 | 1.2756 | 3.7001 | 1.6683 | 4.0739 | 6.7034 | 6.0468 | 1.1927 | 2.3709 |
| 2025-10-11 | 0.9665 | 0.6908 | 1.3604 | 1.5214 | 5.2819 | 6.0497 | 0.9110 | 1.0032 |
| 2025-11-10 | 1.9294 | 0.7898 | 2.0569 | 1.4659 | 5.3392 | 3.2629 | 1.8006 | 0.5613 |
| 2025-12-10 | 1.0382 | 0.6306 | 1.4491 | 1.3037 | 6.4114 | 3.4575 | 1.2485 | 0.3379 |
| 2026-01-09 | 2.8466 | 0.5928 | 2.8755 | 1.2499 | 4.2423 | 4.3375 | 2.2878 | 0.1753 |
| 2026-02-08 | 2.4902 | 0.8443 | 3.5661 | 1.4148 | 3.2381 | 2.0520 | 2.0443 | 0.6819 |
| 2026-03-10 | 0.8664 | 1.5592 | 1.1821 | 2.0153 | 3.7760 | 4.4492 | 0.7062 | 1.1955 |
| 2026-04-09 | 1.0857 | 1.1676 | 1.2196 | 1.5163 | 7.3776 | 10.5325 | 0.9126 | 0.6063 |
| 2026-05-09 | 2.2523 | 1.3485 | 1.9071 | 1.7035 | 8.9473 | 10.6815 | 1.4390 | 0.9288 |
| 2026-06-08 | 3.3132 | 3.3181 | 3.7210 | 3.1019 | 7.8638 | 9.6745 | 2.0205 | 1.9507 |

