"""
Unit/currency lookup for `(market, zone, product)` keys, derived entirely
from `shared/datasets.py`'s `DATASETS` registry -- not a database column
(see that module's `SeriesConfig.unit` docstring for the full "why no DB
column" rationale: unit is a static, code-reviewed fact about how a series
was ingested, not something any individual `market_data_history` row
carries or could carry without a migration that wouldn't even run on an
existing deployment, per `init-db/01-init.sql`'s revision-aware design and
Stage 0's migration-runner fix).

This module exists to close one specific, live defect: `shared/
bess_simulator.py` and `shared/rule_engine.py` both compare/sum values
across `(market, zone, product)` keys with no way to know those values are
denominated in different currencies -- e.g. DK1's `("FCR", "price")` is
DKK/MW/h while DK2's is EUR/MW/h (see `shared/datasets.py`'s `fcr_dk2`
comment). `unit_for`/`currency_for`/`same_currency` give every caller that
needs to compare or combine two series a single, registry-backed way to ask
"is this even the same unit?" before doing so.

**Resolution order:** exact `(market, zone, product)` first, then the
zone-agnostic `(market, None, product)` entry. A dataset's series is
registered zone-agnostically (key's zone is `None`) whenever its zone
varies per ingested record (`DatasetConfig.zone_field` is set, e.g.
`"PriceArea"`) -- the registry entry itself doesn't pin down one zone, so it
correctly applies to every zone that dataset's records carry. A series is
registered under its exact zone only when the *dataset* (via a fixed
`zone_field=None, zone=...`, e.g. `fcr_dk1`'s `zone="DK1"`) or the *series*
itself (via `SeriesConfig.zone`, e.g. a future zone-heterogeneous dataset's
per-zone override) pins one zone down. This is exactly what makes
`fcr_dk1` (exact zone "DK1", DKK/MW/h) and `fcr_dk2` (zone-agnostic, EUR/MW/h)
resolve to different units for `("FCR", "price")` depending on the zone
asked about, despite sharing the same `market`/`product` -- see
`tests/test_units.py`'s first test, which is the case this index has to get
right.

**Index built once at import**, from the registry, not lazily per call --
`DATASETS` is a static module-level list (no runtime mutation), so there's
no staleness risk, and every caller (`shared/bess_simulator.py`'s
per-tick capacity-revenue loop especially) needs this to be a cheap dict
lookup, not a fresh registry walk every time.
"""

from __future__ import annotations

from shared.datasets import DATASETS, DatasetConfig, SeriesConfig

# (market, zone-or-None, product) -> unit string. `None` in the zone slot
# means "zone-agnostic" -- see module docstring's "Resolution order".
_UnitKey = tuple[str, str | None, str]

# Fixed ERM II central-rate peg (DKK is pegged to EUR at ~7.46038, held
# inside a +/-2.25% band, +/-0.5% in practice) -- an accounting convenience
# for *labelled, combined* headline totals only (see
# `shared/bess_dispatch_milp.py`'s module docstring and
# `shared/bess_simulator.py:BacktestResult.total_revenue_all_dkk`/
# `total_revenue_all_eur`). Never used to compare or trade a EUR figure
# against a DKK figure inside any optimization objective or per-currency
# revenue bucket -- those stay unconverted and separate (this module's
# whole reason for existing, see module docstring). A *fixed* policy peg is
# not the same class of bug as the floating-market-price mixing this module
# guards against elsewhere, since it is not a market variable that could
# silently drift; it is surfaced explicitly wherever it is used ("converted
# at fixed 7.46 DKK/EUR"), alongside the raw per-currency buckets, never in
# place of them.
DKK_PER_EUR = 7.46


def _effective_zone(dataset: DatasetConfig, series: SeriesConfig) -> str | None:
    """
    The zone this one series' registry entry should be keyed under, or
    `None` for a zone-agnostic entry (see module docstring). `series.zone`
    (a per-series override) wins if set; otherwise falls back to the
    dataset's own zone resolution -- a fixed zone if `zone_field is None`,
    otherwise `None` (the dataset's records carry their own zone, so no
    single zone applies to the registry entry itself).
    """
    if series.zone is not None:
        return series.zone
    if dataset.zone_field is None:
        return dataset.zone
    return None


def _build_index() -> dict[_UnitKey, str]:
    """
    Walks `DATASETS` once, building the `(market, zone_or_None, product) ->
    unit` index this module resolves against. Raises `ValueError` at import
    time (not silently overwrites) if two registry entries claim the same
    key -- a genuine registry bug (e.g. a copy-paste product name collision)
    should fail loudly at startup, the same posture
    `shared/bess_simulator.py`'s `run_backtest` takes on an unlabelled
    capacity leg (see that module's docstring).
    """
    index: dict[_UnitKey, str] = {}
    for dataset in DATASETS:
        for series in dataset.series:
            market = series.market or dataset.market
            key = (market, _effective_zone(dataset, series), series.product)
            if key in index:
                raise ValueError(
                    f"duplicate (market, zone, product) key {key!r} in the dataset registry "
                    "(shared/datasets.py) -- shared/units.py's index requires every "
                    "(market, zone_or_None, product) triple to resolve to exactly one unit"
                )
            index[key] = series.unit
    return index


_UNIT_INDEX: dict[_UnitKey, str] = _build_index()


def unit_for(market: str, zone: str, product: str) -> str | None:
    """
    Returns the declared unit for `(market, zone, product)` (e.g.
    "DKK/MW/h", "EUR/MWh", "MW"), or `None` if no registry entry covers this
    key at all (as opposed to `"unknown"`, which means a real registry entry
    exists but was never annotated -- see `shared/datasets.py`'s
    `SeriesConfig.unit` docstring). Resolves the exact zone first, then the
    zone-agnostic entry -- see module docstring.
    """
    exact = _UNIT_INDEX.get((market, zone, product))
    if exact is not None:
        return exact
    return _UNIT_INDEX.get((market, None, product))


def currency_for(market: str, zone: str, product: str) -> str | None:
    """
    Returns "DKK" or "EUR" for a monetary series, or `None` for a
    non-monetary series (e.g. "MW", "g/kWh") or an unregistered key.
    Derived from the full unit string's prefix (not a separate stored
    field) -- see `SeriesConfig.unit`'s docstring for why currency is never
    modeled as its own dimension: a capacity price (.../MW/h) and an energy
    price (.../MWh) sharing a currency are still not directly comparable,
    so `same_currency` below intentionally only answers the currency
    question, not the full unit-equality one.
    """
    unit = unit_for(market, zone, product)
    if unit is None:
        return None
    if unit.startswith("DKK"):
        return "DKK"
    if unit.startswith("EUR"):
        return "EUR"
    return None


def same_currency(*keys: tuple[str, str, str]) -> bool:
    """
    True if every `(market, zone, product)` key resolves to the same
    currency (including the trivial case of every key being non-monetary,
    e.g. comparing two MW series -- there's no DKK/EUR mixing risk there).
    False as soon as two keys disagree, whether that's DKK vs EUR or a
    monetary vs non-monetary/unregistered key.

    The one caller today is `shared/rule_engine.py`'s zone-divergence guard
    (`not same_currency((market, "DK1", product), (market, "DK2", product))`)
    -- this is deliberately a *policy* function callers reach for, not
    something `check_zone_divergence` itself is aware of, so
    `shared/rule_engine.py` stays free of any market-name literals (it never
    needs to know FCR is the problematic case; it just asks this function).
    """
    currencies = {currency_for(*key) for key in keys}
    return len(currencies) <= 1
