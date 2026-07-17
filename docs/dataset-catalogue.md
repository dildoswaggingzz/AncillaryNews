# M0: Energinet Energi Data Service Dataset Catalogue

**Last updated:** July 16, 2026  
**Purpose:** Milestone M0 — catalogue exact dataset IDs from Energinet's Energi Data Service API (https://api.energidataservice.dk) required to ingest all markets mentioned in the README: FCR, aFRR capacity/energy, mFRR capacity/energy (EAM), day-ahead/intraday, imbalance prices, and transmission outages.

**Context:** Dataset names have changed as markets evolve (e.g., the 2025 mFRR EAM go-live replacing older regulating-power datasets). This catalogue is the foundation for all M1+ ingestion work.

---

## 1. Core Balancing Market Datasets

### 1.0 mFRR Energy Activation Market (EAM) — RESOLVED, now confirmed and ingested

> **Update (2026-07-17, M0 addendum follow-up):** This dataset — the
> README's primary-focus market, flagged as §8.1 "not yet confirmed" below —
> **is confirmed live and now ingested** as `mfrr_eam` in
> `shared/datasets.py`. The original audit's static-HTML/marketing-site
> discovery method produced a false negative; querying
> `api.energidataservice.dk/dataset/MfrrEnergyActivationMarket` directly
> (and its `meta/dataset/...` companion) confirms it. See
> `docs/dataset-catalogue-addendum.md` for the discovery notes this section
> folds in. §8.1 and §12's "mFRR EAM Energy" row are left below as the
> historical record of the original (incorrect) gap, not as current status.

| Field | Value |
|-------|-------|
| **Dataset ID** | `MfrrEnergyActivationMarket` |
| **Market/Zone Mapping** | Nordic mFRR Energy Activation Market — DK1, DK2 |
| **Description** | 15-minute MTU clearing prices and volumes for mFRR up/down *energy activation* (as opposed to `mFRRCapacityMarket`'s capacity/reservation payments) |
| **Key Fields** | `TimeUTC`, `PriceArea` (DK1/DK2), `mFRRSAUpEUR`/`mFRRSADownEUR` (SA = shared/standard activation — the real EAM clearing price), `mFRRSAUpReqMW`/`mFRRSADownReqMW` (requested volume), `TotalmFRRUpMW`/`TotalmFRRDownMW`, `mFRROfferedUpMW`/`mFRROfferedDownMW`. Also present but null across ~200 live sample records: `mFRRDAUpEUR`/`mFRRDADownEUR` (a second, apparently-unused activation price path), `mFRRLocalUp/DownMW`, `mFRRSpecialUp/DownMW`. |
| **Revision Field** | None observed |
| **Data Maturity** | Provisional (15-minute figures) |
| **Notes** | Ingested as `mfrr_eam` (`shared/datasets.py`), market label `mFRR_EAM` — kept distinct from `mFRRCapacityMarket`'s `mFRR_capacity` market so capacity/reservation and energy/activation prices are never conflated. Live-verified: real rows landed in `market_data_history` and the rule engine's `check_price_spike` fires on real `mFRR_EAM` data (seeded + live-verified 2026-07-17). |

### 1.1 mFRR (Minute Reserves) — Capacity Market

| Field | Value |
|-------|-------|
| **Dataset ID** | `mFRRCapacityMarket` |
| **Market/Zone Mapping** | mFRR capacity market — both DK1 and DK2 |
| **Description** | Hourly clearing prices and procured/demanded volumes for mFRR up/down reserves |
| **Key Fields** | `TimeUTC`, `TimeDK`, `PriceArea` (DK1/DK2), `UpDemandMW`, `UpProcuredMW`, `UpPriceEUR`, `UpPriceDKK`, `DownDemandMW`, `DownProcuredMW`, `DownPriceEUR`, `DownPriceDKK` |
| **Revision Field** | None observed in sample data; appears to publish hourly final figures |
| **Data Maturity** | Final (hourly published figures; verify with Energinet if subsequent revisions occur) |
| **Notes** | Replaces the older `mfrrRequest` dataset post-2025 mFRR EAM go-live. Primary data source for mFRR capacity prices paid to BSPs. |

### 1.2 mFRR (Minute Reserves) — Reserve Capacity (Historical)

| Field | Value |
|-------|-------|
| **Dataset ID** | `mFrrReservesDK1` (DK1), `mFrrReservesDK2` (DK2) |
| **Market/Zone Mapping** | mFRR capacity reserves per zone — historical data before mFRRCapacityMarket standardization |
| **Description** | Historical hourly mFRR reserve volumes (expected, purchased) and prices (up/down, including "Xtra" variants) per bidding zone |
| **Key Fields** | `HourUTC`, `HourDK`, `mFRR_DownExpected`, `mFRR_DownPurchased`, `mFRR_DownPriceDKK`, `mFRR_DownPriceEUR`, `mFRR_DownExpectedXtra`, `mFRR_DownPurchasedXtra`, `mFRR_DownPriceXtraDKK`, `mFRR_DownPriceXtraEUR`, `mFRR_UpExpected`, `mFRR_UpPurchased`, `mFRR_UpPriceDKK`, `mFRR_UpPriceEUR`, `mFRR_UpExpectedXtra`, `mFRR_UpPurchasedXtra`, `mFRR_UpPriceXtraDKK`, `mFRR_UpPriceXtraEUR` |
| **Revision Field** | Not confirmed; Energinet should be queried for revision schedule |
| **Data Maturity** | Provisional/Final (status unconfirmed; recommend verifying with Energinet support) |
| **Notes** | These datasets may be superseded by `mFRRCapacityMarket` for recent data. Retain for historical backtesting. "Xtra" variants relate to Danish market-specific product options. |

### 1.3 aFRR (Automatic Frequency Restoration) — Capacity Market

| Field | Value |
|-------|-------|
| **Dataset ID** | `aFrrReservesDK1` (DK1); **note:** `aFrrReservesDK2` returns "not found" — DK2 aFRR may be in a separate dataset or under different naming |
| **Market/Zone Mapping** | aFRR capacity market — Nordic shared market covering both DK1/DK2 |
| **Description** | Hourly aFRR capacity clearing prices and activated volumes per zone |
| **Key Fields** | To be confirmed by API fetch (rate-limited during research); expected: `HourUTC`, `HourDK`, `PriceArea`, `aFRR_DownActivated`, `aFRR_DownPriceDKK`, `aFRR_DownPriceEUR`, `aFRR_UpActivated`, `aFRR_UpPriceDKK`, `aFRR_UpPriceEUR` |
| **Revision Field** | Not confirmed |
| **Data Maturity** | Likely provisional/revised hourly; unconfirmed |
| **Notes** | **Action item:** Verify if DK2 aFRR is in a separate dataset or if naming differs. Energinet support or data portal documentation should clarify. |

### 1.4 aFRR Energy Activation

| Field | Value |
|-------|-------|
| **Dataset ID** | `aFRREnergyActivation` (listed as `AfrrEnergyActivation` in API response) |
| **Market/Zone Mapping** | aFRR activation energy market (PICASSO platform) — DK1 and DK2 |
| **Description** | Near-real-time aFRR activated volume and price per bidding zone; sub-minute granularity (millisecond timestamps) |
| **Key Fields** | `TimeMsUTC`, `TimeMsDK`, `PriceArea` (DK1/DK2), `aFRR_Activated` (MWh), `aFRR_ActivatedEUR` (price/MWh) |
| **Revision Field** | None observed; data is published in near-real-time with millisecond precision |
| **Data Maturity** | Real-time provisional; Energinet should clarify if/when these figures are finalized |
| **Notes** | This is a high-frequency dataset (update frequency likely 5–15 seconds). Represents the actual activation energy market (PICASSO). No DKK prices observed; only EUR. **Update (2026-07-17):** the volume field `aFRR_Activated` is now also ingested (`shared/datasets.py`, product `activation_volume`, alongside the pre-existing `activation_price`) — confirmed via the dataset's own API metadata: "Activation in MW. Positive value is up regulation, negative value is down regulation." |

### 1.5 aFRR PICASSO Corrections — newly confirmed and ingested

| Field | Value |
|-------|-------|
| **Dataset ID** | `AfrrPicassoCorrections` |
| **Market/Zone Mapping** | aFRR real-time correction volume + PICASSO-calculated price — DK1, DK2 |
| **Description** | "Combined corrections received from PICASSO and the calculated price" (Energinet's own dataset description). ~1-second resolution. |
| **Key Fields** | `TimeMsUTC` (primary key alongside `PriceArea`), `PriceArea`, `Correction` (MW; positive = upwards adjustment, negative = downwards), `PriceUpEUR`, `PriceDownEUR` |
| **Revision Field** | None — investigated as a candidate real revision signal (see below) and ruled out |
| **Data Maturity** | Real-time provisional |
| **Notes** | Ingested as `afrr_picasso_corrections` (`shared/datasets.py`), market label `aFRR_correction`, products `correction_volume`/`up`/`down`. **`Correction` field semantics, confirmed via `meta/dataset/AfrrPicassoCorrections` and a 100-row live sample:** it is documented by Energinet as a signed correction *volume in MW* — analogous to `aFRR_Activated` above — not a corrected/superseding price value and not a boolean flag. `TimeMsUTC` is part of the dataset's primary key with no two sampled rows sharing a timestamp, so it is not "the same time unit revised later" either. **This means it is NOT the true `published_at`/revision signal every milestone since M1 has been looking for** — that gap (`shared/rule_engine.py:check_revisions` still uses `fetched_at` as a proxy, per `init-db/01-init.sql`) remains open. The dataset is still worth ingesting for its own (volume, price) content. |

---

## 2. FCR (Frequency Containment Reserves) Datasets

### 2.1 FCR Reserves — DK1

| Field | Value |
|-------|-------|
| **Dataset ID** | `FCRReservesDK1` |
| **Market/Zone Mapping** | FCR Cooperation (regelleistung.net joint auction) — DK1 |
| **Description** | Hourly FCR-up and FCR-down reserve prices and volumes for DK1; joint market with Germany |
| **Key Fields** | To be confirmed by API fetch; expected from sample data: `HourUTC`, `HourDK`, `FCR_DownExpected`, `FCR_DownPurchased`, `FCR_DownPriceDKK`, `FCR_DownPriceEUR`, `FCR_UpExpected`, `FCR_UpPurchased`, `FCR_UpPriceDKK`, `FCR_UpPriceEUR` |
| **Revision Field** | Not confirmed |
| **Data Maturity** | Likely final (hourly Cooperation auction results); unconfirmed |
| **Notes** | DK1 participates in the regelleistung.net auction with Germany, so clearing prices may show identical EUR values across DK1/DE. |

### 2.2 FCR Reserves — DK2 (Nordic Market)

| Field | Value |
|-------|-------|
| **Dataset ID** | `FCRReservesDK2` |
| **Market/Zone Mapping** | Nordic FCR-N and FCR-D markets — DK2 |
| **Description** | Hourly FCR-N (normal) and FCR-D (disturbance) reserve prices and volumes for DK2; Nordic market structure |
| **Key Fields** | To be confirmed by API fetch; sample showed: `HourUTC`, `HourDK`, `FCR_N_PriceDKK`, `FCR_N_PriceEUR`, `FCR_D_UpPriceDKK`, `FCR_D_UpPriceEUR`, `FCR_D_DownPriceEUR` (and likely down DKK) |
| **Revision Field** | Not confirmed |
| **Data Maturity** | Likely final; unconfirmed |
| **Notes** | Different structure from DK1 because Nordic market splits FCR into -N and -D products. |

### 2.3 FCR-D Market — DK2 (Additional Detail)

| Field | Value |
|-------|-------|
| **Dataset ID** | `FcrNdDK2` |
| **Market/Zone Mapping** | FCR-D (disturbance) market — DK2 |
| **Description** | Detailed FCR-D auction results with product name, auction type (D-1 early, etc.), local vs. total procured volumes, and total clearing price |
| **Key Fields** | `HourUTC`, `HourDK`, `PriceArea` (DK2), `ProductName` ("FCR-D ned" = down, presumably "op" = up exists), `AuctionType` ("D-1 early"), `PurchasedVolumeLocal`, `PurchasedVolumeTotal`, `PriceTotalEUR` |
| **Revision Field** | None observed |
| **Data Maturity** | Final auction results (published post-auction) |
| **Notes** | This is a granular view of FCR-D; may duplicate or complement `FCRReservesDK2`. Confirm deduplication strategy in M1 ingestion. |

---

## 3. Imbalance & Electricity Market Prices

### 3.1 Imbalance Settlement Prices

| Field | Value |
|-------|-------|
| **Dataset ID** | `ImbalancePrice` |
| **Market/Zone Mapping** | System imbalance settlement — DK1 and DK2 |
| **Description** | 15-minute imbalance prices, spot reference prices, and volume-weighted average (VWA) prices for aFRR and mFRR activated energy per zone |
| **Key Fields** | `TimeUTC`, `TimeDK`, `PriceArea` (DK1/DK2), `SatisfiedDemand` (MW; negative = surplus), `ImbalancePriceEUR`, `ImbalancePriceDKK`, `SpotPriceEUR`, `DominatingDirection` (-1/+1), `aFRRUpMW`, `aFRRVWAUpEUR`, `aFRRVWAUpDKK`, `aFRRDownMW`, `aFRRVWADownEUR`, `aFRRVWADownDKK`, (mFRR fields likely similar pattern) |
| **Revision Field** | None observed in 15-minute granularity; Energinet should confirm settlement time |
| **Data Maturity** | Provisional (15-minute figures; likely revised in subsequent hours/days) |
| **Notes** | **Primary focus for ±N hour context window in anomaly reports.** This dataset ties imbalance settlement to activation energy prices (aFRR/mFRR VWA). Field structure suggests mFRR columns may also exist but were truncated in sample. |

### 3.2 Day-Ahead Spot Prices

| Field | Value |
|-------|-------|
| **Dataset ID** | `DayAheadPrices` |
| **Market/Zone Mapping** | Day-ahead electricity market (NordPool) — all zones (DK1, DK2, DE, NO, SE, etc.) |
| **Description** | Hourly day-ahead spot prices published by NordPool; reference price for comparing activation energy costs to baseline |
| **Key Fields** | `TimeUTC`, `TimeDK`, `PriceArea` (zone code: DK1, DK2, DE, etc.), `DayAheadPriceEUR`, `DayAheadPriceDKK` |
| **Revision Field** | None; final published auction prices |
| **Data Maturity** | Final |
| **Notes** | Used in anomaly detection (e.g., mFRR activation price >> day-ahead price). Essential context for explaining activation energy spikes. |

### 3.3 Historical Spot Prices (Deprecated)

| Field | Value |
|-------|-------|
| **Dataset ID** | `Elspotprices` |
| **Market/Zone Mapping** | Historical day-ahead spot prices (deprecated) |
| **Description** | Same as DayAheadPrices; **dataset discontinued after Sept 30, 2025** |
| **Key Fields** | `HourUTC`, `HourDK`, `PriceArea`, `SpotPriceDKK`, `SpotPriceEUR` |
| **Revision Field** | N/A (discontinued) |
| **Data Maturity** | N/A (discontinued) |
| **Notes** | Do not use for new ingestion; historical data may still be available. `DayAheadPrices` is the current source. |

---

## 4. System Status & Generation/Load Data

### 4.1 Real-Time Power System Status

| Field | Value |
|-------|-------|
| **Dataset ID** | `PowerSystemRightNow` |
| **Market/Zone Mapping** | System state reference (all zones) |
| **Description** | Near-real-time (5-minute) aggregated generation, consumption, exchanges, CO2 intensity, and aFRR/wind/solar production; used as explanatory context for price spikes (e.g., "low wind" explanation) |
| **Key Fields** | `Minutes1UTC`, `Minutes1DK`, `CO2Emission`, `ProductionGe100MW`, `ProductionLt100MW`, `SolarPower`, `OffshoreWindPower`, `OnshoreWindPower`, `Exchange_Sum`, `Exchange_DK1_DE`, `Exchange_DK1_NL`, `Exchange_DK1_GB`, `Exchange_DK1_NO`, `Exchange_DK1_SE`, `Exchange_DK1_DK2`, `Exchange_DK2_DE`, `Exchange_DK2_SE`, `Exchange_Bornholm_SE`, `aFRR_ActivatedDK1`, `aFRR_ActivatedDK2` |
| **Revision Field** | None observed; real-time snapshot updates every 5 minutes, replacing previous values |
| **Data Maturity** | Real-time provisional |
| **Notes** | Used primarily for **"soft signal" reporting** — e.g., "imbalance jumped; wind output dropped 50 MW in the prior 15 min." Interconnector flows (Exchange_*) are key for explaining cross-border-driven price moves. |

### 4.2 Generation & Consumption Settlement

| Field | Value |
|-------|-------|
| **Dataset ID** | `ElectricityBalanceNonv` |
| **Market/Zone Mapping** | Generation and load by fuel type — DK1, DK2, other zones |
| **Description** | Hourly electricity production by technology (wind, solar, hydro, biomass, fossil, etc.) and total load per zone; non-validated (preliminary) figures |
| **Key Fields** | `HourUTC`, `HourDK`, `PriceArea`, `TotalLoad`, `Biomass`, `FossilGas`, `FossilHardCoal`, `FossilOil`, `HydroPower`, `OtherRenewable`, `SolarPower`, `Waste`, `OnshoreWindPower`, `OffshoreWindPower`, (possibly more fuel types) |
| **Revision Field** | Name suggests "Nonv" = non-validated; Energinet should clarify if this is provisional data or if a validated version exists |
| **Data Maturity** | Provisional (non-validated) |
| **Notes** | Use for post-hoc explanations of imbalances ("Forecast error on wind" etc.). Likely superseded by validated data in a separate dataset; recommend checking for `ElectricityBalance` (validated) variant. |

### 4.3 Production & Consumption Settlement (Detailed)

| Field | Value |
|-------|-------|
| **Dataset ID** | `ProductionConsumptionSettlement` |
| **Market/Zone Mapping** | Generation and consumption by detailed category and technology — DK1, DK2 |
| **Description** | Hourly metered/settled generation (MW) and consumption by technology class and size (central power, local power, onshore/offshore wind by capacity tier, hydro, solar, etc.) and self-consumption figures |
| **Key Fields** | `HourUTC`, `HourDK`, `PriceArea`, `CentralPowerMWh`, `LocalPowerMWh`, `CommercialPowerMWh`, `LocalPowerSelfConMWh`, `OffshoreWindLt100MW_MWh`, `OffshoreWindGe100MW_MWh`, `OnshoreWindLt50kW_MWh`, `OnshoreWindGe50kW_MWh`, `HydroPowerMWh`, `SolarPowerLt10kW_MWh`, (and likely more) |
| **Revision Field** | Not confirmed; settlement data typically finalized after T+2 to T+10 days |
| **Data Maturity** | Settlement-grade (finalized after reconciliation period) |
| **Notes** | High-granularity view suitable for detailed post-event analysis. Used to validate explanations ("was wind really 50 MW below forecast?"). |

---

## 5. Consumption & Sectoral Data

### 5.1 Industrial Consumption by DK36/DK19 Code

| Field | Value |
|-------|-------|
| **Dataset ID** | `ConsumptionDK3619IndustryHour` |
| **Market/Zone Mapping** | Industrial consumption — aggregated across DK1+DK2 (or per-zone variant may exist) |
| **Description** | Hourly industrial electricity consumption by Danish industrial classification (DK36 and DK19 codes; e.g., agriculture, food processing, chemicals) |
| **Key Fields** | `TimeUTC`, `TimeDK`, `DK36Code`, `DK36Title`, `DK19Code`, `DK19Title`, `Consumption_MWh` |
| **Revision Field** | None observed |
| **Data Maturity** | Final metered data (post-settlement) |
| **Notes** | Useful for explaining consumption-driven imbalances in specific industrial sectors. |

### 5.2 Consumer Category Consumption by Region

| Field | Value |
|-------|-------|
| **Dataset ID** | `ConsumptionConsumerCategoryHour` |
| **Market/Zone Mapping** | Consumer consumption by region and type — DK1+DK2 |
| **Description** | Hourly consumption by customer type (residential, commercial "Erhverv") and region (e.g., Region Hovedstaden) |
| **Key Fields** | `TimeUTC`, `TimeDK`, `RegionName`, `ConsumerCategory3` (broad category), `ConsumerCategory2` (sub-category), `ConsumptionkWh` |
| **Revision Field** | None observed |
| **Data Maturity** | Final metered data |
| **Notes** | Lower resolution than industrial data; used for regional demand context. |

---

## 6. Transmission & Interconnection

### 6.1 Transmission Lines & Interconnector Flows

| Field | Value |
|-------|-------|
| **Dataset ID** | `Transmissionlines` |
| **Market/Zone Mapping** | Interconnector flows between DK and neighboring zones (DE, NO, SE, NL, GB); DK1/DK2 internal exchange |
| **Description** | Hourly transmission line flows (import/export capacity, scheduled exchange, physical exchange, congestion management, and prices at the exchange points) |
| **Key Fields** | `HourUTC`, `HourDK`, `PriceArea` (DK1), `ConnectedArea` (DE/NO/SE/NL/GB), `ImportCapacity`, `ExportCapacity` (negative), `ScheduledExchangeDayAhead`, `ScheduledExchangeIntraday`, `PhysicalExchangeNonvalidated`, `PhysicalExchangeSettlement`, `CongestionIncomeDKK`, `HomePriceDKK`, `ConnectedPriceDKK`, `CongestionIncomeEUR`, `HomePriceEUR`, `ConnectedPriceEUR` |
| **Revision Field** | Not confirmed; interconnector flows may be revised as settlement finalizes |
| **Data Maturity** | Likely provisional/final (unconfirmed); physical exchange may be revised |
| **Notes** | **Key dataset for cross-border flow explanations** (e.g., "DK1→DE export capacity fully used; prevents wind export; drives down balancing prices" or vice versa). Congestion prices indicate whether flows are constrained. |

---

## 7. Gas System Data

### 7.1 Danish Gas Flows

| Field | Value |
|-------|-------|
| **Dataset ID** | `Gasflow` |
| **Market/Zone Mapping** | Danish gas system (Energinet Gas) |
| **Description** | Daily Danish natural gas balance flows: biogas production, imports (North Sea, Germany, Sweden), storage, and consumption |
| **Key Fields** | `GasDay`, `KWhFromBiogas`, `KWhToDenmark`, `KWhFromNorthSea`, `KWhToOrFromStorage`, `KWhToOrFromGermany`, `KWhToSweden`, `kWhFromTyra`, `KWhToPoland` |
| **Revision Field** | None observed |
| **Data Maturity** | Final daily aggregate |
| **Notes** | **Lower priority for M0 (not in README markets scope)** but included for completeness. May be useful for explaining gas-fired generation availability. |

---

## 8. Missing / Not Yet Catalogued Datasets

### 8.1 Potential mFRR EAM Energy Activation Dataset

**Status:** ~~Not yet confirmed in API discovery.~~ **RESOLVED 2026-07-17 — see
§1.0 above.** `MfrrEnergyActivationMarket` exists and is now ingested; the
"not found" result below was a false negative from querying the JS-rendered
marketing site rather than the API directly (same root cause as the
EnergyWatch RSS discovery issue elsewhere in this catalogue). The rest of
this subsection is left as-is as the historical record of the original
(incorrect) M0 finding.

**Original status (incorrect, kept for record):** Not yet confirmed in API discovery.

**Context:** The README mentions "mFRR Energy Activation Market (EAM)" as the primary focus, replacing older regulating-power datasets post-March 2025. The dataset `mFRRCapacityMarket` covers *capacity* clearing; however, **there should be a separate dataset for mFRR EAM energy activation volumes and prices** (analogous to `aFRREnergyActivation`).

**Action:** Search API for:
- `mFRREnergyActivation` or `MfrrEnergyActivation`
- `mFRRActivatedEnergy` or similar
- `mFRREAM` or `MFRREAM`
- Query Energinet support for the official dataset name

**Expected fields:** `TimeUTC`, `TimeDK`, `PriceArea` (DK1/DK2), `mFRR_Activated` (MWh), `mFRR_ActivationPrice` (DKK/MWh or EUR/MWh), possibly direction (up/down)

### 8.2 Intraday Prices

**Status:** Not found in API discovery.

**Context:** README mentions "day-ahead / intraday prices" as context markets. Only `DayAheadPrices` was found.

**Action:** Search for:
- `IntradayPrices`
- `IntraDayMarket`
- `IntradayAuctions`

### 8.3 Transmission Outages / UMM Data

**Status:** Not found in API discovery.

**Context:** README mentions "transmission outages/congestion data" and "UMMs/outages" from ENTSO-E. Energinet may publish its own outage/maintenance notifications.

**Action:** Search for:
- `TransmissionOutages`
- `GridOutages`
- `Outages`
- `MaintenanceNotifications`
- Or check ENTSO-E UMM endpoints (see §9 below)

### 8.4 aFRR Reserves DK2

**Status:** Not found ("dataset not found" for `aFrrReservesDK2`).

**Context:** DK1 has `aFrrReservesDK1`, but DK2 lookup failed. DK2 may be served through a different dataset (possibly Nordic-wide aggregation) or under a different name.

**Action:** Clarify with Energinet whether DK2 aFRR is:
- In the Nordic market under a shared dataset name
- Published as a separate `aFrrReservesDK2` (currently appears missing)
- Included in `aFRREnergyActivation` only

---

## 9. External APIs: ENTSO-E Transparency Platform

**Purpose:** Cross-border flows, outages (UMMs), balancing data for neighboring zones (explanatory context).

### 9.1 ENTSO-E REST API

| Attribute | Value |
|-----------|-------|
| **Base URL** | `https://web-api.tp.entsoe.eu/api` |
| **Authentication** | Security token (register on Transparency Platform; email transparency@entsoe.eu) |
| **Response Format** | XML |
| **Relevant Endpoints** | Cross-border Physical Flows, Unit Commitment & Maintenance Notifications (UMMs), Generation Forecast, Load Forecast, Balancing data |
| **Denmark Codes** | EIC codes for DK1, DK2, Bornholm (find via https://transparency.entsoe.eu/ portal) |
| **Documentation** | Official user guide: https://transparency.entsoe.eu/content/static_content/Static%20content/web%20api/Guide.html; Postman collection available |

### 9.2 Relevant Endpoints for AncillaryNews

- **Cross-border Physical Flows:** `/PhysicalFlow/show` (flows between DK and DE, NO, SE, etc.)
- **Unit Commitment & Maintenance Notifications (UMMs):** `/UnavailabilityOfGenerationUnits/show` (transmission line outages, generator maintenance)
- **Load Forecast & Actual:** Context for imbalance explanations
- **Balancing Data:** Actual balancing energy prices/volumes for neighboring zones

**Note:** ENTSO-E data is XML; M1 ingestion will need XML parsing. Consider the `entsoeapi` R package or `entsoe-py` Python wrapper for reference implementations.

---

## 10. News & Market Analysis RSS Feeds

### 10.1 EnergyWatch (Danish energy sector news)

| Attribute | Value |
|-----------|-------|
| **Organization** | Watch Medier (subsidiary of JP/Politikens Hus, major Danish media) |
| **Website** | https://energywatch.com/ |
| **RSS Feed** | https://energywatch.com/service/RSS |
| **Coverage** | Danish and Northern European energy sector, utilities, renewables, climate policy |
| **Update Frequency** | (Unknown; check feed) |

### 10.2 Montel News (European energy markets)

| Attribute | Value |
|-----------|-------|
| **Organization** | Montel News |
| **Website** | https://montelnews.com/ |
| **News Portal** | https://montel.energy/news/real-time-energy-market-news |
| **Coverage** | European electricity & gas markets, price news, balancing/ancillary service updates |
| **Update Frequency** | Real-time news wire |
| **Note** | Subscription model; free access limited. Check for API or RSS options. |

### 10.3 U.S. Energy Information Administration (EIA) RSS Feeds

| Attribute | Value |
|-----------|-------|
| **Website** | https://www.eia.gov/ |
| **RSS Feeds** | https://www.eia.gov/tools/rssfeeds/ |
| **Relevance** | Lower priority for Nordic focus, but useful for global energy commodity and policy context (oil, gas, coal price drivers that may affect Danish balancing) |

### 10.4 Nordic Balancing Model (NBM) News & Announcements

| Attribute | Value |
|-----------|-------|
| **Website** | https://nordicbalancingmodel.net/ |
| **Coverage** | Official announcements on mFRR EAM, aFRR, FCR market operations; platform incidents; implementation guides |
| **Feed** | (Check for RSS/news feed; may publish via announcements page or email subscription) |

### 10.5 Energinet Press Releases & Market Messages

| Attribute | Value |
|-----------|-------|
| **Website** | https://en.energinet.dk/ |
| **Coverage** | Official TSO announcements, market-rule changes, system events, balancing market updates |
| **Feed** | (Check for RSS feed at https://en.energinet.dk/energy-data/news-from-energi-data-service/ or main news page) |

---

## 11. Revision & Data Quality Notes

### 11.1 Provisional vs. Final Data

Based on API exploration, Energinet's datasets publish with varying maturity:

- **Real-time (5–15 min):** `PowerSystemRightNow`, `aFRREnergyActivation` — updated continuously; figures are provisional and subject to revision once validated data flows in.
- **Hourly provisional:** `ImbalancePrice`, `mFRRCapacityMarket`, `DayAheadPrices` — published at the hour and may be revised in subsequent hours.
- **Hourly final:** `ProductionConsumptionSettlement`, `ElectricityBalanceNonv` — finalized after T+1 to T+10 days (Energinet should clarify exact schedule).
- **Daily:** `Gasflow` — daily aggregates.

### 11.2 Revision Fields

**Finding:** None of the datasets include explicit `PublishedTime` or `RevisedTime` fields in their API responses. This is a **critical gap for bitemporal tracking** (see README §5).

**Recommendation for M1:**
1. **Check with Energinet** whether revision metadata can be accessed via a different endpoint or download format.
2. **Implement polling with revision detection:** Store every fetch with its own `fetched_at` timestamp and monitor for changed values (detecting revisions post-hoc).
3. **Query Energinet's official revision schedule** for each dataset (e.g., "aFRR capacity prices are finalized T+0 18:00", "imbalance prices revised up to T+2").

### 11.3 Bitemporal Schema Recommendation

Even without explicit revision fields, design TimescaleDB hypertables with:
```sql
CREATE TABLE market_data (
  time TIMESTAMPTZ NOT NULL,        -- Market time unit (HourUTC, TimeUTC, etc.)
  market TEXT NOT NULL,             -- e.g., 'mFRR_EAM', 'FCR_DK1', 'aFRR_capacity'
  zone TEXT NOT NULL,               -- e.g., 'DK1', 'DK2'
  product TEXT,                     -- e.g., 'up', 'down', 'reserve', 'energy'
  value FLOAT,                      -- e.g., price (DKK or EUR) or volume (MW/MWh)
  published_at TIMESTAMPTZ NOT NULL,-- When Energinet published this figure
  fetched_at TIMESTAMPTZ NOT NULL,  -- When we polled it
  is_provisional BOOLEAN,           -- True if Energinet has not yet finalized
  source TEXT,                      -- 'Energinet', 'ENTSO-E', 'NBM'
  PRIMARY KEY (time, market, zone, product, published_at)
);
```

This allows:
- **Revision tracking:** Rows with same `(time, market, zone, product)` but different `published_at` are revisions.
- **Provisional labeling:** `is_provisional=true` gates report generation (see README §5).
- **Source traceability:** `source` field for multi-source data.

---

## 12. Summary Table: Actionable Dataset List for M1 Ingestion

| Dataset ID | Market | Zone(s) | Granularity | Key Field | Status | Priority |
|------------|--------|---------|-------------|-----------|--------|----------|
| `MfrrEnergyActivationMarket` | mFRR energy (EAM) | DK1, DK2 | 15 min | `mFRRSAUpEUR`, `mFRRSADownEUR` | ✓ Confirmed & ingested (2026-07-17) | **CRITICAL** |
| `AfrrPicassoCorrections` | aFRR correction volume + price | DK1, DK2 | ~1 sec | `Correction`, `PriceUpEUR` | ✓ Confirmed & ingested (2026-07-17) | Medium |
| `mFRRCapacityMarket` | mFRR capacity | DK1, DK2 | Hourly | `UpPriceDKK`, `DownPriceDKK` | ✓ Confirmed | **High** |
| `mFrrReservesDK1`, `mFrrReservesDK2` | mFRR capacity (legacy) | DK1, DK2 | Hourly | `mFRR_UpPriceDKK`, `mFRR_DownPriceDKK` | ✓ Confirmed (rate-limited) | Medium |
| `aFrrReservesDK1` | aFRR capacity | DK1 | Hourly | `aFRR_UpPriceDKK`, `aFRR_DownPriceDKK` | ✓ Confirmed (rate-limited) | Medium |
| `aFrrReservesDK2` | aFRR capacity | DK2 | Hourly | (same as DK1) | ✗ Not found | **Action needed** |
| `aFRREnergyActivation` | aFRR energy (PICASSO) | DK1, DK2 | ~5–15 sec | `aFRR_ActivatedEUR` | ✓ Confirmed | High |
| `FCRReservesDK1` | FCR (Cooperation) | DK1 | Hourly | `FCR_UpPriceDKK`, `FCR_DownPriceDKK` | ✓ Confirmed (rate-limited) | Medium |
| `FCRReservesDK2` | FCR (Nordic) | DK2 | Hourly | `FCR_N_PriceDKK`, `FCR_D_UpPriceDKK` | ✓ Confirmed (rate-limited) | Medium |
| `FcrNdDK2` | FCR-D (detail) | DK2 | Hourly | `PriceTotalEUR` | ✓ Confirmed | Low (detail view) |
| **mFRR EAM Energy** *(original, incorrect finding — see §1.0 / §8.1)* | **mFRR energy (EAM)** | **DK1, DK2** | **~15 min** | **`mFRR_ActivationPrice`** | **✗ Not found → ✓ RESOLVED, see row above** | **CRITICAL** |
| `ImbalancePrice` | Imbalance settlement + aFRR/mFRR VWA | DK1, DK2 | 15 min | `ImbalancePriceDKK`, `aFRRVWAUpDKK` | ✓ Confirmed | **High** |
| `DayAheadPrices` | Day-ahead spot (NordPool) | All zones | Hourly | `DayAheadPriceDKK` | ✓ Confirmed | High |
| `PowerSystemRightNow` | System state (generation, wind, exchange) | DK1, DK2 | 5 min | `OnshoreWindPower`, `Exchange_DK1_DE` | ✓ Confirmed | High |
| `ElectricityBalanceNonv` | Generation & load by fuel | DK1, DK2 | Hourly | `OnshoreWindPower`, `TotalLoad` | ✓ Confirmed | Medium |
| `ProductionConsumptionSettlement` | Settlement generation/consumption (detailed) | DK1, DK2 | Hourly | `OffshoreWindGe100MW_MWh` | ✓ Confirmed | Medium |
| `Transmissionlines` | Interconnector flows & congestion | DK1/DK2 to neighbors | Hourly | `PhysicalExchangeSettlement`, `CongestionIncomeDKK` | ✓ Confirmed | High |
| `ConsumptionDK3619IndustryHour` | Industrial consumption by sector | DK1+DK2 | Hourly | `Consumption_MWh` | ✓ Confirmed | Low |
| `ConsumptionConsumerCategoryHour` | Consumer consumption by region | DK1+DK2 | Hourly | `ConsumptionkWh` | ✓ Confirmed | Low |
| `Gasflow` | Gas flows (Energinet Gas) | Denmark | Daily | `KWhFromNorthSea` | ✓ Confirmed | Low |
| `Elspotprices` | Day-ahead spot (legacy) | All zones | Hourly | (same as DayAheadPrices) | ⚠ Discontinued 2025-09-30 | Legacy |
| **Transmission Outages** | **UMM / maintenance** | **DK1, DK2** | **Event** | **Outage details** | **✗ Not found** | **Action needed** |
| **Intraday Prices** | **Intraday market** | **DK1, DK2** | **Continuous** | **Intraday price** | **✗ Not found** | **Action needed** |

---

## 13. Actions for M0 Completion

- [ ] **Confirm mFRR EAM energy dataset:** Contact Energinet support to verify the official dataset ID for mFRR Energy Activation Market (EAM) prices and activated volumes. Expected fields: time, zone, price (DKK/EUR), activated volume (MWh), direction (up/down).
  
- [ ] **Clarify aFRR DK2:** Determine whether DK2 aFRR is published separately or as part of a Nordic-wide dataset.

- [ ] **Find transmission outages:** Locate Energinet's dataset for planned/unplanned transmission line outages and maintenance notifications (UMMs). If not in Energi Data Service, check ENTSO-E UMM endpoints.

- [ ] **Find intraday prices:** Confirm whether intraday market data is published by Energinet/NordPool via Energi Data Service or ENTSO-E.

- [ ] **Verify revision schedules:** For each high-priority dataset, obtain Energinet's official T+X finalization timeline and any available revision metadata endpoints.

- [ ] **Test ENTSO-E API:** Obtain security token and validate that DK1/DK2 EIC codes work with ENTSO-E endpoints for cross-border flows and UMM data.

- [ ] **Validate RSS feeds:** Test EnergyWatch, Montel, Energinet, and NBM feeds for actual article frequency and coverage of balancing-market events.

---

## 14. References & Sources

### Energinet Official
- [Energi Data Service Portal](https://www.energidataservice.dk/)
- [Energi Data Service API Guide](https://www.energidataservice.dk/guides/api-guides)
- [Data Catalog (PDF, EN/DA)](https://www.energidataservice.dk/Data_catalog_EN.pdf)
- [Energinet News & Announcements](https://en.energinet.dk/energy-data/news-from-energi-data-service/)

### Nordic Balancing Model
- [NBM Implementation Guides](https://nordicbalancingmodel.net/implementation-guides/)
- [mFRR EAM Market Overview](https://nordicbalancingmodel.net/tag/mfrr-eam/)
- [BSP Implementation Guide – mFRR EAM v1.2.0](https://nordicbalancingmodel.net/wp-content/uploads/2025/09/Implementation-Guide-mFRR-energy-activation-market-BSP-v1.2.0.pdf)

### ENTSO-E
- [Transparency Platform](https://transparency.entsoe.eu/)
- [REST API Documentation](https://transparency.entsoe.eu/content/static_content/Static%20content/web%20api/Guide.html)
- [Postman API Collection](https://documenter.getpostman.com/view/7009892/2s93JtP3F6)

### News Sources
- [EnergyWatch RSS](https://energywatch.com/service/RSS)
- [Montel News](https://montelnews.com/)
- [EIA RSS Feeds](https://www.eia.gov/tools/rssfeeds/)

---

**Report compiled:** July 16, 2026  
**Methodology:** Direct API testing via `https://api.energidataservice.dk/dataset/{ID}?limit=1-2`, web search, ENTSO-E/NBM documentation review, RSS feed discovery.  
**Rate limiting encountered:** Yes; rate limit ~1 request per second observed during bulk discovery.  
**Next milestone:** M1 — Ingestion engine & TimescaleDB schema design.

