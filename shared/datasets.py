"""
Declarative configuration for Energinet Energi Data Service datasets.

Each `DatasetConfig` describes one Energi Data Service dataset (`dataset/{dataset_id}`
on `api.energidataservice.dk`) and how its JSON records map onto the
`market_data_history` schema (see `init-db/01-init.sql`):

- `time_field` / `zone_field` locate the market time unit and bidding zone within
  each record.
- `series` lists the individual value columns to extract from each record; a
  single Energinet record commonly carries several products at once (e.g. an
  mFRR capacity record carries both `UpPriceDKK` and `DownPriceDKK`), so each
  dataset can declare multiple `SeriesConfig` entries.

Field names are taken verbatim from `docs/dataset-catalogue.md` (the M0 audit)
and `docs/dataset-catalogue-addendum.md` (the M0 addendum, which confirmed
several datasets M0 could not, by querying `api.energidataservice.dk`
directly rather than the JS-rendered marketing site), not invented. Known
gaps carried forward here:

- No dataset exposes an explicit `PublishedTime`/`RevisedTime` field for
  *most* series, so `is_provisional` is a static, catalogue-derived best
  guess per dataset, not something Energinet tells us per-record. See the
  `afrr_picasso_corrections` entry below for the one dataset investigated as
  a possible exception (it turned out not to be one — see its comment).
- The "mFRR EAM energy activation" dataset the README treats as the primary
  focus — `MfrrEnergyActivationMarket` — **is now confirmed and ingested**
  (`mfrr_eam` below), closing the gap the original M0 audit (catalogue §8.1)
  flagged and every milestone since M1 worked around via `mFRRCapacityMarket`
  (a *capacity/reservation* market, still ingested separately as
  `mfrr_capacity` below — the two are distinct products and are not merged).

Every `SeriesConfig` below also declares its `unit` (see `shared/units.py` for
the lookup this registry backs). This closes a real, live defect: without a
declared unit, `shared/bess_simulator.py` and `shared/rule_engine.py` had no
way to know that DK1's FCR price (`fcr_dk1`, DKK/MW/h) and DK2's FCR price
(`fcr_dk2`, EUR/MW/h) are not the same currency, and were silently
summing/subtracting them. `unit` defaults to `"unknown"` (not e.g. `"DKK/MWh"`)
precisely so a series nobody has annotated yet is *visibly* unannotated
rather than silently mislabeled — a shipped series left at `"unknown"` is a
bug, caught by `tests/test_units.py`.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SeriesConfig:
    """One value column within a dataset record, mapped to a market_data product."""

    product: str
    value_field: str
    # The physical unit this series is denominated in, e.g. "DKK/MW/h"
    # (capacity/reservation payments), "EUR/MWh" (energy activation
    # payments), "MW" (a volume), "g/kWh" (an emissions intensity). Deriving
    # currency from the full unit (not a bare "DKK"/"EUR" tag) matters
    # because monetary series have two different denominators -- a capacity
    # price (DKK/MW/h) and an energy price (DKK/MWh) are not interchangeable
    # even when both are DKK, so collapsing straight to currency would hide
    # that second latent unit mismatch. Looked up via `shared/units.py`,
    # never stored as a market_data_history column (see that module's
    # docstring for why: it's a registry-derived fact, not something any
    # ingested record carries). Defaults to "unknown", not a guessed unit --
    # see module docstring.
    unit: str = "unknown"
    # Override the dataset-level market label for this series, if the same
    # dataset spans more than one logical market (unused today, kept for
    # forward-compatibility with e.g. combined capacity+energy datasets).
    market: str | None = None
    # Override the dataset-level zone for this one series, for datasets whose
    # records mix a zone-wide figure with per-zone figures under different
    # column names (unused today -- kept symmetric with `market` above,
    # which datasets.py already documents as "kept for forward-compatibility"
    # -- this is that same case, needed once a zone-heterogeneous dataset
    # like `InertiaNordicSyncharea` is ingested). `None` (the default) means
    # "use the dataset's own zone resolution" (`zone_field`/`zone`), so every
    # existing series is unaffected.
    zone: str | None = None
    # Optional record-level filter: only map this series for records where
    # `record[filter_field] == filter_value`. Needed for datasets that pack
    # several distinct products into one shared value column, distinguished
    # only by a categorical field rather than by separate columns — e.g.
    # `FcrNdDK2` (see below) carries "FCR-D ned"/"FCR-D upp"/"FCR-N" and
    # "D-1 early"/"Total" auction rows all through the same `PriceTotalEUR`
    # column. Unused (None) for the common case of one value_field == one
    # product.
    filter_field: str | None = None
    filter_value: str | None = None
    # Additional record-level filters, all of which must match (AND'd
    # together with `filter_field`/`filter_value` above), for datasets that
    # need more than one categorical field to pin down a single product --
    # e.g. `FcrNdDK2` (see below) needs both `ProductName` ("FCR-D
    # ned"/"FCR-D upp"/"FCR-N") and `AuctionType` ("D-1 early"/"Total") to
    # unambiguously select one series. Empty (default) for the common case.
    extra_filters: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DatasetConfig:
    """Declarative mapping from one Energi Data Service dataset to market_data rows."""

    name: str  # short slug used for logging, e.g. "mfrr_capacity"
    dataset_id: str  # Energinet dataset ID, e.g. "mFRRCapacityMarket"
    market: str  # market label stored in market_data.market
    time_field: str  # JSON field holding the market time unit (usually *UTC)
    series: list[SeriesConfig]
    # Some datasets (e.g. PowerSystemRightNow) are system-wide snapshots with no
    # per-record zone field; set zone_field=None and rely on `zone` instead.
    zone_field: str | None = "PriceArea"
    zone: str = "ALL"
    source: str = "Energinet"
    is_provisional: bool = True
    params: dict = field(default_factory=lambda: {"limit": 100, "sort": "TimeUTC DESC"})


DATASETS: list[DatasetConfig] = [
    # High priority — mFRR capacity (reservation) market. Closest confirmed
    # substitute for the unconfirmed "mFRR EAM energy activation" dataset;
    # this is capacity/reservation payments, not activation/energy payments.
    DatasetConfig(
        name="mfrr_capacity",
        dataset_id="mFRRCapacityMarket",
        market="mFRR_capacity",
        time_field="TimeUTC",
        zone_field="PriceArea",
        series=[
            SeriesConfig(product="up", value_field="UpPriceDKK", unit="DKK/MW/h"),
            SeriesConfig(product="down", value_field="DownPriceDKK", unit="DKK/MW/h"),
        ],
        is_provisional=True,
    ),
    # mFRR capacity extra auctions -- Energinet runs an afternoon "extra
    # auction" on `mfrr_capacity` above only when the daily morning auction's
    # forecast-based dimensioning underestimates the need (confirmed live
    # 2026-07-21, same field shape as `AfrrReservesNordic`). **Kept as a
    # separate market label** (`mFRR_capacity_extra`, not merged into
    # `mFRR_capacity`) deliberately: extra-auction clearings mixed into the
    # main auction's price history would corrupt its rule-engine baseline
    # (`shared/rule_engine.py:check_price_spike`) and fire a spike trigger
    # every extra-auction hour, which is a normal, expected event for this
    # market, not an anomaly. Added to
    # `shared/bess_simulator.py:EXCLUDED_MARKETS` -- same domain rule as
    # `mFRR_capacity`/`mFRR_EAM`, a BESS cannot currently participate in
    # mFRR in these markets.
    #
    # **Current live data (confirmed 2026-07-21): prices are null, volumes
    # are 0.0** -- extra auctions are rare (this dataset's own
    # `meta/dataset` description: "may appear empty... but volumes and
    # prices are shown when an auction has taken place"), so the price
    # columns being null today is a legitimate "hasn't happened recently"
    # state, NOT a typo -- `shared/dataset_validation.py`'s schema-based
    # (not sample-based) validation correctly does not flag this (the
    # columns are present in the published schema regardless of what any
    # particular row's values are).
    DatasetConfig(
        name="mfrr_capacity_extra",
        dataset_id="MfrrCapacityMarketExtra",
        market="mFRR_capacity_extra",
        time_field="TimeUTC",
        zone_field="PriceArea",
        series=[
            SeriesConfig(product="up", value_field="UpPriceDKK", unit="DKK/MW/h"),
            SeriesConfig(product="down", value_field="DownPriceDKK", unit="DKK/MW/h"),
            SeriesConfig(product="up_demand_volume", value_field="UpDemandMW", unit="MW"),
            SeriesConfig(product="down_demand_volume", value_field="DownDemandMW", unit="MW"),
            SeriesConfig(product="up_procured_volume", value_field="UpProcuredMW", unit="MW"),
            SeriesConfig(product="down_procured_volume", value_field="DownProcuredMW", unit="MW"),
        ],
        is_provisional=True,
        params={"limit": 100, "sort": "TimeUTC DESC"},
    ),
    # High priority — aFRR activation energy (PICASSO). Millisecond-resolution,
    # near-real-time; catalogue notes only EUR prices are published (no DKK).
    DatasetConfig(
        name="afrr_energy_activation",
        dataset_id="aFRREnergyActivation",
        market="aFRR_energy",
        time_field="TimeMsUTC",
        zone_field="PriceArea",
        series=[
            SeriesConfig(
                product="activation_price", value_field="aFRR_ActivatedEUR", unit="EUR/MWh"
            ),
            # aFRR_Activated (MW): confirmed live via the dataset's own API
            # metadata (`meta/dataset/AfrrEnergyActivation`) — "Activation in
            # MW. Positive value is up regulation, negative value is down
            # regulation." A single signed field, not an up/down pair, so one
            # product (not "up_volume"/"down_volume") is enough to carry it.
            SeriesConfig(product="activation_volume", value_field="aFRR_Activated", unit="MW"),
        ],
        is_provisional=True,
        params={"limit": 100, "sort": "TimeMsUTC DESC"},
    ),
    # High priority — mFRR Energy Activation Market (EAM), Nordic, 15-minute
    # MTU. This is the dataset the README's primary focus (mFRR EAM) refers
    # to; confirmed live (see docs/dataset-catalogue-addendum.md) after the
    # original M0 audit (catalogue §8.1) could not find it. `mFRRSAUp/DownEUR`
    # ("SA" = shared/standard activation) is the real Nordic EAM clearing
    # price — distinct from `mFRRCapacityMarket`'s capacity/reservation price
    # (`mfrr_capacity` above), hence the separate `mFRR_EAM` market label so
    # the two are never conflated in a chart or a rule-engine baseline.
    #
    # `market_data_history` stores one `value DOUBLE PRECISION` per row
    # (init-db/01-init.sql), so — rather than a schema migration — volumes
    # get their own `product` names (`*_volume`) alongside the price
    # products (`up`/`down`), consistent with how this whole registry
    # already distinguishes products within one dataset via `SeriesConfig`.
    # `mFRRSAUpReqMW`/`mFRRSADownReqMW` (requested volume) is mapped as
    # `up_volume`/`down_volume` since it's the closest analogue to the
    # `up`/`down` price pair; `TotalmFRRUpMW`/`TotalmFRRDownMW` (total
    # regulation, i.e. requested + local + special) and
    # `mFRROfferedUpMW`/`mFRROfferedDownMW` (offered/available volume) are
    # also live and useful context, so both are ingested too.
    #
    # `mFRRDAUpEUR`/`mFRRDADownEUR` ("DA" = a second, currently-unused
    # activation price path) and `mFRRLocalUp/DownMW`/`mFRRSpecialUp/DownMW`
    # were null across every sample pulled during this audit (~200 DK1
    # records) — left unmapped rather than ingesting an always-null series;
    # revisit if Energinet starts populating them.
    DatasetConfig(
        name="mfrr_eam",
        dataset_id="MfrrEnergyActivationMarket",
        market="mFRR_EAM",
        time_field="TimeUTC",
        zone_field="PriceArea",
        series=[
            SeriesConfig(product="up", value_field="mFRRSAUpEUR", unit="EUR/MWh"),
            SeriesConfig(product="down", value_field="mFRRSADownEUR", unit="EUR/MWh"),
            SeriesConfig(product="up_volume", value_field="mFRRSAUpReqMW", unit="MW"),
            SeriesConfig(product="down_volume", value_field="mFRRSADownReqMW", unit="MW"),
            SeriesConfig(product="up_total_volume", value_field="TotalmFRRUpMW", unit="MW"),
            SeriesConfig(product="down_total_volume", value_field="TotalmFRRDownMW", unit="MW"),
            SeriesConfig(product="up_offered_volume", value_field="mFRROfferedUpMW", unit="MW"),
            SeriesConfig(product="down_offered_volume", value_field="mFRROfferedDownMW", unit="MW"),
        ],
        is_provisional=True,
        params={"limit": 100, "sort": "TimeUTC DESC"},
    ),
    # Medium priority — aFRR PICASSO "corrections". Investigated as a
    # candidate real revision signal (every milestone since M1 has flagged
    # that no dataset exposes a true publish/revision timestamp, forcing
    # `fetched_at` as a proxy — see init-db/01-init.sql). It is NOT that:
    # per the dataset's own API metadata (`meta/dataset/AfrrPicassoCorrections`),
    # `Correction` is documented as "The correction itself. Positive value is
    # upwards adjustment, negative value is downwards adjustment." with
    # **unit MW** — i.e. a real-time correction *volume* PICASSO applies,
    # analogous to `aFRR_Activated` above, not a revised/superseding price
    # value and not a flag. `TimeMsUTC` is also the dataset's primary key
    # (alongside `PriceArea`) with second-level resolution — each row is a
    # distinct real-time observation, not a later correction of an earlier
    # `TimeMsUTC` row already in the dataset. A 100-row live sample (sorted
    # `TimeMsUTC DESC`) confirmed this: `Correction` values change from row
    # to row largely independently of `PriceUpEUR`/`PriceDownEUR`, and no two
    # rows shared a `TimeMsUTC`. So this dataset is ingested for its own
    # (volume, price) content — useful context in its own right — but it
    # does NOT give `shared/rule_engine.py:check_revisions` a better signal
    # than `fetched_at`; that rule-engine gap remains open.
    DatasetConfig(
        name="afrr_picasso_corrections",
        dataset_id="AfrrPicassoCorrections",
        market="aFRR_correction",
        time_field="TimeMsUTC",
        zone_field="PriceArea",
        series=[
            SeriesConfig(product="correction_volume", value_field="Correction", unit="MW"),
            SeriesConfig(product="up", value_field="PriceUpEUR", unit="EUR/MWh"),
            SeriesConfig(product="down", value_field="PriceDownEUR", unit="EUR/MWh"),
        ],
        is_provisional=True,
        params={"limit": 100, "sort": "TimeMsUTC DESC"},
    ),
    # High priority — imbalance settlement prices, plus aFRR volume-weighted
    # average activation prices carried in the same dataset.
    DatasetConfig(
        name="imbalance_price",
        dataset_id="ImbalancePrice",
        market="imbalance",
        time_field="TimeUTC",
        zone_field="PriceArea",
        series=[
            SeriesConfig(
                product="imbalance_price", value_field="ImbalancePriceDKK", unit="DKK/MWh"
            ),
            SeriesConfig(product="afrr_vwa_up", value_field="aFRRVWAUpDKK", unit="DKK/MWh"),
            SeriesConfig(product="afrr_vwa_down", value_field="aFRRVWADownDKK", unit="DKK/MWh"),
        ],
        is_provisional=True,
    ),
    # High priority — day-ahead spot reference prices, used to contextualize
    # activation price spikes (e.g. "mFRR price >> day-ahead price").
    DatasetConfig(
        name="day_ahead_prices",
        dataset_id="DayAheadPrices",
        market="day_ahead",
        time_field="TimeUTC",
        zone_field="PriceArea",
        series=[
            SeriesConfig(product="price", value_field="DayAheadPriceDKK", unit="DKK/MWh"),
        ],
        is_provisional=False,
    ),
    # BESS simulator (shared/bess_simulator.py) — FCR capacity/reservation
    # prices. Two different datasets per zone since FCR is structured
    # differently in each synchronous area (README §1 table): DK1 sits in
    # the FCR Cooperation (regelleistung.net) joint auction with Germany,
    # DK2 sits in the Nordic FCR-N/FCR-D market. Confirmed live via direct
    # API query 2026-07-17 (docs/dataset-catalogue-addendum.md §"Other
    # datasets confirmed live").
    #
    # FcrDK1 has no PriceArea field (it's already DK1-specific) and no
    # up/down split (FCR is a single symmetric band) — one "price" product,
    # using `FCRdk_DKK` (the domestic-weighted clearing price, combining
    # domestic + cross-border volume) rather than `FCRcross_DKK` alone.
    DatasetConfig(
        name="fcr_dk1",
        dataset_id="FcrDK1",
        market="FCR",
        time_field="HourUTC",
        zone_field=None,
        zone="DK1",
        series=[
            SeriesConfig(product="price", value_field="FCRdk_DKK", unit="DKK/MW/h"),
        ],
        is_provisional=True,
        params={"limit": 100, "sort": "HourUTC DESC"},
    ),
    # FcrNdDK2 packs three products (FCR-D ned/upp, FCR-N) and two auction
    # views (D-1 early / Total) into one shared `PriceTotalEUR` column,
    # distinguished only by `ProductName`/`AuctionType` — hence
    # `filter_field`/`filter_value` (ProductName) plus `extra_filters`
    # (AuctionType) on each SeriesConfig below. All three products' "Total"
    # (final cleared) auction row are mapped: FCR-N (the Nordic normal-
    # operation band, DK2's analogue to DK1's single symmetric FCR price
    # above) plus both FCR-D legs (disturbance reserve, split up/down --
    # DK2/Nordic-only, no DK1 equivalent, read by
    # `shared/bess_simulator.py`'s FCR-D-eligible capacity_markets). Both
    # `AuctionType` values ("D-1 early" and "Total") actually occur live
    # (confirmed 2026-07-17 via direct API query) -- every series here is
    # constrained to "Total" so a D-1 early row never silently gets treated
    # as the final cleared price.
    #
    # `market="FCR"` is kept for all three (not a separate "FCR_D" label):
    # DK1's FCR is also `market="FCR"`, and shared/bess_simulator.py
    # addresses capacity series by (market, product), so "FCR"/"up" and
    # "FCR"/"down" (FCR-D legs) vs. "FCR"/"price" (FCR-N, matching DK1's
    # single product name) are already unambiguous without a new label.
    #
    # `unit="EUR/MW/h"` on every series here, vs. `fcr_dk1`'s "DKK/MW/h"
    # above -- this is the exact live defect `shared/units.py` exists to
    # catch: `("FCR", "price")` means two different currencies depending on
    # zone (DK1's FCR Cooperation joint auction settles in DKK; DK2's Nordic
    # FCR-N/FCR-D auction settles in EUR), and until this field existed
    # nothing stopped `shared/bess_simulator.py` from summing them into one
    # "capacity_revenue_dkk" figure or `shared/rule_engine.py` from computing
    # a DK1-DK2 "divergence" that was really just a unit artifact. Since
    # `zone_field="PriceArea"` here (unlike `fcr_dk1`'s fixed `zone="DK1"`),
    # `shared/units.py` registers this as the zone-agnostic entry for
    # `("FCR", "price")` — DK1's fixed-zone entry always wins an exact-zone
    # lookup first, so a DK2 (or any non-DK1) lookup falls through to this
    # EUR entry. See `tests/test_units.py`'s first test.
    #
    # Volumes + D-1 early auction (confirmed live 2026-07-21), appended
    # below the three settled ("Total") prices above, same
    # `filter_field`/`extra_filters` pattern:
    # - `PurchasedVolumeTotal` (Nordic-wide total procured, "Total" auction)
    #   -> `volume`/`up_volume`/`down_volume`, mirroring the `price`/`up`/
    #   `down` product names above.
    # - `PurchasedVolumeLocal` (this record's own PriceArea's local
    #   contribution -- genuinely varies per zone, unlike the price/total-
    #   volume columns above, which are the same Nordic-wide clearing figure
    #   regardless of which PriceArea a given row belongs to) ->
    #   `*_volume_local`.
    # - `PriceTotalEUR` again, but filtered to `AuctionType="D-1 early"`
    #   instead of "Total" -> `d1_price`/`d1_up`/`d1_down`. This is where a
    #   BESS operator actually bids (the day-ahead-of-delivery early
    #   auction), so the D-1-vs-Total spread is directly actionable, not
    #   just informational. `Total` (mapped above) stays the settled price
    #   every other product/module reads.
    #
    # **`limit=100` coverage check (confirmed live 2026-07-21):** a live
    # 30-row pull sorted `HourUTC DESC` returned 30 rows for a SINGLE hour --
    # `FcrNdDK2` carries every Nordic PriceArea (DK2/SE1-4, 5 zones) x 3
    # ProductName x 2 AuctionType = 30 raw records/hour, not the "3 products
    # x 2 auction types = 6/hour" this dataset's name suggests. With
    # `limit=100`, that's ~3.3h of raw record coverage -- still comfortably
    # more than the 15-minute live-poll cadence needs (services/ingestor/
    # main.py), so `limit` is unchanged; noted here since it's a real
    # correction to what a first glance at the dataset would assume. Adding
    # the volume/D-1 series above extracts more *columns* from these same
    # already-fetched records -- it does not change how many raw records
    # `limit` needs to cover, so this append is safe at the current value.
    DatasetConfig(
        name="fcr_dk2",
        dataset_id="FcrNdDK2",
        market="FCR",
        time_field="HourUTC",
        zone_field="PriceArea",
        series=[
            SeriesConfig(
                product="price",
                value_field="PriceTotalEUR",
                unit="EUR/MW/h",
                filter_field="ProductName",
                filter_value="FCR-N",
                extra_filters={"AuctionType": "Total"},
            ),
            SeriesConfig(
                product="down",
                value_field="PriceTotalEUR",
                unit="EUR/MW/h",
                filter_field="ProductName",
                filter_value="FCR-D ned",
                extra_filters={"AuctionType": "Total"},
            ),
            SeriesConfig(
                product="up",
                value_field="PriceTotalEUR",
                unit="EUR/MW/h",
                filter_field="ProductName",
                filter_value="FCR-D upp",
                extra_filters={"AuctionType": "Total"},
            ),
            SeriesConfig(
                product="volume",
                value_field="PurchasedVolumeTotal",
                unit="MW",
                filter_field="ProductName",
                filter_value="FCR-N",
                extra_filters={"AuctionType": "Total"},
            ),
            SeriesConfig(
                product="down_volume",
                value_field="PurchasedVolumeTotal",
                unit="MW",
                filter_field="ProductName",
                filter_value="FCR-D ned",
                extra_filters={"AuctionType": "Total"},
            ),
            SeriesConfig(
                product="up_volume",
                value_field="PurchasedVolumeTotal",
                unit="MW",
                filter_field="ProductName",
                filter_value="FCR-D upp",
                extra_filters={"AuctionType": "Total"},
            ),
            SeriesConfig(
                product="volume_local",
                value_field="PurchasedVolumeLocal",
                unit="MW",
                filter_field="ProductName",
                filter_value="FCR-N",
                extra_filters={"AuctionType": "Total"},
            ),
            SeriesConfig(
                product="down_volume_local",
                value_field="PurchasedVolumeLocal",
                unit="MW",
                filter_field="ProductName",
                filter_value="FCR-D ned",
                extra_filters={"AuctionType": "Total"},
            ),
            SeriesConfig(
                product="up_volume_local",
                value_field="PurchasedVolumeLocal",
                unit="MW",
                filter_field="ProductName",
                filter_value="FCR-D upp",
                extra_filters={"AuctionType": "Total"},
            ),
            SeriesConfig(
                product="d1_price",
                value_field="PriceTotalEUR",
                unit="EUR/MW/h",
                filter_field="ProductName",
                filter_value="FCR-N",
                extra_filters={"AuctionType": "D-1 early"},
            ),
            SeriesConfig(
                product="d1_down",
                value_field="PriceTotalEUR",
                unit="EUR/MW/h",
                filter_field="ProductName",
                filter_value="FCR-D ned",
                extra_filters={"AuctionType": "D-1 early"},
            ),
            SeriesConfig(
                product="d1_up",
                value_field="PriceTotalEUR",
                unit="EUR/MW/h",
                filter_field="ProductName",
                filter_value="FCR-D upp",
                extra_filters={"AuctionType": "D-1 early"},
            ),
        ],
        is_provisional=True,
        params={"limit": 100, "sort": "HourUTC DESC"},
    ),
    # FFR (Fast Frequency Reserve) capacity/reservation market, DK2-only --
    # confirmed live 2026-07-21, documented but never previously ingested
    # (docs/dataset-catalogue-addendum.md). No `PriceArea` field (already
    # DK2-specific, same shape as `fcr_dk1`'s fixed-zone pattern above).
    #
    # Both currencies are published (`FFR_PriceDKK`/`FFR_PriceEUR`); DKK is
    # mapped as the primary `price` product deliberately -- not because DKK
    # is somehow more "correct", but because it lets FFR stack with
    # `aFRR_capacity` (also DKK) in a BESS capacity_markets config without
    # needing a currency conversion. Without this choice, DK2's stack would
    # be split 100% EUR (FCR) vs. 100% DKK (aFRR) with no shared-currency
    # anchor at all. `price_eur` carries the EUR figure too (both are
    # published, so both are ingested -- closes the question rather than
    # picking one and dropping the other).
    #
    # **Prices are currently `0.0`, not null** (confirmed live 2026-07-21) --
    # they WILL ingest starting from the very first poll, and
    # `shared/rule_engine.py:check_negative_or_zero` WILL evaluate them: it
    # self-suppresses once history is mostly zeros (the "historically rare"
    # threshold check), but may fire spuriously for FFR's first
    # ~`MIN_HISTORY_POINTS` polls after any future non-zero season ends and
    # prices drop back to 0. Not a bug to "fix" here -- documented so it
    # isn't mistaken for one later.
    DatasetConfig(
        name="ffr_dk2",
        dataset_id="FFRDK2",
        market="FFR",
        time_field="HourUTC",
        zone_field=None,
        zone="DK2",
        series=[
            SeriesConfig(product="price", value_field="FFR_PriceDKK", unit="DKK/MW/h"),
            SeriesConfig(product="price_eur", value_field="FFR_PriceEUR", unit="EUR/MW/h"),
            SeriesConfig(product="demand_volume", value_field="FFR_DemandMW", unit="MW"),
            SeriesConfig(product="purchased_volume", value_field="FFR_PurchasedMW", unit="MW"),
        ],
        is_provisional=True,
        params={"limit": 100, "sort": "HourUTC DESC"},
    ),
    # FFR demand curve, DK2-only -- eight step-wise demand levels
    # (`ffrupdemandd0`..`ffrupdemandd7`) from the same auction FFR clears in
    # (confirmed live 2026-07-21). **Field names are lowercase, unlike every
    # other dataset in this registry** (`ffrupdemandd0`, not e.g.
    # `FFRUpDemandD0`) -- this is genuinely how Energinet publishes them for
    # this one dataset, confirmed via live `meta/dataset/FFRdemandDK2`; do
    # not "fix" the casing to match this file's usual PascalCase convention,
    # that would just break the mapping.
    DatasetConfig(
        name="ffr_demand_dk2",
        dataset_id="FFRdemandDK2",
        market="FFR",
        time_field="HourUTC",
        zone_field=None,
        zone="DK2",
        series=[
            SeriesConfig(product=f"demand_step_{i}", value_field=f"ffrupdemandd{i}", unit="MW")
            for i in range(8)
        ],
        is_provisional=True,
        params={"limit": 100, "sort": "HourUTC DESC"},
    ),
    # BESS simulator — Nordic aFRR capacity/reservation market (distinct
    # from `afrr_energy_activation` above, which is the PICASSO
    # *activation/energy* price). Confirmed live 2026-07-17. Up/down
    # procured prices are genuinely asymmetric (separate auctions), so both
    # are mapped as their own products, consistent with `mfrr_capacity`'s
    # up/down pattern.
    #
    # Demand/procured volumes (confirmed live 2026-07-21) added alongside the
    # prices: the demand-vs-procured shortfall is the leading indicator of a
    # capacity price spike -- exactly the kind of explanatory signal
    # `shared/price_recap_synthesizer.py`/`shared/rule_engine.py` want.
    # *Naming note:* `mfrr_eam` (above) uses a bare `up_volume`/`down_volume`
    # pair for a single *requested* volume, since that dataset only publishes
    # one volume figure per direction. This dataset publishes both a demand
    # figure and a procured figure, so the explicit `*_demand_volume`/
    # `*_procured_volume` split is deliberate, not a stylistic upgrade --
    # **`mfrr_eam`'s existing `up_volume`/`down_volume` products are
    # deliberately NOT retro-renamed to match**, since that would orphan
    # already-ingested history under the old product names (the
    # inconsistency is accepted, not "fixed").
    DatasetConfig(
        name="afrr_reserves_nordic",
        dataset_id="AfrrReservesNordic",
        market="aFRR_capacity",
        time_field="TimeUTC",
        zone_field="PriceArea",
        series=[
            SeriesConfig(product="up", value_field="UpPriceDKK", unit="DKK/MW/h"),
            SeriesConfig(product="down", value_field="DownPriceDKK", unit="DKK/MW/h"),
            SeriesConfig(product="up_demand_volume", value_field="UpDemandMW", unit="MW"),
            SeriesConfig(product="down_demand_volume", value_field="DownDemandMW", unit="MW"),
            SeriesConfig(product="up_procured_volume", value_field="UpProcuredMW", unit="MW"),
            SeriesConfig(product="down_procured_volume", value_field="DownProcuredMW", unit="MW"),
            # EUR prices, published alongside the DKK ones above -- optional
            # context (`up`/`down` DKK remain the products BESS revenue
            # modelling actually reads), never summed with any DKK figure.
            SeriesConfig(product="up_eur", value_field="UpPriceEUR", unit="EUR/MW/h"),
            SeriesConfig(product="down_eur", value_field="DownPriceEUR", unit="EUR/MW/h"),
        ],
        is_provisional=True,
        params={"limit": 100, "sort": "TimeUTC DESC"},
    ),
    # High priority — near-real-time system state (wind/solar/CO2), used as
    # explanatory soft-signal context ("low wind" style narratives). No
    # per-record zone; treated as a single system-wide series (zone="ALL").
    DatasetConfig(
        name="power_system_right_now",
        dataset_id="PowerSystemRightNow",
        market="system_state",
        time_field="Minutes1UTC",
        zone_field=None,
        zone="ALL",
        series=[
            SeriesConfig(product="onshore_wind", value_field="OnshoreWindPower", unit="MW"),
            SeriesConfig(product="offshore_wind", value_field="OffshoreWindPower", unit="MW"),
            SeriesConfig(product="solar", value_field="SolarPower", unit="MW"),
            SeriesConfig(product="co2_emission", value_field="CO2Emission", unit="g/kWh"),
        ],
        is_provisional=True,
        params={"limit": 100, "sort": "Minutes1UTC DESC"},
    ),
    # Grid inertia (confirmed live 2026-07-21) -- low Nordic inertia is a
    # causal driver of FCR-D/FFR demand (less rotating mass on the
    # synchronous grid means frequency swings faster after a disturbance, so
    # more disturbance reserve gets procured), which makes this exactly the
    # kind of explanatory context `shared/price_recap_synthesizer.py`'s
    # "why did prices move" job wants -- see that module's `("inertia",
    # "DK2", "dk2")` system-state key.
    #
    # **Zone-heterogeneous record** -- this is the dataset that forces
    # `SeriesConfig.zone` (see that field's docstring in this file):
    # `InertiaNordicGWs` is a single synchronous-area-wide figure (no
    # `PriceArea` field on this dataset at all -- `zone_field=None`), while
    # `InertiaDK2GWs`/`InertiaNOGWs`/`InertiaSEGWs`/`InertiaFIGWs` are each
    # one specific zone's own figure, packed as separate columns in the same
    # record rather than separate rows. `market="inertia"` is shared across
    # every series here (all one physical quantity, GWs, just at different
    # spatial granularity) -- `product` (`nordic`/`dk2`/`no`/`se`/`fi`)
    # disambiguates.
    DatasetConfig(
        name="inertia_nordic",
        dataset_id="InertiaNordicSyncharea",
        market="inertia",
        time_field="HourUTC",
        zone_field=None,
        zone="ALL",  # InertiaNordicGWs (the "nordic" product below) is synchronous-area-wide
        series=[
            SeriesConfig(product="nordic", value_field="InertiaNordicGWs", unit="GWs"),
            SeriesConfig(product="dk2", value_field="InertiaDK2GWs", unit="GWs", zone="DK2"),
            SeriesConfig(product="no", value_field="InertiaNOGWs", unit="GWs", zone="NO"),
            SeriesConfig(product="se", value_field="InertiaSEGWs", unit="GWs", zone="SE"),
            SeriesConfig(product="fi", value_field="InertiaFIGWs", unit="GWs", zone="FI"),
        ],
        is_provisional=True,
        params={"limit": 100, "sort": "HourUTC DESC"},
    ),
]
