# M0 Addendum: closing the mFRR EAM gap

**Date:** 2026-07-17
**Method:** Direct queries against `api.energidataservice.dk/dataset/<id>` (the human-facing
`energidataservice.dk` marketing pages are client-rendered JS and return no usable content to
static fetchers — same issue the original M0 audit hit with EnergyWatch's RSS page). All datasets
below were confirmed live with real, current (non-null) sample records.

## Primary finding: `MfrrEnergyActivationMarket` exists

The original M0 catalogue (`docs/dataset-catalogue.md`) could not confirm a dataset for the
README's primary-focus market — Nordic mFRR Energy Activation Market (EAM) prices. It exists.

- **Dataset ID:** `MfrrEnergyActivationMarket`
- **Granularity:** 15-minute MTU (`TimeUTC` increments of 15 min) — matches the README's own
  description of the EAM's settlement resolution.
- **Zones:** `PriceArea` = DK1/DK2.
- **Price fields:** `mFRRSAUpEUR`, `mFRRSADownEUR` (SA = shared/standard activation — the actual
  Nordic EAM clearing price). Also `mFRRDAUpEUR`/`mFRRDADownEUR` (present but null in samples so
  far — a second, currently-unused activation price path, meaning worth verifying over more
  history before assuming it's dead).
- **Volume fields:** `mFRRSAUpReqMW`/`mFRRSADownReqMW` (requested), `TotalmFRRUpMW`/
  `TotalmFRRDownMW`, `mFRROfferedUpMW`/`mFRROfferedDownMW`, plus `mFRRLocalUpMW`/`DownMW` and
  `mFRRSpecialUpMW`/`DownMW` (both null in samples so far).
- Real sample: `{'TimeUTC': '2026-07-17T06:15:00', 'PriceArea': 'DK1', 'mFRRSAUpReqMW': 38,
  'mFRRSAUpEUR': 165.24, 'mFRRSADownReqMW': None, 'mFRRSADownEUR': 132.5, 'TotalmFRRUpMW': 38,
  'TotalmFRRDownMW': 0, 'mFRROfferedUpMW': 525, 'mFRROfferedDownMW': 1011, ...}`

This closes both the primary-focus dataset gap **and** the "no volume data ingested" gap flagged
by every milestone since M2 — a single dataset carries both price and volume for the exact market
the whole system was built to monitor.

## Secondary finding: `AfrrPicassoCorrections` — a real revision signal

Every milestone since M1 has flagged that no Energinet dataset exposes a true publish/revision
timestamp, forcing the system to use `fetched_at` (when *we* polled) as a practical but imperfect
proxy. This dataset looks like the real thing:

- **Fields:** `TimeMsUTC`, `PriceArea`, `Correction`, `PriceUpEUR`, `PriceDownEUR`.
- Sample: `{'TimeMsUTC': '2026-07-16T13:06:03.983', 'PriceArea': 'DK1', 'Correction': 126.88699,
  'PriceUpEUR': 150.0, 'PriceDownEUR': None}`
- The `Correction` field's exact semantics (a corrected price value vs. a delta vs. a correction
  flag/magnitude) need one more look at a longer history sample before wiring into the rule
  engine's revision-alert trigger — but this is the first dataset found across three audits that
  appears purpose-built for revision tracking, rather than repurposing `fetched_at`.

## Other datasets confirmed live (not yet ingested)

| Dataset | Fields | Notes |
|---|---|---|
| `AfrrEnergyActivation` | `TimeMsUTC`, `PriceArea`, `aFRR_Activated`, `aFRR_ActivatedEUR` | Already ingested for price (M1); **`aFRR_Activated` (the volume) is not currently mapped** — a one-line config addition. |
| `AfrrReservesNordic` | `TimeUTC`, `PriceArea`, `UpDemandMW`, `UpProcuredMW`, `UpPriceEUR/DKK`, `DownDemandMW`, `DownProcuredMW`, `DownPriceEUR/DKK` | aFRR capacity/reservation payments — not yet ingested (mFRR capacity is; aFRR capacity isn't). |
| `FcrNdDK2` | `HourUTC`, `PriceArea`, `ProductName` (e.g. "FCR-D ned"), `AuctionType`, `PurchasedVolumeLocal/Total`, `PriceTotalEUR` | FCR-D Nordic auction, DK2. |
| `FcrDK1` | `HourUTC`, `FCRdomestic_MW`, `FCRabroad_MW`, `FCRcross_EUR/DKK`, `FCRdk_EUR/DKK` | FCR Cooperation, DK1 — domestic vs. cross-border split. |
| `FFRdemandDK2` | `HourUTC`, `ffrupdemandd0`..`d7` | Fast Frequency Reserve demand curve steps — a market not listed in the README's own §1 table at all. |
| `FFRDK2` | `HourUTC`, `FFR_DemandMW`, `FFR_PurchasedMW`, `FFR_PriceEUR/DKK` | FFR capacity payments, DK2. |
| `ImbalancePrice` | (already ingested) — additionally exposes `SpotPriceEUR`, `mFRRMarginalPriceUpEUR/DKK`, `mFRRMarginalPriceDownEUR/DKK` | The `mFRRMarginalPrice*` fields are a second possible EAM-price signal, currently unmapped — worth comparing against `MfrrEnergyActivationMarket`'s `mFRRSA*EUR` over real history to see if they're the same clearing price surfaced two ways or genuinely different. |
| `InertiaNordicSyncharea` | `HourUTC`, `InertiaNordicGWs`, `InertiaDK2GWs`, `InertiaNOGWs`, `InertiaSEGWs`, `InertiaFIGWs` | Grid inertia — system-state context data, not a payment market; useful contextual signal for synthesis (README §3C step 2) rather than a trigger source. |
