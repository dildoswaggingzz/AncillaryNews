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

Field names are taken verbatim from `docs/dataset-catalogue.md` (the M0 audit),
not invented. Known gaps from that audit, carried forward here:

- No dataset exposes an explicit `PublishedTime`/`RevisedTime` field, so
  `is_provisional` is a static, catalogue-derived best guess per dataset, not
  something Energinet tells us per-record.
- The exact "mFRR EAM energy activation" dataset the README treats as the
  primary focus could not be confirmed to exist (catalogue §8.1) and is
  therefore *not* included below. `mFRRCapacityMarket` (reservation/capacity
  payments) is included as the closest confirmed substitute.
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
