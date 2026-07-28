"""
Post-processing net-profit layer over `shared/bess_simulator.py:run_backtest`
(`docs/bess-cooptimizer-design.md`-adjacent; see the reality-check plan this
module was built from). `run_backtest`/`shared/bess_dispatch_milp.py` report
**revenue only** -- the co-optimizer's own objective already nets the
day-ahead spot cost of charging into its arbitrage figure, but nothing else
in the stack a real retailer-billed operator pays (grid tariff, energy tax,
VAT, and an optimizer service fee) has any term anywhere upstream.

This module is a pure, read-only function of a `BacktestResult` -- no DB, no
network, no mutation of `shared/bess_simulator.py`/`shared/bess_dispatch_milp.py`,
which stay exactly as they are (the revenue engine is not this module's
concern; see those modules' docstrings for how that side works).

**Cost stack** (reconciled against a real 2 MW/4 MWh DK2 asset's retailer
agreement + "Aconto" cost model, Apr-Jun 2026 -- see the reality-check
harness `scripts/bess_reality_check.py`):

- **Grid tariff** (TSO+DSO, production+consumption, low-tariff hours): 21
  €/MWh ex-tax ex-VAT, billed per MWh *discharged*.
- **Energy tax** (elafgift): ~96 €/MWh, but reimbursed for storage use in
  practice -- net 0 unless `energy_tax_reimbursed=False`.
- **VAT**: 25%, reclaimable for a business, so excluded from *economic*
  profit by default (`include_vat=False`); flip it on to see the cash/
  accounting-basis number instead.
- **Service fee** (optimizer, retailer agreement §3.2.1): 3.75 €/MWh,
  billed on the same discharged-MWh volume basis.
- **Electricity purchase**: day-ahead spot (already inside the backtest's
  own arbitrage revenue, split out below) plus a fixed additive cut per
  MWh *charged* (`spot_cut_eur_per_mwh`) -- the one unknown in the real
  agreement; defaults to 0 until fitted via `implied_spot_cut` below.

All of `RetailerCostConfig`'s rates are business-agreement figures, not
market data -- there is deliberately no source for them beyond this
module's defaults/callers overriding them explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.bess_simulator import BacktestResult
from shared.units import DKK_PER_EUR


@dataclass(frozen=True)
class RetailerCostConfig:
    """
    Retailer cost-stack parameters, in EUR/MWh unless noted. Defaults are
    the reconciled real-agreement figures (module docstring above) -- every
    field can be overridden per call, e.g. to fit `spot_cut_eur_per_mwh`
    against a real month's total cost via `implied_spot_cut`.
    """

    grid_tariff_eur_per_mwh: float = 21.0
    energy_tax_eur_per_mwh: float = 96.0
    # Storage assets typically get the energy tax reimbursed -- net 0 cost
    # from this line. Set False to model a regime/agreement without that
    # reimbursement.
    energy_tax_reimbursed: bool = True
    vat_rate: float = 0.25
    # VAT is reclaimable for a business, so it is not a real economic cost
    # by default -- set True to see the gross/cash-basis figure instead
    # (e.g. to match an invoice/Aconto total that is VAT-inclusive).
    include_vat: bool = False
    service_fee_eur_per_mwh: float = 3.75
    # Additive markup on the day-ahead spot price paid while charging --
    # the one unknown in the real retailer agreement (form/size not
    # published). Defaults to 0; `implied_spot_cut` below back-solves it
    # from a real month's total cost.
    spot_cut_eur_per_mwh: float = 0.0
    # Every cost line above is billed per MWh *discharged* in the real
    # agreement (reconciled via the Aconto check in this module's tests) --
    # kept as an explicit field, not a hardcoded assumption, so a future
    # agreement billed on a different basis (e.g. charged MWh, or
    # nameplate capacity) doesn't require touching `compute_pnl`'s
    # arithmetic silently. Only `"discharged"` is implemented today.
    volume_basis: str = "discharged"


@dataclass(frozen=True)
class BessPnl:
    """Every P&L line item from `compute_pnl`, in EUR. `net_profit` is the
    bottom line; every other field is kept so a caller (e.g. the
    reality-check table) can show the full breakdown, not just the total."""

    volume_mwh: float
    charged_mwh: float
    gross_discharge_income: float
    capacity_income: float
    gross_revenue: float
    charging_spot_cost: float
    electricity_cost: float
    tariff_cost: float
    service_fee_cost: float
    net_profit: float


def _charged_mwh(result: BacktestResult) -> float:
    """Sum of positive SoC deltas between consecutive ticks -- the energy
    that flowed *into* the battery over the run. `BessTick` has `soc_mwh`
    but no charged-MWh field of its own (unlike `energy_discharged_mwh`,
    which the simulator already tracks per tick), so this is recovered from
    the SoC trajectory rather than the `action` label: a discharge tick can
    still show a small positive SoC delta from round-trip-efficiency losses
    being applied elsewhere in the tick's charge leg, but never enough for
    this to double-count meaningfully, and it needs no assumption about
    `action` semantics at all."""
    charged = 0.0
    for prev, cur in zip(result.ticks, result.ticks[1:], strict=False):
        delta = cur.soc_mwh - prev.soc_mwh
        if delta > 0:
            charged += delta
    return charged


def _tick_arbitrage_split_eur(result: BacktestResult) -> tuple[float, float]:
    """Splits `arbitrage_revenue_dkk` per tick on its sign: positive ticks
    (discharge, sold at a favourable price) sum into gross discharge
    income; negative ticks (charge, bought at spot) sum into a *positive*
    charging spot cost. Both totals are converted to EUR at the fixed
    `shared.units.DKK_PER_EUR` peg -- the same presentation-layer
    conversion `BacktestResult.total_revenue_all_eur` already uses,
    reused here rather than reimplemented."""
    income_dkk = 0.0
    cost_dkk = 0.0
    for tick in result.ticks:
        if tick.arbitrage_revenue_dkk > 0:
            income_dkk += tick.arbitrage_revenue_dkk
        elif tick.arbitrage_revenue_dkk < 0:
            cost_dkk += -tick.arbitrage_revenue_dkk
    return income_dkk / DKK_PER_EUR, cost_dkk / DKK_PER_EUR


def _effective_tariff_eur_per_mwh(cost: RetailerCostConfig) -> float:
    """`grid_tariff_eur_per_mwh` plus the energy tax when it is *not*
    reimbursed, then VAT-loaded if `include_vat` -- the same arithmetic the
    module docstring's rate table and `tests/test_bess_economics.py`'s
    Aconto reconciliation both walk through explicitly."""
    tariff = cost.grid_tariff_eur_per_mwh
    if not cost.energy_tax_reimbursed:
        tariff += cost.energy_tax_eur_per_mwh
    if cost.include_vat:
        tariff *= 1.0 + cost.vat_rate
    return tariff


def compute_pnl(result: BacktestResult, cost: RetailerCostConfig | None = None) -> BessPnl:
    """
    Turns a `BacktestResult` into a full P&L (gross revenue -> net profit)
    using `cost`'s retailer cost stack (defaults to `RetailerCostConfig()`'s
    reconciled real-agreement rates if omitted). Pure function of
    `result`/`cost` -- no DB, no mutation of either argument.
    """
    cost = cost or RetailerCostConfig()
    volume_mwh = result.total_discharged_mwh
    charged_mwh = _charged_mwh(result)

    gross_discharge_income, charging_spot_cost = _tick_arbitrage_split_eur(result)
    capacity_income = (
        result.total_capacity_revenue_dkk / DKK_PER_EUR
        + result.total_capacity_revenue_eur
        + result.total_afrr_activation_revenue_eur
    )
    gross_revenue = gross_discharge_income + capacity_income

    tariff_cost = volume_mwh * _effective_tariff_eur_per_mwh(cost)
    service_fee_cost = volume_mwh * cost.service_fee_eur_per_mwh
    electricity_cost = charging_spot_cost + cost.spot_cut_eur_per_mwh * charged_mwh

    net_profit = gross_revenue - electricity_cost - tariff_cost - service_fee_cost

    return BessPnl(
        volume_mwh=volume_mwh,
        charged_mwh=charged_mwh,
        gross_discharge_income=gross_discharge_income,
        capacity_income=capacity_income,
        gross_revenue=gross_revenue,
        charging_spot_cost=charging_spot_cost,
        electricity_cost=electricity_cost,
        tariff_cost=tariff_cost,
        service_fee_cost=service_fee_cost,
        net_profit=net_profit,
    )


def implied_spot_cut(
    result: BacktestResult, cost: RetailerCostConfig, real_cost_eur: float
) -> float:
    """
    Back-solves the one unknown in the real retailer agreement --
    `spot_cut_eur_per_mwh` -- from a real month's total cost, holding every
    other line item (tariff, service fee, the spot cost the model itself
    already computes) fixed at `cost`'s other rates:

        spot_cut = (real_cost - charging_spot_cost - tariff_cost - service_fee_cost) / charged_mwh

    Returns 0.0 if `charged_mwh` is 0 (nothing was charged, so there is no
    per-MWh cut to solve for -- `float("nan")` would force every caller to
    special-case it for no benefit, since a zero-charge run has nothing
    to fit).
    """
    charged_mwh = _charged_mwh(result)
    if charged_mwh == 0:
        return 0.0

    _, charging_spot_cost = _tick_arbitrage_split_eur(result)
    tariff_cost = result.total_discharged_mwh * _effective_tariff_eur_per_mwh(cost)
    service_fee_cost = result.total_discharged_mwh * cost.service_fee_eur_per_mwh

    return (real_cost_eur - charging_spot_cost - tariff_cost - service_fee_cost) / charged_mwh
