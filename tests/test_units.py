"""
Tests for shared/units.py -- the (market, zone, product) -> unit/currency
lookup backing the Stage 1 correctness fix (see that module's docstring).
"""

from shared.units import currency_for, same_currency, unit_for

# --- unit_for: the case this index has to get right -------------------------


def test_unit_for_fcr_dk1_and_dk2_resolve_to_different_currencies():
    """
    The two-entries-one-label case: DK1's FCR price (fixed zone="DK1") and
    DK2's FCR price (zone-agnostic, resolved via PriceArea) share
    market="FCR"/product="price" but are genuinely different currencies --
    this is the live defect (shared/bess_simulator.py summing them) this
    whole module exists to catch. Written first, per the plan.
    """
    assert unit_for("FCR", "DK1", "price") == "DKK/MW/h"
    assert unit_for("FCR", "DK2", "price") == "EUR/MW/h"


def test_unit_for_dk2_fcr_d_legs_are_eur():
    assert unit_for("FCR", "DK2", "up") == "EUR/MW/h"
    assert unit_for("FCR", "DK2", "down") == "EUR/MW/h"


def test_unit_for_afrr_capacity_is_dkk_in_both_zones():
    assert unit_for("aFRR_capacity", "DK1", "up") == "DKK/MW/h"
    assert unit_for("aFRR_capacity", "DK2", "up") == "DKK/MW/h"


def test_unit_for_zone_agnostic_series_resolves_for_any_zone():
    # imbalance_price has no fixed zone (zone_field="PriceArea") -- every
    # zone that ever appears in the data resolves to the one registered unit.
    assert unit_for("imbalance", "DK1", "imbalance_price") == "DKK/MWh"
    assert unit_for("imbalance", "DK2", "imbalance_price") == "DKK/MWh"
    assert unit_for("imbalance", "SE3", "imbalance_price") == "DKK/MWh"


def test_unit_for_unregistered_key_is_none():
    assert unit_for("not_a_real_market", "DK1", "price") is None


def test_unit_for_zone_override_resolves_inertia_dk2():
    """
    Stage 3: `inertia_nordic`'s `dk2` product uses `SeriesConfig.zone="DK2"`
    (a per-series override, not the dataset's own `zone="ALL"` default) --
    this is the exact case that field was added for (§1.2 of the plan).
    """
    assert unit_for("inertia", "DK2", "dk2") == "GWs"
    # The Nordic-wide product (no series-level zone override) resolves under
    # the dataset's own default zone instead.
    assert unit_for("inertia", "ALL", "nordic") == "GWs"


def test_unit_for_ffr_dk2_is_dkk_primary():
    assert unit_for("FFR", "DK2", "price") == "DKK/MW/h"
    assert unit_for("FFR", "DK2", "price_eur") == "EUR/MW/h"


def test_unit_for_mfrr_capacity_extra_matches_mfrr_capacity_shape():
    assert unit_for("mFRR_capacity_extra", "DK1", "up") == "DKK/MW/h"


def test_no_shipped_series_has_unit_unknown():
    """
    "unknown" is SeriesConfig.unit's default specifically so an
    un-annotated series is *visibly* un-annotated (shared/datasets.py's
    module docstring) -- every series actually shipped in the registry must
    have a real declared unit, not the default.
    """
    from shared.datasets import DATASETS

    unknown = [(d.name, s.product) for d in DATASETS for s in d.series if s.unit == "unknown"]
    assert unknown == []


# --- currency_for ------------------------------------------------------------


def test_currency_for_monetary_series():
    assert currency_for("FCR", "DK1", "price") == "DKK"
    assert currency_for("FCR", "DK2", "price") == "EUR"


def test_currency_for_non_monetary_series_is_none():
    assert currency_for("system_state", "ALL", "solar") is None


def test_currency_for_unregistered_key_is_none():
    assert currency_for("not_a_real_market", "DK1", "price") is None


# --- same_currency -------------------------------------------------------------


def test_same_currency_false_for_the_fcr_pair():
    assert same_currency(("FCR", "DK1", "price"), ("FCR", "DK2", "price")) is False


def test_same_currency_true_for_same_currency_pair():
    assert same_currency(("aFRR_capacity", "DK1", "up"), ("aFRR_capacity", "DK2", "up")) is True


def test_same_currency_true_trivially_for_a_single_key():
    assert same_currency(("FCR", "DK1", "price")) is True


def test_same_currency_true_for_non_monetary_pair():
    # Both non-monetary (no currency at all) -- no DKK/EUR mixing risk, so
    # this must not be flagged as a mismatch.
    assert (
        same_currency(
            ("system_state", "ALL", "onshore_wind"), ("system_state", "ALL", "offshore_wind")
        )
        is True
    )
