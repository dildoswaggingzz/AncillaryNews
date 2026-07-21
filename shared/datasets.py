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
    # Declares whether this dataset publishes records for FUTURE delivery
    # times -- a D-1 auction clearing (e.g. `fcr_dk2`'s FCR-D/FCR-N D-1
    # auction) or a forecast (e.g. `forecasts_hour`) that reveals tomorrow's
    # values today, as opposed to a realised measurement or same-period
    # settlement that only ever has data up to "now" (e.g.
    # `imbalance_price`, `prodex_5min_realtime`).
    #
    # **Why this field exists (a real, live bug):** every `DatasetConfig`
    # polls with `sort=<time_field> DESC` and a fixed `limit` -- for a
    # forward-publishing dataset, the newest `limit` records by that sort
    # are tomorrow's delivery periods, so the poll window never reaches the
    # present and captures zero usable history. Confirmed live 2026-07-21:
    # `day_ahead_prices`/`fcr_dk2`/`afrr_reserves_nordic`/`forecasts_hour`
    # each returned 0 past/now records under the pre-fix `limit`-only
    # params (`FCR` DK2 had a 25-day hole in `market_data_history` as a
    # result). The fix is an explicit `start` param bounding the poll
    # window from the past side (see `FORWARD_PUBLISH_START` /
    # `_forward_publish_params` below) -- but a *future* dataset addition
    # must not silently reintroduce this bug by omitting it. Declaring the
    # property here, rather than leaving it implicit in whether a `start`
    # key happens to be hand-typed into `params`, is what lets
    # `tests/test_datasets.py`'s
    # `test_forward_publishing_datasets_declare_a_start_param` catch that at
    # CI time: any dataset with `forward_publish_horizon` set MUST have
    # `start` in `params`, so setting this field without also setting
    # `start` fails the suite immediately, and forgetting to set this field
    # at all for a genuinely forward-publishing dataset is a registry
    # authoring mistake this test cannot catch (that judgment call --
    # "does this dataset publish into the future" -- has to be verified
    # live per dataset; see the datasets flagged as unclassified in the
    # git history around this field's introduction).
    #
    # `None` (default): backward/realised-only, or genuinely uninvestigated
    # (see individual entries below for which). A relative-time string
    # (Energinet's own syntax, e.g. `"P1D"`) when set: documents *how far*
    # forward this dataset currently publishes, for a reader's benefit --
    # informational only, NOT machine-parsed to size `params["limit"]` (the
    # limit is sized per-dataset from measured record-rate arithmetic, see
    # each entry's comment), since the actual forward reach fluctuates
    # through the day (e.g. right after a D-1 auction clears vs. just
    # before the next one) in a way a single horizon string can't capture
    # precisely enough to safely drive a `limit` computation.
    forward_publish_horizon: str | None = None


# Relative-time margin used as `start` for every forward-publishing
# dataset's poll (see `DatasetConfig.forward_publish_horizon` above).
# "now-P2D" is Energinet's documented relative-time syntax, confirmed live
# against the real API (2026-07-21) to fix the zero-past-records bug:
# `day_ahead_prices`/`fcr_dk2`/`afrr_reserves_nordic`/`forecasts_hour` all
# went from 0 past/now records to a healthy several-day-deep window under
# this exact value.
#
# Deliberately NOT widened further "just in case": every extra day of
# margin is extra rate-limit budget spent on every single 15-minute poll
# cycle, against an API that already rate-limits aggressively per-IP (see
# services/ingestor/main.py's RATE_LIMIT_SECONDS and shared/backfill.py's
# module docstring on 429s). P2D (48h) is chosen specifically as a margin
# that lets a handful of missed poll cycles self-heal (the live poller
# fills any gap on its very next successful cycle) without needing a wider
# window "for safety" -- a genuinely missed 48h+ of cycles is an outage
# `shared/backfill.py`'s manual/on-demand backfill exists to repair, not
# something the live poller's steady-state margin should be sized around.
FORWARD_PUBLISH_START = "now-P2D"


def _forward_publish_params(sort_field: str, limit: int) -> dict:
    """
    Builds the `params` dict for a forward-publishing `DatasetConfig` (one
    with `forward_publish_horizon` set). Centralizing this is the point:
    every call site gets `start=FORWARD_PUBLISH_START` for free, so a
    forward-publishing entry built this way cannot omit it by a copy-paste
    slip the way a hand-typed `params={...}` dict could.

    Without an explicit `start`, `sort=<field> DESC` + a fixed `limit`
    returns only the newest (i.e. furthest-future) `limit` records for a
    forward-publishing dataset and never reaches the present -- see
    `DatasetConfig.forward_publish_horizon`'s docstring for the live defect
    this exists to prevent from recurring.
    """
    return {"limit": limit, "sort": f"{sort_field} DESC", "start": FORWARD_PUBLISH_START}


DATASETS: list[DatasetConfig] = [
    # High priority — mFRR capacity (reservation) market. Closest confirmed
    # substitute for the unconfirmed "mFRR EAM energy activation" dataset;
    # this is capacity/reservation payments, not activation/energy payments.
    #
    # **Forward-publishing, thin margin (confirmed live 2026-07-21):** this
    # is a D-1 capacity auction -- under the pre-fix `limit`-only params
    # (sort `TimeUTC DESC`, no `start`), a poll returned 27 future times vs.
    # only 23 past times, i.e. the fixed `limit=100` window reached back
    # just ~21-23h before running out, not zero like `fcr_dk2`/
    # `afrr_reserves_nordic`/`day_ahead_prices`/`forecasts_hour` below, but
    # thin enough to be in-scope for the same `start` fix (see
    # `DatasetConfig.forward_publish_horizon`'s docstring). **Limit
    # arithmetic:** 100 records / 50 distinct hours (27 future + 23 past) =
    # 2 records/hour (DK1 + DK2, one row per zone-hour, `up`/`down` share a
    # row). Fixed past window `FORWARD_PUBLISH_START` gives 48h; forward
    # reach is bounded to the D-1 auction's own ~1-day-ahead horizon, so a
    # 48h worst-case margin on that side too is conservative. Total window
    # ~96h x 2 records/hour = 192 records; `limit=250` leaves modest (~30%)
    # headroom without over-provisioning (API etiquette: every extra record
    # is rate-limit budget on an aggressively-limited API).
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
        forward_publish_horizon="P1D",
        params=_forward_publish_params("TimeUTC", 250),
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
    #
    # **Forward-publishing, thin margin** -- same `MfrrCapacityMarketExtra`
    # shape/dimensioning as `mfrr_capacity` above (confirmed live 2026-07-21:
    # same ~27 future / ~23 past time split under the pre-fix params), so
    # the same arithmetic applies: 2 records/hour (DK1+DK2), 96h worst-case
    # window (48h fixed past + ~48h D-1-bounded forward) => ~192 records;
    # `limit=250` for the same ~30% headroom reasoning as `mfrr_capacity`.
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
        forward_publish_horizon="P1D",
        params=_forward_publish_params("TimeUTC", 250),
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
    #
    # **Forward-publishing -- zero past/now records under the pre-fix params
    # (confirmed live 2026-07-21).** `DayAheadPrices` is a D-1 auction
    # (NordPool day-ahead), now 15-minute MTU (docs/forecast-datasets-scope.md
    # §3's target table), and this Energi Data Service dataset publishes
    # `PriceArea`-broad -- no zone filter is applied at query time (this
    # entry's `zone_field="PriceArea"` maps whatever zones the raw feed
    # contains, not a fixed DK1/DK2 pair), so the auction reveal spans
    # every zone the feed carries at once. The old `sort=TimeUTC DESC,
    # limit=100` (no `start`) returned 17 distinct 15-min slots, ALL future
    # -- 0 past/now, the exact live defect this field/params fix exists for.
    #
    # **Limit arithmetic (measured live with `start=now-P2D`):** 1818
    # records / 303 distinct 15-min slots = 6.0 records/slot (confirms this
    # dataset's zone count is effectively constant at 6 for this feed, not
    # something this entry needs to enumerate). The past side of the window
    # is fixed by `FORWARD_PUBLISH_START` at 48h = 192 fifteen-minute slots
    # -- and the measured 192 past slots in that same live check match this
    # exactly, confirming the 15-min-slot assumption. Forward reach is
    # bounded by the auction's own mechanics (reveals one full calendar day
    # once cleared, ~12:00 UTC daily) to a worst case of ~48h forward (right
    # after a clearing, with today's remainder still outstanding) = another
    # 192 slots. Worst-case total: 384 slots x 6 records/slot = 2304
    # records; `limit=2500` leaves ~10% headroom -- modest headroom
    # deliberately, not overprovisioned (API etiquette).
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
        forward_publish_horizon="P1D",
        params=_forward_publish_params("TimeUTC", 2500),
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
    #
    # **Forward-publishing (confirmed live 2026-07-21, same sweep as the
    # rest of this bugfix).** `FcrDK1` is the FCR Cooperation's own D-1
    # auction -- a pre-fix poll (`sort=HourUTC DESC`, no `start`) returned
    # 100 records / 100 distinct hours, 27 future / 73 past, oldest reached
    # 2026-07-18T18:00 (~3 days back).
    #
    # **That 3-day reach was accidental, not designed -- this is the fact a
    # future reader most needs.** At the measured 1.0 record/hour (100
    # records over 100 distinct hours -- one row per hour, no PriceArea
    # split to multiply it), `limit=100` happens to span ~100h, comfortably
    # past "now" despite the missing `start`. That adequacy holds only while
    # record density stays at 1/hour: if Energinet ever adds a price area,
    # a product, or an auction-type split to this dataset -- exactly what
    # `fcr_dk2`'s own comment above documents happening to it (30
    # records/hour measured, not the "6/hour" a first glance at that
    # dataset's name would assume) -- this entry fails the identical
    # zero-past-records way, with the identical absence of symptoms, the
    # day that happens. Declaring `forward_publish_horizon` and using
    # `start` now closes that latent recurrence, not just today's measured
    # shortfall.
    #
    # **Limit arithmetic:** 1.0 record/hour measured x 96h worst-case window
    # (48h fixed past via `FORWARD_PUBLISH_START` + ~48h D-1-bounded
    # forward) = 96 records needed. `limit=250` (consistent with
    # `mfrr_capacity`/`mfrr_capacity_extra`) leaves ~160% headroom --
    # deliberately generous here specifically because the current 1/hour
    # density is the fragile assumption above, not because this dataset
    # needs a wide margin on its own account.
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
        forward_publish_horizon="P1D",
        params=_forward_publish_params("HourUTC", 250),
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
    # x 2 auction types = 6/hour" this dataset's name suggests.
    #
    # **Correction, same investigation session -- `limit=100` was NOT fine.**
    # This is a D-1 auction (`AuctionType` literally carries "D-1 early"),
    # i.e. forward-publishing -- `sort=HourUTC DESC` with no `start` means
    # the newest 100 records by that sort are tomorrow's auction hours, not
    # the most recent past ones. Confirmed live 2026-07-21: an unfixed poll
    # returned 3 distinct hours, ALL future, 0 past/now -- this dataset had
    # a 25-day hole in `market_data_history` as a direct result. The
    # "~3.3h of raw record coverage... comfortably more than the 15-minute
    # live-poll cadence needs" reasoning above assumed the window's floor
    # was "now"; it never was. See `DatasetConfig.forward_publish_horizon`'s
    # docstring for the general fix.
    #
    # **New limit arithmetic (measured live with `start=now-P2D`):** 3375
    # records / 75 distinct hours = 45.0 records/hour -- higher than the
    # 30/hour single-hour sample above (that sample evidently caught a
    # moment with fewer than all 5 PriceAreas reporting, or fewer than all
    # 6 product/auction combos; 45/hour is the figure actually observed
    # across the fixed window and is what this `limit` is sized from). Past
    # side fixed by `FORWARD_PUBLISH_START` at 48h; the measured window's 48
    # past hours match exactly. Forward reach is bounded by the D-1
    # auction's own ~1-day-ahead horizon -- worst case ~48h forward.
    # Worst-case total: 96h x 45 records/hour = 4320 records; `limit=4500`
    # leaves ~4% headroom on top of that worst case (deliberately modest,
    # not "days more just in case" -- API etiquette). Adding the volume/D-1
    # series above extracts more *columns* from these same fetched records
    # -- it does not change how many raw records `limit` needs to cover.
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
        forward_publish_horizon="P1D",
        params=_forward_publish_params("HourUTC", 4500),
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
    #
    # **Forward-publishing (confirmed live 2026-07-21, same sweep as the
    # rest of this bugfix).** FFR clears in a D-1 auction, same mechanics as
    # `fcr_dk1`/`ffr_demand_dk2` -- a pre-fix poll (`sort=HourUTC DESC`, no
    # `start`) returned 100 records / 100 distinct hours, 27 future / 73
    # past, oldest reached 2026-07-18T18:00 (~3 days back).
    #
    # **That 3-day reach is accidental, not designed** -- see `fcr_dk1`'s
    # comment above for the full reasoning: at 1.0 record/hour (no
    # PriceArea split, one row per hour), `limit=100` happens to reach past
    # "now" today, but that headroom evaporates the moment Energinet adds
    # any per-record split to this dataset (a live-documented pattern --
    # see `fcr_dk2`'s comment), at which point this fails the identical
    # zero-past-records way with the identical absence of symptoms.
    #
    # **Limit arithmetic:** 1.0 record/hour measured x 96h worst-case window
    # (48h fixed past + ~48h D-1-bounded forward) = 96 records needed;
    # `limit=250` (consistent with `mfrr_capacity`/`fcr_dk1`) leaves ~160%
    # headroom for exactly that fragile-density scenario.
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
        forward_publish_horizon="P1D",
        params=_forward_publish_params("HourUTC", 250),
    ),
    # FFR demand curve, DK2-only -- eight step-wise demand levels
    # (`ffrupdemandd0`..`ffrupdemandd7`) from the same auction FFR clears in
    # (confirmed live 2026-07-21). **Field names are lowercase, unlike every
    # other dataset in this registry** (`ffrupdemandd0`, not e.g.
    # `FFRUpDemandD0`) -- this is genuinely how Energinet publishes them for
    # this one dataset, confirmed via live `meta/dataset/FFRdemandDK2`; do
    # not "fix" the casing to match this file's usual PascalCase convention,
    # that would just break the mapping.
    #
    # **Forward-publishing (confirmed live 2026-07-21, same sweep as the
    # rest of this bugfix).** Same D-1 auction this dataset's demand curve
    # comes from -- a pre-fix poll (`sort=HourUTC DESC`, no `start`)
    # returned 100 records / 100 distinct hours, 27 future / 73 past,
    # oldest reached 2026-07-18T18:00 (~3 days back).
    #
    # **That 3-day reach is accidental, not designed** -- see `fcr_dk1`'s
    # comment above: at 1.0 record/hour (no PriceArea split, one row per
    # hour), `limit=100` happens to reach past "now" today only because of
    # this dataset's current record density, which evaporates the moment
    # Energinet adds any per-record split (the live-documented pattern --
    # see `fcr_dk2`'s comment), reintroducing the identical zero-past-
    # records failure with the identical absence of symptoms.
    #
    # **Limit arithmetic:** 1.0 record/hour measured x 96h worst-case window
    # (48h fixed past + ~48h D-1-bounded forward) = 96 records needed;
    # `limit=250` (consistent with `mfrr_capacity`/`fcr_dk1`/`ffr_dk2`)
    # leaves ~160% headroom for that same fragile-density scenario.
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
        forward_publish_horizon="P1D",
        params=_forward_publish_params("HourUTC", 250),
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
    #
    # **Forward-publishing -- zero past/now records under the pre-fix params
    # (confirmed live 2026-07-21).** `AfrrReservesNordic` is a D-1 capacity
    # auction across the Nordic PriceAreas -- the old `sort=TimeUTC DESC,
    # limit=100` (no `start`) returned only 9 distinct hours, ALL future --
    # 0 past/now. **Limit arithmetic (measured live with `start=now-P2D`):**
    # 900 records / 75 distinct hours = 12.0 records/hour (empirical rate
    # across however many Nordic PriceAreas this feed actually carries per
    # hour -- not independently re-enumerated here, this measured rate is
    # the arithmetic input). Past side fixed by `FORWARD_PUBLISH_START` at
    # 48h (measured window's 48 past hours confirm this). Forward reach
    # bounded by the D-1 auction's own ~1-day-ahead horizon -- worst case
    # ~48h forward. Worst-case total: 96h x 12 records/hour = 1152 records;
    # `limit=1300` leaves ~13% headroom, deliberately modest.
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
        forward_publish_horizon="P1D",
        params=_forward_publish_params("TimeUTC", 1300),
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
    # --- M6 P0: fundamentals gap (docs/forecast-datasets-scope.md §2 Tier 1) ---
    #
    # Wind/solar generation forecast, hourly, per price area and per
    # forecast type. Confirmed live 2026-07-20. `filter_field="ForecastType"`
    # follows the `fcr_dk2` precedent above (a shared value shape --
    # `ForecastDayAhead`/`ForecastIntraday`/`Forecast5Hour`/`Forecast1Hour`/
    # `ForecastCurrent` -- distinguished per record by a categorical field,
    # here `ForecastType` instead of `ProductName`). Three ForecastType
    # values confirmed live: "Offshore Wind", "Onshore Wind", "Solar".
    # `meta/dataset/Forecasts_Hour` labels every horizon column "MWh per
    # hour", not the bare "MW" this file uses elsewhere (e.g.
    # `power_system_right_now`'s wind/solar) for the same physical quantity
    # -- energy-per-hour and power are numerically identical, so `unit="MW"`
    # below is deliberate, not an oversight; kept consistent with this
    # registry's existing convention rather than Energinet's own
    # inconsistent per-dataset labeling.
    #
    # **`ForecastCurrent` is leak-contaminated -- never use it as a model
    # feature** (docs/forecast-datasets-scope.md §1.3, re-confirmed live
    # 2026-07-20: a same-day sample showed `ForecastCurrent ==
    # ForecastDayAhead` on a not-yet-elapsed hour, and the module's own
    # verified example shows it equalling `ForecastIntraday` once that
    # horizon has passed). It is the *last-revised* value as of ingestion
    # time, i.e. it silently uses information that would not have been
    # available at bid time -- a model trained on it backtests beautifully
    # and fails live. Rather than simply omitting the column (losing the
    # "what did Energinet believe most recently" figure entirely, which has
    # legitimate audit/display value), it is ingested under a product name
    # that makes the hazard impossible to miss at the call site:
    # `f"{type}_current_leaky_do_not_use_as_feature"`. Every consumer -- P1's
    # leak-safe feature builder above all -- must treat this product name as
    # a hard exclusion list entry, not just a column to be careful with.
    #
    # **Forward-publishing -- zero past/now records under the pre-fix params
    # (confirmed live 2026-07-21).** This is the P1 feature store's core
    # fundamentals input, and by definition every row's `HourUTC` is the
    # delivery hour a forecast was made *for*, which `ForecastDayAhead`
    # reveals up to a day before it arrives -- the old `sort=HourUTC DESC,
    # limit=100` (no `start`) returned 17 distinct hours, ALL future, 0
    # past/now. **Limit arithmetic (measured live with `start=now-P2D`):**
    # 450 records / 75 distinct hours = 6.0 records/hour (3 ForecastType
    # values x 2 PriceAreas -- DK1/DK2, matching this dataset's declared
    # zone scope). Past side fixed by `FORWARD_PUBLISH_START` at 48h
    # (measured window's 48 past hours confirm this). Forward reach bounded
    # by the forecast's own ~1-day-ahead horizon -- worst case ~48h forward.
    # Worst-case total: 96h x 6 records/hour = 576 records; `limit=650`
    # leaves ~13% headroom, deliberately modest.
    DatasetConfig(
        name="forecasts_hour",
        dataset_id="Forecasts_Hour",
        market="wind_solar_forecast",
        time_field="HourUTC",
        zone_field="PriceArea",
        series=[
            series
            for forecast_type, slug in (
                ("Offshore Wind", "offshore_wind"),
                ("Onshore Wind", "onshore_wind"),
                ("Solar", "solar"),
            )
            for series in (
                SeriesConfig(
                    product=f"{slug}_day_ahead",
                    value_field="ForecastDayAhead",
                    unit="MW",
                    filter_field="ForecastType",
                    filter_value=forecast_type,
                ),
                SeriesConfig(
                    product=f"{slug}_intraday",
                    value_field="ForecastIntraday",
                    unit="MW",
                    filter_field="ForecastType",
                    filter_value=forecast_type,
                ),
                SeriesConfig(
                    product=f"{slug}_5hour",
                    value_field="Forecast5Hour",
                    unit="MW",
                    filter_field="ForecastType",
                    filter_value=forecast_type,
                ),
                SeriesConfig(
                    product=f"{slug}_1hour",
                    value_field="Forecast1Hour",
                    unit="MW",
                    filter_field="ForecastType",
                    filter_value=forecast_type,
                ),
                # LEAK HAZARD -- see the dataset-level comment above. Do not
                # read this product from any forecasting feature builder.
                SeriesConfig(
                    product=f"{slug}_current_leaky_do_not_use_as_feature",
                    value_field="ForecastCurrent",
                    unit="MW",
                    filter_field="ForecastType",
                    filter_value=forecast_type,
                ),
            )
        ],
        is_provisional=True,
        forward_publish_horizon="P1D",
        params=_forward_publish_params("HourUTC", 650),
    ),
    # Realised production and cross-border exchange, 5-minute resolution,
    # per price area. Confirmed live 2026-07-20. Not one of the §1.2
    # 90-day-retention datasets (history from 2014-12-31), so the default
    # `limit=100` (~4h+ of coverage across both zones at 1 record/5min/zone)
    # is unchanged from this file's usual default -- see the millisecond
    # datasets below for where `limit` actually needs sizing arithmetic.
    #
    # Differenced against `forecasts_hour`'s `*_1hour`/`*_5hour` columns
    # (P1), this is what turns a forecast into a forecast *error* -- the
    # single most useful engineered feature `docs/forecast-datasets-scope.md`
    # §1.1/§2 identifies. `BornholmSE4` is genuinely null on many DK1 rows
    # and genuinely populated on DK2 rows (Bornholm is electrically part of
    # DK2's SE4 interconnection) -- not a typo, matches
    # `shared/db_manager.py`'s "missing series' value field simply omits
    # that product" handling with no special-casing needed here.
    DatasetConfig(
        name="prodex_5min_realtime",
        dataset_id="ElectricityProdex5MinRealtime",
        market="realtime_production_exchange",
        time_field="Minutes5UTC",
        zone_field="PriceArea",
        series=[
            SeriesConfig(product="production_lt100mw", value_field="ProductionLt100MW", unit="MW"),
            SeriesConfig(product="production_ge100mw", value_field="ProductionGe100MW", unit="MW"),
            SeriesConfig(product="offshore_wind", value_field="OffshoreWindPower", unit="MW"),
            SeriesConfig(product="onshore_wind", value_field="OnshoreWindPower", unit="MW"),
            SeriesConfig(product="solar", value_field="SolarPower", unit="MW"),
            SeriesConfig(product="exchange_great_belt", value_field="ExchangeGreatBelt", unit="MW"),
            SeriesConfig(product="exchange_germany", value_field="ExchangeGermany", unit="MW"),
            SeriesConfig(
                product="exchange_netherlands", value_field="ExchangeNetherlands", unit="MW"
            ),
            SeriesConfig(
                product="exchange_great_britain", value_field="ExchangeGreatBritain", unit="MW"
            ),
            SeriesConfig(product="exchange_norway", value_field="ExchangeNorway", unit="MW"),
            SeriesConfig(product="exchange_sweden", value_field="ExchangeSweden", unit="MW"),
            SeriesConfig(product="exchange_bornholm_se4", value_field="BornholmSE4", unit="MW"),
        ],
        is_provisional=True,
        params={"limit": 100, "sort": "Minutes5UTC DESC"},
    ),
    # PICASSO cross-border available transfer capacity for aFRR -- **one of
    # the three §1.2 90-day-rolling-retention datasets**; live-reconfirmed
    # 2026-07-20 that the window is genuinely rolling (earliest record had
    # advanced from 2026-04-20T09:00 at the scope doc's audit time earlier
    # today to 2026-04-20T18:00 by the time this entry was written -- ~9
    # hours later, same calendar day. Every day this sits un-ingested
    # destroys a day of aFRR-border training data with no way to recover it.
    #
    # **Schema mismatch, flagged rather than forced (see task instructions):
    # this dataset has no `PriceArea` field at all.** It is keyed by
    # `BorderName` (a corridor, e.g. "DK1-DE", "DK2-DK1" -- confirmed live;
    # see below), not a bidding zone. `market_data_history.zone` is a single
    # column every other dataset in this registry populates with an actual
    # bidding-zone code (DK1/DK2/SE3/.../ALL) that `shared/units.py` and
    # `shared/rule_engine.py`'s DK1-vs-DK2 divergence checks both assume.
    # `BorderName` doesn't fit that shape:
    #   - `DK1-DE`/`DK2-DE` are unambiguous -- each zone's own external
    #     interconnector to Germany. **Correction from an earlier, narrower
    #     live check**: a first 500-record/~33-minute sample (2026-07-20)
    #     only showed `DK1-DE` and `DK2-DK1`, and this comment briefly (in
    #     the same change) concluded DK2 had no external aFRR-ATC border of
    #     its own -- a full one-day backfill run moments later (same date)
    #     surfaced `DK2-DE` too (2,648/2,168 export/import rows), proving
    #     that conclusion wrong. Left here as a concrete, live example of
    #     why design (b) below (auto-discovery, not enumeration) is the
    #     right call: a narrow sample is not a safe basis for a hardcoded
    #     BorderName enumeration, and this dataset's own volume made that
    #     obvious within the same working session.
    #   - `DK2-DK1` (the Storebælt/Great Belt internal cable) is NOT a
    #     bidding-zone-external border at all -- it's the constraint
    #     *between* the two Danish zones this whole system models. Neither
    #     "DK1" nor "DK2" alone is a correct zone label for it.
    #
    # Two designs were considered:
    #   (a) Enumerate each (BorderName, Direction) pair as its own
    #       `SeriesConfig` with a fixed per-series `zone` override (the
    #       `inertia_nordic` pattern above) -- consistent with this file's
    #       usual style, but **silently drops any future BorderName**
    #       Energinet adds until someone updates this registry entry. Given
    #       this dataset's entire reason for urgency is "every unarchived
    #       day is destroyed permanently," a silent enumeration gap is
    #       exactly the failure mode P0 exists to prevent.
    #   (b) `zone_field="BorderName"` -- the raw corridor string flows
    #       straight into `market_data_history.zone`, auto-covering any
    #       BorderName Energinet publishes, present or future, with zero
    #       registry maintenance. **Chosen** for that reason, but it means
    #       `zone` for this one dataset/market is a corridor identifier
    #       ("DK1-DE", "DK2-DK1"), not a bidding-zone code -- `zone="DK1"`
    #       will NOT return this dataset's DK1-DE row. Any future consumer
    #       (P1's feature builder in particular) must know this dataset is
    #       the one exception and derive a bidding-zone split from the
    #       `BorderName` prefix itself if it needs one (e.g.
    #       `BorderName.split("-")[0]`), not query by zone directly.
    #
    # **This is a real schema tension, not a clean fit -- flagged in the M6
    # P0 report for operator sign-off, not resolved unilaterally.** A P1
    # follow-up could add a `zone_from_field`/derivation hook to
    # `SeriesConfig`/`shared/db_manager.py` so this data could be
    # re-projected onto true bidding zones without losing choice (b)'s
    # auto-discovery property; not built here to avoid a schema change
    # mid-way through an urgent, otherwise-additive-only P0 ingestion task.
    #
    # `Direction` ("Import"/"Export"/occasionally "N/A" -- the latter left
    # unmapped, seen live but undocumented in `meta/dataset`) is the
    # remaining categorical field, handled the usual `filter_field` way.
    #
    # **`limit` sizing (confirmed live 2026-07-20, `fcr_dk2`-style
    # arithmetic):** a full one-day backfill run returned 23,523 records
    # across every BorderName/Direction combo active that day (DK1-DE,
    # DK2-DE, DK2-DK1 x Import/Export) -- ~16.3 records/min, so a 15-minute
    # poll window needs >=~245 records for what's active today. `limit=2000`
    # leaves >8x headroom for additional borders Energinet may start
    # publishing (which, per design (b) above, this entry will pick up
    # automatically with no further registry change).
    DatasetConfig(
        name="afrr_border_atc",
        dataset_id="AfrrBorderAvailableTransferCapacity",
        market="aFRR_border_atc",
        time_field="TimeMsUTC",
        zone_field="BorderName",
        series=[
            SeriesConfig(
                product="import",
                value_field="Limit",
                unit="MW",
                filter_field="Direction",
                filter_value="Import",
            ),
            SeriesConfig(
                product="export",
                value_field="Limit",
                unit="MW",
                filter_field="Direction",
                filter_value="Export",
            ),
        ],
        is_provisional=True,
        params={"limit": 2000, "sort": "TimeMsUTC DESC"},
    ),
    # aFRR LFC activation limits -- Energinet's own hard ceiling on how much
    # energy the Load Frequency Controller is allowed to activate, per price
    # area and direction. The third of the three 90-day-rolling-retention
    # datasets in the M6 scope's §1.2 (`docs/forecast-datasets-scope.md`);
    # `meta/dataset` states the window explicitly ("includes data from the
    # last 3 months"), so the same "every unarchived day is destroyed
    # permanently" urgency applies here as to `afrr_energy_activation` and
    # `afrr_border_atc`.
    #
    # Forecast value: this is a *binding constraint on the aFRR activation
    # price*, not merely a correlate. When the LFC hits its limit it cannot
    # activate further aFRR energy regardless of the merit order, so the
    # marginal price decouples -- the same mechanism as border ATC exhaustion
    # in `afrr_border_atc` above, but internal rather than cross-border. The
    # headroom between realised activation and this limit is the natural
    # derived feature, not the raw limit.
    #
    # **Volume correction to the scope doc (measured live 2026-07-20):** §1.2
    # groups this with the other two as a "millisecond dataset", which reads
    # as high-frequency. It is not. A 3000-record `TimeMsUTC DESC` pull spans
    # 16.2 days -- ~185 records/day across both zones (~0.13/min, one update
    # per zone per ~15.6 min), versus `afrr_energy_activation`'s confirmed
    # ~172,400/day. It is millisecond-*timestamped*, not high-cadence. Two
    # practical consequences: its full 90-day backfill is ~16,650 records
    # (seconds, not the ~20-25 min `afrr_energy_activation` needs), and it
    # needs no `--chunk-days 1` special-casing.
    #
    # **`limit` sizing** (`fcr_dk2`-style arithmetic): ~1.9 records per
    # 15-minute poll window, so the file-default `limit=100` carries ~50x
    # headroom and ~13h of coverage. Left at the default deliberately -- the
    # elevated `limit=2000` on `afrr_border_atc` above is not the pattern to
    # copy here; that entry needed it, this one does not.
    #
    # Unlike `afrr_border_atc`, this dataset has a real `PriceArea` (DK1/DK2),
    # so it maps cleanly onto the usual `zone` convention with none of that
    # entry's corridor-vs-bidding-zone tension.
    DatasetConfig(
        name="afrr_lfc_limits",
        dataset_id="AfrrLfcActivationLimits",
        market="aFRR_lfc_limits",
        time_field="TimeMsUTC",
        zone_field="PriceArea",
        series=[
            SeriesConfig(product="up", value_field="LimitUp", unit="MW"),
            SeriesConfig(product="down", value_field="LimitDown", unit="MW"),
        ],
        is_provisional=True,
        params={"limit": 100, "sort": "TimeMsUTC DESC"},
    ),
]
