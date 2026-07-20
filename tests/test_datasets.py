"""
Tests for shared/datasets.py's registry shape -- distinct from
tests/test_units.py's lookup-behavior tests and tests/test_db_manager.py's
save_market_data mapping tests, this file guards the registry's own
invariants (no duplicate keys, no un-annotated units, the frozen-snapshot
guard on shared/units.py's "never mutate an existing SeriesConfig's unit"
accepted cost -- see shared/units.py's module docstring §1.1).
"""

from shared.datasets import DATASETS, DatasetConfig, SeriesConfig


def _effective_zone(dataset: DatasetConfig, series: SeriesConfig) -> str | None:
    """Mirrors shared/units.py:_effective_zone -- kept as an independent
    reimplementation here (not an import) so this test doesn't just
    tautologically re-check units.py's own logic against itself."""
    if series.zone is not None:
        return series.zone
    if dataset.zone_field is None:
        return dataset.zone
    return None


def test_no_duplicate_market_zone_product_keys_across_registry():
    """
    shared/units.py's index requires every (market, zone_or_None, product)
    triple to resolve to exactly one unit -- a duplicate here would mean
    either an ambiguous registry entry or (if units disagree) a straight-up
    registry bug. shared/units.py itself raises on this at import time; this
    test makes the invariant explicit and independently verifiable.
    """
    seen: set[tuple[str, str | None, str]] = set()
    duplicates = []
    for dataset in DATASETS:
        for series in dataset.series:
            market = series.market or dataset.market
            key = (market, _effective_zone(dataset, series), series.product)
            if key in seen:
                duplicates.append(key)
            seen.add(key)
    assert duplicates == []


def test_every_series_has_a_declared_unit():
    for dataset in DATASETS:
        for series in dataset.series:
            assert series.unit != "unknown", (
                f"{dataset.name}/{series.product} has no declared unit -- add `unit=` to its "
                "SeriesConfig in shared/datasets.py"
            )


# --- frozen (market, product) -> unit snapshot --------------------------------
#
# Guards shared/units.py §1.1's "accepted cost": since unit is a registry
# lookup (not a stored column), retroactively changing an existing
# SeriesConfig's unit would relabel already-ingested history. The mitigation
# is "never mutate an existing SeriesConfig's unit -- introduce a new
# product name instead" -- this snapshot fails loudly if that discipline is
# ever violated for a series that existed at the time this test was written.
# A *new* series/product is fine to add (this test only checks entries it
# already knows about); an existing (market, product)'s unit *changing* is
# the thing this test exists to catch.

_FROZEN_UNIT_SNAPSHOT: dict[tuple[str, str], str] = {
    ("mFRR_capacity", "up"): "DKK/MW/h",
    ("mFRR_capacity", "down"): "DKK/MW/h",
    ("aFRR_energy", "activation_price"): "EUR/MWh",
    ("aFRR_energy", "activation_volume"): "MW",
    ("mFRR_EAM", "up"): "EUR/MWh",
    ("mFRR_EAM", "down"): "EUR/MWh",
    ("mFRR_EAM", "up_volume"): "MW",
    ("mFRR_EAM", "down_volume"): "MW",
    ("mFRR_EAM", "up_total_volume"): "MW",
    ("mFRR_EAM", "down_total_volume"): "MW",
    ("mFRR_EAM", "up_offered_volume"): "MW",
    ("mFRR_EAM", "down_offered_volume"): "MW",
    ("aFRR_correction", "correction_volume"): "MW",
    ("aFRR_correction", "up"): "EUR/MWh",
    ("aFRR_correction", "down"): "EUR/MWh",
    ("imbalance", "imbalance_price"): "DKK/MWh",
    ("imbalance", "afrr_vwa_up"): "DKK/MWh",
    ("imbalance", "afrr_vwa_down"): "DKK/MWh",
    ("day_ahead", "price"): "DKK/MWh",
    ("FCR", "price"): "DKK/MW/h",  # DK1's fixed-zone entry -- see fcr_dk2 below
    ("FCR", "up"): "EUR/MW/h",  # DK2's FCR-D upp leg (zone-agnostic entry)
    ("FCR", "down"): "EUR/MW/h",  # DK2's FCR-D ned leg (zone-agnostic entry)
    ("aFRR_capacity", "up"): "DKK/MW/h",
    ("aFRR_capacity", "down"): "DKK/MW/h",
    ("system_state", "onshore_wind"): "MW",
    ("system_state", "offshore_wind"): "MW",
    ("system_state", "solar"): "MW",
    ("system_state", "co2_emission"): "g/kWh",
    # Stage 3 registry expansion.
    ("FFR", "price"): "DKK/MW/h",
    ("FFR", "price_eur"): "EUR/MW/h",
    ("FFR", "demand_volume"): "MW",
    ("FFR", "purchased_volume"): "MW",
    ("FFR", "demand_step_0"): "MW",
    ("FFR", "demand_step_7"): "MW",
    ("mFRR_capacity_extra", "up"): "DKK/MW/h",
    ("mFRR_capacity_extra", "down"): "DKK/MW/h",
    ("mFRR_capacity_extra", "up_demand_volume"): "MW",
    ("mFRR_capacity_extra", "down_procured_volume"): "MW",
    ("aFRR_capacity", "up_demand_volume"): "MW",
    ("aFRR_capacity", "down_procured_volume"): "MW",
    ("aFRR_capacity", "up_eur"): "EUR/MW/h",
    ("aFRR_capacity", "down_eur"): "EUR/MW/h",
    ("FCR", "volume"): "MW",  # DK2's FCR-N total volume (zone-agnostic entry)
    ("FCR", "up_volume"): "MW",
    ("FCR", "down_volume_local"): "MW",
    ("FCR", "d1_price"): "EUR/MW/h",
    ("FCR", "d1_up"): "EUR/MW/h",
    ("FCR", "d1_down"): "EUR/MW/h",
    ("inertia", "nordic"): "GWs",
    ("inertia", "dk2"): "GWs",
}


def test_frozen_unit_snapshot_matches_registry():
    """
    Note: `("FCR", "price")` appears once in this snapshot dict (Python dict
    keys can't repeat), holding DK1's DKK/MW/h value -- DK2's `("FCR",
    "price")` entry is EUR/MW/h and is checked separately below, since a
    plain `(market, product)` key can't distinguish the two zones (that's
    exactly why shared/units.py's real index is keyed on zone too).
    """
    actual: dict[tuple[str, str], str] = {}
    for dataset in DATASETS:
        for series in dataset.series:
            market = series.market or dataset.market
            key = (market, series.product)
            # fcr_dk1 (checked first below via DATASETS order) and fcr_dk2
            # both produce a ("FCR", "price") key -- skip fcr_dk2's since
            # it's checked separately (zone matters for this one pair).
            if key == ("FCR", "price") and dataset.name == "fcr_dk2":
                continue
            actual[key] = series.unit

    for key, expected_unit in _FROZEN_UNIT_SNAPSHOT.items():
        assert key in actual, f"{key} is missing from the registry entirely"
        assert actual[key] == expected_unit, (
            f"{key}'s unit changed from {expected_unit!r} to {actual[key]!r} -- if this is a "
            "genuine currency/unit correction, introduce a NEW product name instead of mutating "
            "the existing one (see shared/units.py's module docstring, accepted-cost mitigation) "
            "so already-ingested history under the old product name isn't retroactively relabeled"
        )


def test_dk1_and_dk2_fcr_price_are_different_frozen_units():
    fcr_dk1 = next(d for d in DATASETS if d.name == "fcr_dk1")
    fcr_dk2 = next(d for d in DATASETS if d.name == "fcr_dk2")
    dk1_price = next(s for s in fcr_dk1.series if s.product == "price")
    dk2_price = next(s for s in fcr_dk2.series if s.product == "price")
    assert dk1_price.unit == "DKK/MW/h"
    assert dk2_price.unit == "EUR/MW/h"
