"""
Tests for `shared/feature_store.py`, written BEFORE the builder per
`docs/forecast-feature-store-design.md` §2: "write the horizon test first,
then the builder. The test is the deliverable." The four cases below are
the ones that document mandates (§2.1-§2.4); a handful of supplementary
tests below them exercise the other named hazards (§4.4's corridor
endpoint-order parsing, §4.5's zone-allowlist logging) and basic interface
sanity, but the four required cases are the actual guarantee.

Every case uses a synthetic, hand-built fake `DatabaseManager` (per §2's
"Use synthetic fixtures for the leak tests" instruction) -- never the real
database. `_make_fake_db`'s `forbid_products` mechanism raises from *inside*
`fetch_series_values` itself if the builder ever queries a denied product,
so case 1 fails loudly even under a future refactor that changes how
`build_features` decides what to query, not only by inspecting its output.

Also includes the schema-determinism test the coordinator asked for after
the smoke run: the returned key set must depend only on `(zone, horizon)`,
never on what data happens to exist in the requested window -- see
`test_schema_is_identical_across_windows_with_the_same_zone_and_horizon`.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from shared.feature_store import KNOWN_BORDER_CORRIDORS, LEAKY_SUFFIX, build_features

BASE = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _make_fake_db(
    series: dict[tuple[str, str, str], list[dict]] | None = None,
    zone_counts: dict[str, int] | None = None,
    forbid_products: set[str] | None = None,
    events: list[dict] | None = None,
):
    """
    Builds a MagicMock `DatabaseManager` whose `fetch_series_values` answers
    from `series` (keyed by (market, zone, product), each a list of
    {"time": ..., "value": ...} dicts -- matching
    `shared.db_manager.DatabaseManager.fetch_series_values`'s own return
    shape), filtered to the requested [time_from, time_to] the same way the
    real `market_data` view's WHERE clause would. `fetch_zone_counts`
    answers the §4.5 zone-allowlist diagnostic. Corridors are no longer
    discovered via the database (`build_features` reads the declared
    `KNOWN_BORDER_CORRIDORS` registry instead, per the schema-determinism
    fix), so there is no corridor-seeding knob here any more.

    `events` (M6+, docs/supply-event-features-design.md) seeds
    `fetch_market_events`, matching
    `shared.db_manager.DatabaseManager.fetch_market_events`'s own contract:
    every event dict with `known_at <= known_at_before` is returned,
    regardless of zone/market/type -- the same "fetch broad, filter in
    `build_features`" shape the real method uses, so these fixtures exercise
    the same code path the real DB read would.
    """
    series = dict(series or {})
    forbid_products = forbid_products or set()
    events = list(events or [])
    db = MagicMock()

    def fetch_series_values(
        market, zone, product, limit=None, time_from=None, time_to=None, history=False
    ):
        if product in forbid_products:
            raise AssertionError(
                f"build_features must never query the denied product {product!r} "
                f"(market={market!r}, zone={zone!r})"
            )
        rows = series.get((market, zone, product), [])
        if time_from is not None:
            rows = [r for r in rows if r["time"] >= time_from]
        if time_to is not None:
            rows = [r for r in rows if r["time"] <= time_to]
        return rows

    def fetch_market_events(known_at_before):
        return [e for e in events if e["known_at"] <= known_at_before]

    db.fetch_series_values.side_effect = fetch_series_values
    db.fetch_zone_counts.return_value = zone_counts or {"DK1": 5, "DK2": 5}
    db.fetch_market_events.side_effect = fetch_market_events

    return db


def _event(
    event_id="evt-1",
    event_type="prequalification",
    market="FCR",
    zone="DK2",
    direction=None,
    magnitude_mw=50.0,
    effective_from=None,
    known_at=None,
    confidence=0.9,
    source_tier="tier1",
):
    """A synthetic `market_events` row, shaped like `fetch_market_events`'s return value."""
    return {
        "event_id": event_id,
        "event_type": event_type,
        "market": market,
        "zone": zone,
        "direction": direction,
        "magnitude_mw": magnitude_mw,
        "effective_from": effective_from,
        "known_at": known_at,
        "confidence": confidence,
        "source_tier": source_tier,
    }


# --- §2.1: leaky column is absent at every horizon --------------------------


def test_leaky_forecast_column_never_appears_at_any_horizon():
    forbid = {
        f"{t}_current_leaky_do_not_use_as_feature"
        for t in ("offshore_wind", "onshore_wind", "solar")
    }
    db = _make_fake_db(forbid_products=forbid)

    for horizon in (
        timedelta(minutes=15),
        timedelta(hours=1),
        timedelta(hours=12),
        timedelta(hours=48),
    ):
        rows = build_features(db, "DK2", BASE, BASE + timedelta(hours=3), horizon)
        for row in rows:
            for key in row:
                assert LEAKY_SUFFIX not in key, f"leaky key {key!r} present at horizon={horizon}"
    # Reaching here (rather than an AssertionError raised from inside
    # fetch_series_values via `forbid_products`) additionally proves the
    # leaky product is never even *queried*, not merely filtered afterward.


# --- §2.2: horizon monotonicity ----------------------------------------------


def test_horizon_monotonicity_forecast_columns_are_a_subset_at_longer_horizon():
    mtu = BASE
    series = {
        ("wind_solar_forecast", "DK2", "offshore_wind_day_ahead"): [{"time": mtu, "value": 100.0}],
        ("wind_solar_forecast", "DK2", "offshore_wind_5hour"): [{"time": mtu, "value": 110.0}],
        ("wind_solar_forecast", "DK2", "offshore_wind_1hour"): [{"time": mtu, "value": 115.0}],
    }
    db = _make_fake_db(series=series)

    short = build_features(db, "DK2", mtu, mtu + timedelta(hours=1), timedelta(hours=1))[0]
    long = build_features(db, "DK2", mtu, mtu + timedelta(hours=1), timedelta(hours=12))[0]

    # day_ahead (declared min lead 12h, design §4.1) survives at both.
    assert short["offshore_wind_day_ahead"] == 100.0
    assert long["offshore_wind_day_ahead"] == 100.0

    # 5hour/1hour (min lead 5h/1h) and the revision derived from both are
    # structurally ABSENT as keys at the 12h horizon -- not merely null.
    for key in ("offshore_wind_5hour", "offshore_wind_1hour", "offshore_wind_revision_5h_to_1h"):
        assert key in short
        assert key not in long
    assert short["offshore_wind_5hour"] == 110.0
    assert short["offshore_wind_1hour"] == 115.0

    # The anti-regression check this case exists for: a builder that
    # silently ignores `horizon` would produce identical key sets at every
    # horizon. Proper-subset (`<`) asserts both containment and inequality.
    assert set(long.keys()) < set(short.keys())


def test_horizon_monotonicity_raw_lagged_value_degrades_not_just_forecast_columns():
    """
    The same monotonicity property, but for a RULE-B (as-of joined) raw
    feature rather than a forecast-horizon column: a data point published
    strictly between the two decision times is visible at the shorter
    horizon and genuinely absent (None, the key itself still present) at
    the longer one -- design §2.2's "unless its source publication time
    genuinely falls between them" clause, stated directly.
    """
    mtu = BASE
    short_horizon = timedelta(hours=1)
    long_horizon = timedelta(hours=12)
    # Published after the 12h decision point but before the 1h one.
    mid_time = mtu - timedelta(hours=6)
    series = {
        ("day_ahead", "DE", "price"): [{"time": mid_time, "value": 55.5}],
    }
    db = _make_fake_db(series=series)

    short = build_features(db, "DK1", mtu, mtu + timedelta(hours=1), short_horizon)[0]
    long_ = build_features(db, "DK1", mtu, mtu + timedelta(hours=1), long_horizon)[0]

    assert "day_ahead_price_DE" in short
    assert "day_ahead_price_DE" in long_
    assert short["day_ahead_price_DE"] == 55.5
    assert long_["day_ahead_price_DE"] is None


# --- §2.3: source-time bound --------------------------------------------------


def test_source_time_strictly_after_cutoff_never_leaks_into_the_row():
    mtu = BASE
    horizon = timedelta(hours=12)
    decision_time = mtu - horizon
    leak_time = decision_time + timedelta(minutes=1)  # one minute past the cutoff
    safe_time = decision_time - timedelta(hours=1)

    series = {
        ("day_ahead", "DE", "price"): [
            {"time": safe_time, "value": 42.0},
            {"time": leak_time, "value": 999999.0},  # must never surface, at any margin
        ],
    }
    db = _make_fake_db(series=series)

    row = build_features(db, "DK1", mtu, mtu + timedelta(hours=1), horizon)[0]

    assert row["day_ahead_price_DE"] == 42.0
    assert 999999.0 not in row.values()


# --- §2.4: realised-value features respect the lag --------------------------


def test_realised_production_at_mtu_start_itself_is_never_used():
    mtu = BASE
    horizon = timedelta(hours=12)
    decision_time = mtu - horizon

    series = {
        ("realtime_production_exchange", "DK2", "offshore_wind"): [
            {"time": decision_time - timedelta(hours=1), "value": 10.0},
            {"time": mtu, "value": 99999.0},  # this MTU's own realised value -- must never leak
        ],
        ("realtime_production_exchange", "DK2", "onshore_wind"): [
            {"time": decision_time - timedelta(hours=1), "value": 5.0},
        ],
    }
    db = _make_fake_db(series=series)

    row = build_features(db, "DK2", mtu, mtu + timedelta(hours=1), horizon)[0]

    assert row["realised_offshore_wind"] == 10.0
    assert 99999.0 not in row.values()


# --- supplementary: §4.4 corridor endpoint-order parsing --------------------


def test_corridors_relevant_to_zone_found_regardless_of_endpoint_order():
    """
    `SE4-DK2` and `DK2-DK1` put DK2 *second* -- a naive `startswith` check
    would miss both. `aFRR_border_atc`'s corridors come from the declared
    `KNOWN_BORDER_CORRIDORS` registry (module docstring's "Schema
    determinism" note -- no longer a live discovery query), but the
    endpoint-order parsing itself is still real code, exercised here
    against that registry's actual contents.
    """
    assert {"DK1-DE", "DK1-NL", "DK2-DE", "DK2-DK1", "SE4-DK2"} <= set(KNOWN_BORDER_CORRIDORS)

    db = _make_fake_db()

    dk2_rows = build_features(db, "DK2", BASE, BASE + timedelta(hours=1), timedelta(hours=1))
    dk1_rows = build_features(db, "DK1", BASE, BASE + timedelta(hours=1), timedelta(hours=1))

    dk2_keys = set(dk2_rows[0].keys())
    dk1_keys = set(dk1_rows[0].keys())

    assert "atc_import_DK2_DE" in dk2_keys
    assert "atc_import_DK2_DK1" in dk2_keys  # endpoint second -- must still be found
    assert "atc_import_SE4_DK2" in dk2_keys  # endpoint second -- must still be found
    assert "atc_import_DK1_DE" not in dk2_keys  # not DK2's corridor

    assert "atc_import_DK1_DE" in dk1_keys
    assert "atc_import_DK1_NL" in dk1_keys
    # DK2-DK1 has DK1 as its FIRST endpoint too -- this corridor is
    # legitimately relevant to both zones, not a bug to guard against.
    assert "atc_import_DK2_DK1" in dk1_keys
    assert "atc_import_SE4_DK2" not in dk1_keys  # not DK1's corridor


def test_atc_saturated_column_no_longer_exists():
    """
    Removed per coordinator review: `aFRR_border_atc`'s import/export limits
    (~0-50 MW, reserved specifically for aFRR exchange) and
    `realtime_production_exchange`'s realised flow (~+-1000+ MW, all
    cross-border trade) are different physical quantities -- comparing them
    produced a flag that was misleadingly close to always-`True`. See
    module docstring's "atc_saturated was removed" note.
    """
    db = _make_fake_db()
    row = build_features(db, "DK2", BASE, BASE + timedelta(hours=1), timedelta(hours=1))[0]

    assert not any(key.startswith("atc_saturated") for key in row)


def test_corridor_with_no_data_in_window_still_gets_none_valued_columns():
    """
    A corridor declared in `KNOWN_BORDER_CORRIDORS` can have zero rows in a
    given call's window -- verified live for `SE4-DK2` (stopped publishing
    2026-06-02) and `DK1-NL` (stopped 2026-07-09). Per the coordinator's
    schema-determinism fix, its columns must still be present, valued
    `None` -- never omitted, since omitting them would make the schema a
    function of data availability rather than of `(zone, horizon)` alone.
    """
    db = _make_fake_db(series={})  # no data for any (market, zone, product) at all

    row = build_features(db, "DK2", BASE, BASE + timedelta(hours=1), timedelta(hours=1))[0]

    assert "atc_import_SE4_DK2" in row
    assert row["atc_import_SE4_DK2"] is None
    assert "atc_export_SE4_DK2" in row
    assert row["atc_export_SE4_DK2"] is None


# --- M6+: supply-event features (docs/supply-event-features-design.md) -----
#
# §1's leak rule, different from the RULE-B "source-time cutoff" tests
# above: an event's `known_at` (article publish time; the crawler's
# assignment, never the model's) gates availability, and `effective_from`
# (when the announced capacity lands) is a VALUE inside the feature, never
# the availability key. Written before any of `build_features`'s event-column
# implementation existed, per the task's "write the leak test first"
# instruction -- exactly as P1's own horizon tests (§2.1-§2.4 above) were.


def test_event_known_at_after_decision_time_never_leaks_into_that_mtus_features():
    """
    THE core leak test (design §1). An event whose `known_at` is one minute
    AFTER the decision time (`mtu_start - horizon`) must never surface in
    that MTU's features -- even though its `effective_from` already lies
    safely in the *past* relative to the decision time. That combination is
    exactly design §1's "the trap": a builder that (wrongly) keyed
    availability on `effective_from` instead of `known_at` would let this
    event through, because "capacity already effective" looks safe if you
    don't check when the information became public.
    """
    mtu = BASE
    horizon = timedelta(hours=12)
    decision_time = mtu - horizon

    leaky_event = _event(
        magnitude_mw=99999.0,  # distinctive -- must never appear anywhere in the row
        effective_from=(decision_time - timedelta(days=10)).date(),  # already "in effect"
        known_at=decision_time + timedelta(minutes=1),  # one minute too late to be knowable
    )
    db = _make_fake_db(events=[leaky_event])

    row = build_features(db, "DK2", mtu, mtu + timedelta(hours=1), horizon)[0]

    assert row["announced_mw_entering_90d"] is None
    assert row["days_since_last_supply_event"] is None
    assert 99999.0 not in row.values()


def test_event_known_at_exactly_the_decision_time_boundary_is_included():
    """`known_at <= decision_time` (design §1) -- the boundary itself is inclusive."""
    mtu = BASE
    horizon = timedelta(hours=12)
    decision_time = mtu - horizon

    event = _event(
        magnitude_mw=42.0,
        effective_from=(decision_time + timedelta(days=30)).date(),
        known_at=decision_time,  # exactly at the cutoff
        confidence=1.0,
    )
    db = _make_fake_db(events=[event])

    row = build_features(db, "DK2", mtu, mtu + timedelta(hours=1), horizon)[0]

    assert row["announced_mw_entering_90d"] == 42.0


def test_event_known_well_in_advance_of_its_future_effective_date_is_included():
    """
    Design §1's trap paragraph, the affirmative case: an event *reported*
    well before the decision time but whose capacity only becomes
    *effective* well after it (e.g. reported 2026-07-15 about capacity
    landing 2026-09-01) is knowable from `known_at` onward and must be
    counted -- proving `effective_from` is read purely as a value describing
    how soon the capacity lands, never as the gate for whether the event is
    visible at all.
    """
    mtu = BASE
    horizon = timedelta(hours=12)
    decision_time = mtu - horizon

    event = _event(
        magnitude_mw=240.0,
        known_at=decision_time - timedelta(days=5),  # reported well in advance
        effective_from=(decision_time + timedelta(days=45)).date(),  # lands well after
        confidence=1.0,
    )
    db = _make_fake_db(events=[event])

    row = build_features(db, "DK2", mtu, mtu + timedelta(hours=1), horizon)[0]

    assert row["announced_mw_entering_90d"] == 240.0


def test_event_outside_the_90d_forward_window_is_excluded():
    mtu = BASE
    horizon = timedelta(hours=1)
    decision_time = mtu - horizon

    event = _event(
        magnitude_mw=15.0,
        known_at=decision_time - timedelta(days=1),
        effective_from=(decision_time + timedelta(days=200)).date(),  # far beyond 90d
    )
    db = _make_fake_db(events=[event])

    row = build_features(db, "DK2", mtu, mtu + timedelta(hours=1), horizon)[0]

    assert row["announced_mw_entering_90d"] is None


def test_event_zone_mismatch_excluded_from_zone_specific_columns():
    """
    `build_features` has no `market`/`direction` parameter at all (its grain
    is `(zone, horizon)`) -- so event matching here is on `zone` alone. A
    DK1 event must never feed a DK2 row's columns.
    """
    mtu = BASE
    horizon = timedelta(hours=1)
    decision_time = mtu - horizon

    event = _event(
        zone="DK1",
        magnitude_mw=77.0,
        known_at=decision_time - timedelta(days=1),
        effective_from=(decision_time + timedelta(days=5)).date(),
    )
    db = _make_fake_db(events=[event])

    row = build_features(db, "DK2", mtu, mtu + timedelta(hours=1), horizon)[0]

    assert row["announced_mw_entering_90d"] is None


def test_announced_mw_entering_90d_confidence_weighted_and_type_filtered():
    mtu = BASE
    horizon = timedelta(hours=1)
    decision_time = mtu - horizon

    prequal = _event(
        event_id="evt-prequal",
        event_type="prequalification",
        magnitude_mw=100.0,
        confidence=0.5,  # Tier-2-capped, e.g.
        known_at=decision_time - timedelta(days=2),
        effective_from=(decision_time + timedelta(days=10)).date(),
    )
    commissioning = _event(
        event_id="evt-commission",
        event_type="capacity_commissioning",
        magnitude_mw=40.0,
        confidence=1.0,
        known_at=decision_time - timedelta(days=1),
        effective_from=(decision_time + timedelta(days=20)).date(),
    )
    retirement = _event(
        event_id="evt-retire",
        event_type="capacity_retirement",  # not "entering" -- must not be summed here
        magnitude_mw=1000.0,
        confidence=1.0,
        known_at=decision_time - timedelta(days=1),
        effective_from=(decision_time + timedelta(days=20)).date(),
    )
    db = _make_fake_db(events=[prequal, commissioning, retirement])

    row = build_features(db, "DK2", mtu, mtu + timedelta(hours=1), horizon)[0]

    assert row["announced_mw_entering_90d"] == 100.0 * 0.5 + 40.0 * 1.0


def test_net_demand_volume_change_30d_signed_confidence_weighted_sum():
    mtu = BASE
    horizon = timedelta(hours=1)
    decision_time = mtu - horizon

    increase = _event(
        event_id="evt-up",
        event_type="demand_volume_change",
        magnitude_mw=20.0,
        confidence=1.0,
        known_at=decision_time - timedelta(days=5),
    )
    decrease = _event(
        event_id="evt-down",
        event_type="demand_volume_change",
        magnitude_mw=-8.0,
        confidence=0.5,
        known_at=decision_time - timedelta(days=10),
    )
    too_old = _event(
        event_id="evt-old",
        event_type="demand_volume_change",
        magnitude_mw=1000.0,
        confidence=1.0,
        known_at=decision_time - timedelta(days=45),  # outside the trailing 30d window
    )
    db = _make_fake_db(events=[increase, decrease, too_old])

    row = build_features(db, "DK2", mtu, mtu + timedelta(hours=1), horizon)[0]

    assert row["net_demand_volume_change_30d"] == 20.0 * 1.0 + (-8.0) * 0.5


def test_regime_change_within_horizon_true_when_known_and_effective_in_window():
    mtu = BASE
    horizon = timedelta(hours=1)
    decision_time = mtu - horizon

    event = _event(
        event_type="regime_change",
        magnitude_mw=None,
        confidence=0.8,
        known_at=decision_time - timedelta(days=2),
        effective_from=(decision_time + timedelta(days=20)).date(),
    )
    db = _make_fake_db(events=[event])

    row = build_features(db, "DK2", mtu, mtu + timedelta(hours=1), horizon)[0]

    assert row["regime_change_within_horizon"] is True


def test_regime_change_within_horizon_false_when_none_present():
    db = _make_fake_db(events=[])
    row = build_features(db, "DK2", BASE, BASE + timedelta(hours=1), timedelta(hours=1))[0]
    assert row["regime_change_within_horizon"] is False


def test_days_since_last_supply_event_computed_from_most_recent_known_event():
    mtu = BASE
    horizon = timedelta(hours=1)
    decision_time = mtu - horizon

    older = _event(
        event_id="evt-older",
        event_type="outage",
        magnitude_mw=None,
        known_at=decision_time - timedelta(days=10),
    )
    newer = _event(
        event_id="evt-newer",
        event_type="prequalification",
        magnitude_mw=5.0,
        known_at=decision_time - timedelta(days=3),
    )
    db = _make_fake_db(events=[older, newer])

    row = build_features(db, "DK2", mtu, mtu + timedelta(hours=1), horizon)[0]

    assert row["days_since_last_supply_event"] == 3


def test_days_since_last_supply_event_none_when_no_event_ever_known():
    db = _make_fake_db(events=[])
    row = build_features(db, "DK2", BASE, BASE + timedelta(hours=1), timedelta(hours=1))[0]
    assert row["days_since_last_supply_event"] is None


def test_event_columns_always_present_with_honest_defaults_when_no_events_at_all():
    """
    Design §5's schema-determinism guarantee, applied to the event columns:
    present on every row -- null/0/false where none, never omitted -- even
    when `fetch_market_events` returns nothing at all (the ordinary case
    today, design §0).
    """
    db = _make_fake_db(events=[])
    row = build_features(db, "DK2", BASE, BASE + timedelta(hours=1), timedelta(hours=1))[0]

    assert row["announced_mw_entering_90d"] is None
    assert row["net_demand_volume_change_30d"] is None
    assert row["regime_change_within_horizon"] is False
    assert row["days_since_last_supply_event"] is None


def test_event_feature_fill_rate_is_logged(caplog):
    """
    Design §5's mandatory honesty requirement: `build_features` must log the
    event columns' fill rate over the requested window, so a 99%-null
    reality (design §0) is visible in the logs, never silently shipped as if
    the columns carried more signal than they do.
    """
    mtu = BASE
    horizon = timedelta(hours=1)
    decision_time = mtu - horizon
    event = _event(
        known_at=decision_time - timedelta(days=1),
        effective_from=(decision_time + timedelta(days=5)).date(),
    )
    db = _make_fake_db(events=[event])

    with caplog.at_level("INFO"):
        build_features(db, "DK2", mtu, mtu + timedelta(hours=5), horizon)

    assert any(
        "fill rate" in record.message.lower() and "event" in record.message.lower()
        for record in caplog.records
    )


# --- schema determinism (coordinator review, post-smoke-run) ----------------


def test_schema_is_identical_across_windows_with_the_same_zone_and_horizon():
    """
    The returned key set must depend only on `(zone, horizon)`, never on
    what data happens to exist in `[start, end]` -- otherwise a training
    frame and a serving frame built from different windows are not
    comparable (train/serve skew), the same "invisible until it costs
    money" failure class as a leak, one layer up. Two calls below use
    disjoint windows and completely different (one populated, one empty)
    underlying data; their row's key sets must still match exactly.

    Extended (M6+) to also seed `events=[...]` on the populated side and
    none on the empty side -- the event columns (design §5) must be part of
    this same guarantee, not a schema-drift exception to it.
    """
    horizon = timedelta(hours=1)

    populated_db = _make_fake_db(
        series={
            ("wind_solar_forecast", "DK2", "offshore_wind_1hour"): [{"time": BASE, "value": 1.0}],
            ("aFRR_border_atc", "SE4-DK2", "import"): [{"time": BASE, "value": 5.0}],
        },
        events=[
            _event(
                known_at=BASE - timedelta(hours=2),
                effective_from=(BASE + timedelta(days=5)).date(),
            )
        ],
    )
    empty_db = _make_fake_db(series={}, events=[])

    window_a_start = BASE
    window_b_start = BASE + timedelta(days=90)  # a completely disjoint window

    rows_a = build_features(
        populated_db, "DK2", window_a_start, window_a_start + timedelta(hours=1), horizon
    )
    rows_b = build_features(
        empty_db, "DK2", window_b_start, window_b_start + timedelta(hours=1), horizon
    )

    assert set(rows_a[0].keys()) == set(rows_b[0].keys())


# --- supplementary: §4.5 zone-allowlist drop logging -------------------------


def test_zone_filter_drop_count_is_logged_when_rows_exist_outside_allowlist(caplog):
    db = _make_fake_db(zone_counts={"DK1": 10, "DK2": 12, "UNDEFINED": 482})

    with caplog.at_level("WARNING"):
        build_features(db, "DK2", BASE, BASE + timedelta(hours=1), timedelta(hours=1))

    assert any("482" in record.message for record in caplog.records)


def test_zone_filter_logs_nothing_when_no_rows_outside_allowlist(caplog):
    db = _make_fake_db(zone_counts={"DK1": 10, "DK2": 12})

    with caplog.at_level("WARNING"):
        build_features(db, "DK2", BASE, BASE + timedelta(hours=1), timedelta(hours=1))

    # Distinct from `_log_all_null_columns`'s (unrelated, expected here
    # since this fake db has no series data at all) warning -- only the
    # §4.5 zone-allowlist message must be absent.
    assert not any("outside the zone allow-list" in r.message for r in caplog.records)


# --- interface sanity ---------------------------------------------------------


def test_rejects_unknown_zone():
    db = _make_fake_db()
    try:
        build_features(db, "DE", BASE, BASE + timedelta(hours=1), timedelta(hours=1))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a zone outside {'DK1', 'DK2'}")


def test_one_row_per_hour_sorted_by_mtu_start():
    db = _make_fake_db()
    rows = build_features(db, "DK1", BASE, BASE + timedelta(hours=5), timedelta(hours=1))

    assert [r["mtu_start"] for r in rows] == [BASE + timedelta(hours=i) for i in range(5)]
    assert all(r["zone"] == "DK1" for r in rows)


def test_empty_window_returns_no_rows():
    db = _make_fake_db()
    assert build_features(db, "DK1", BASE, BASE, timedelta(hours=1)) == []
    assert build_features(db, "DK1", BASE, BASE - timedelta(hours=1), timedelta(hours=1)) == []
