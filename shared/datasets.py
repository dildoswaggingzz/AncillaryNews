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
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SeriesConfig:
    """One value column within a dataset record, mapped to a market_data product."""

    product: str
    value_field: str
    # Override the dataset-level market label for this series, if the same
    # dataset spans more than one logical market (unused today, kept for
    # forward-compatibility with e.g. combined capacity+energy datasets).
    market: str | None = None
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
            SeriesConfig(product="up", value_field="UpPriceDKK"),
            SeriesConfig(product="down", value_field="DownPriceDKK"),
        ],
        is_provisional=True,
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
            SeriesConfig(product="activation_price", value_field="aFRR_ActivatedEUR"),
            # aFRR_Activated (MW): confirmed live via the dataset's own API
            # metadata (`meta/dataset/AfrrEnergyActivation`) — "Activation in
            # MW. Positive value is up regulation, negative value is down
            # regulation." A single signed field, not an up/down pair, so one
            # product (not "up_volume"/"down_volume") is enough to carry it.
            SeriesConfig(product="activation_volume", value_field="aFRR_Activated"),
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
            SeriesConfig(product="up", value_field="mFRRSAUpEUR"),
            SeriesConfig(product="down", value_field="mFRRSADownEUR"),
            SeriesConfig(product="up_volume", value_field="mFRRSAUpReqMW"),
            SeriesConfig(product="down_volume", value_field="mFRRSADownReqMW"),
            SeriesConfig(product="up_total_volume", value_field="TotalmFRRUpMW"),
            SeriesConfig(product="down_total_volume", value_field="TotalmFRRDownMW"),
            SeriesConfig(product="up_offered_volume", value_field="mFRROfferedUpMW"),
            SeriesConfig(product="down_offered_volume", value_field="mFRROfferedDownMW"),
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
            SeriesConfig(product="correction_volume", value_field="Correction"),
            SeriesConfig(product="up", value_field="PriceUpEUR"),
            SeriesConfig(product="down", value_field="PriceDownEUR"),
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
            SeriesConfig(product="imbalance_price", value_field="ImbalancePriceDKK"),
            SeriesConfig(product="afrr_vwa_up", value_field="aFRRVWAUpDKK"),
            SeriesConfig(product="afrr_vwa_down", value_field="aFRRVWADownDKK"),
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
            SeriesConfig(product="price", value_field="DayAheadPriceDKK"),
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
            SeriesConfig(product="price", value_field="FCRdk_DKK"),
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
                filter_field="ProductName",
                filter_value="FCR-N",
                extra_filters={"AuctionType": "Total"},
            ),
            SeriesConfig(
                product="down",
                value_field="PriceTotalEUR",
                filter_field="ProductName",
                filter_value="FCR-D ned",
                extra_filters={"AuctionType": "Total"},
            ),
            SeriesConfig(
                product="up",
                value_field="PriceTotalEUR",
                filter_field="ProductName",
                filter_value="FCR-D upp",
                extra_filters={"AuctionType": "Total"},
            ),
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
    DatasetConfig(
        name="afrr_reserves_nordic",
        dataset_id="AfrrReservesNordic",
        market="aFRR_capacity",
        time_field="TimeUTC",
        zone_field="PriceArea",
        series=[
            SeriesConfig(product="up", value_field="UpPriceDKK"),
            SeriesConfig(product="down", value_field="DownPriceDKK"),
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
            SeriesConfig(product="onshore_wind", value_field="OnshoreWindPower"),
            SeriesConfig(product="offshore_wind", value_field="OffshoreWindPower"),
            SeriesConfig(product="solar", value_field="SolarPower"),
            SeriesConfig(product="co2_emission", value_field="CO2Emission"),
        ],
        is_provisional=True,
        params={"limit": 100, "sort": "Minutes1UTC DESC"},
    ),
]
