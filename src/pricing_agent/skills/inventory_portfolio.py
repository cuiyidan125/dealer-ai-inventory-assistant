"""Skill 2: inventory portfolio forecast. §14, skills/inventory-portfolio-forecast/SKILL.md.

One simulation covers every vehicle at its **current** list price, so the forecast answers
"what happens if we do nothing" — the baseline a manager needs before deciding to act.

Per-vehicle break-even and headroom are recomputed here from the same shared domain
modules rather than by re-running the single-vehicle skill twelve times. Both paths call
`domain/break_even.py` and `domain/promotion.py`, so they cannot disagree (§28).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

import numpy as np

from pricing_agent.config import Config, expected_discount_rate, load_config
from pricing_agent.domain import portfolio as portfolio_mod
from pricing_agent.domain import valuation as valuation_mod
from pricing_agent.domain.break_even import current_accounting_break_even
from pricing_agent.domain.depreciation import effective_monthly_rate
from pricing_agent.domain.holding_cost import daily_holding_cost
from pricing_agent.domain.summarize import (
    PERCENTILE_CONVENTION,
    collect_explanation_inputs,
    explanation_value,
    probability,
)
from pricing_agent.domain.vehicle import CostBasis, MissingCostBasis, Vehicle
from pricing_agent.mcp_clients import (
    CapacityClient,
    CostClient,
    MockTransport,
    PolicyClient,
    MarketDataClient,
)
from pricing_agent.policy import freshness
from pricing_agent.policy import warnings as warnings_mod
from pricing_agent.simulation import VehicleSimInput, simulate


def analyze(
    transport: MockTransport,
    *,
    config: Config | None = None,
    dealer_id: str = "DEALER-1001",
    user_id: str = "demo-user",
    revenue_target_one_month: float | None = None,
    revenue_target_three_month: float | None = None,
    request_id: str | None = None,
    input_text: str | None = None,
) -> dict:
    """Return an `inventory-portfolio-result.schema.json`-valid dict."""
    config = config or load_config()
    as_of_dt: datetime = transport.as_of
    as_of: date = as_of_dt.date()
    request_id = request_id or f"req_{uuid.uuid4().hex[:12]}"

    market = MarketDataClient(transport)
    cost = CostClient(transport)

    inventory_response = market.get_dealer_inventory(dealer_id)
    capacity_response = CapacityClient(transport).get_dealer_capacity(dealer_id)
    inbound_response = CapacityClient(transport).get_inbound_inventory(dealer_id)
    policy_response = PolicyClient(transport).get_dealer_pricing_policy(dealer_id)

    responses = [inventory_response, capacity_response, inbound_response, policy_response]
    policy = policy_response.data if policy_response.ok else {}

    capacity_raw = capacity_response.data
    inbound = inbound_response.data or []
    capacity = portfolio_mod.capacity_position(capacity_raw, inbound)
    utilization = capacity["current_utilization"]

    # --- per-vehicle assembly ---------------------------------------------------------
    sim_inputs: list[VehicleSimInput] = []
    records: list[dict] = []
    missing_cost_basis = 0
    missing_market = 0
    missing_comparables = 0

    for payload in inventory_response.data or []:
        vehicle = Vehicle.from_payload(payload)
        vid = vehicle.vehicle_id

        cost_response = cost.get_vehicle_cost_basis(vid)
        try:
            cost_basis = CostBasis.from_payload(cost_response.data, vid)
        except MissingCostBasis:
            # Counted, never silently dropped.
            missing_cost_basis += 1
            continue

        position = market.get_vehicle_market_position(vid)
        if not position.ok or not position.data:
            missing_market += 1
            continue
        responses.append(position)

        comparables_response = market.get_vehicle_comparables(vid)
        velocity = market.get_market_sales_velocity(vehicle.segment)
        engagement = market.get_shopper_engagement(vid)
        if not velocity.ok:
            continue

        comparables = valuation_mod.normalize_comparables(
            vehicle.year, vehicle.mileage, vehicle.trim, vehicle.condition,
            comparables_response.data or [], config,
        )
        internal = valuation_mod.internal_estimate(comparables, config)
        if internal is None:
            missing_comparables += 1

        market_value = float(position.data["market_reference_price"])
        holding = daily_holding_cost(cost_basis.financing_amount, config, utilization)
        monthly_rate, _ = effective_monthly_rate(
            vehicle.segment, vehicle.powertrain, vehicle.age_years(as_of), vehicle.mileage, config
        )
        discount_rate = expected_discount_rate(
            config, vehicle.segment, vehicle.current_list_price or market_value
        )

        accounting = current_accounting_break_even(cost_basis)
        floors = config.pricing["policy_floor"]
        min_safe_transaction = max(
            accounting,
            cost_basis.capitalized_cost * float(floors["cost_basis_multiple"]),
            cost_basis.financing_amount,
            market_value * float(floors["risk_floor_pct_of_market"]),
        )
        min_safe_list = min_safe_transaction / (1.0 - discount_rate)
        list_price = vehicle.current_list_price or market_value

        label = f"{vid}@CURRENT"
        sim_inputs.append(
            VehicleSimInput(
                vehicle_id=label,
                list_price=list_price,
                market_value=market_value,
                days_in_inventory=vehicle.days_in_inventory,
                mileage=vehicle.mileage,
                condition=vehicle.condition,
                segment=vehicle.segment,
                vehicle_age_years=vehicle.age_years(as_of),
                median_days_to_sale=float(velocity.data["median_days_to_sale"]),
                supply_to_sales_ratio=float(velocity.data["supply_to_sales_ratio"]),
                total_cost=cost_basis.capitalized_cost,
                direct_selling_costs=cost_basis.direct_selling_costs,
                daily_cash_holding_cost=holding.cash,
                daily_slot_opportunity_cost=holding.slot_opportunity,
                monthly_depreciation_rate=monthly_rate,
                expected_discount_rate=discount_rate,
                engagement_vdp_views=(engagement.data or {}).get("vdp_views")
                if engagement.ok
                else None,
            )
        )
        records.append(
            {
                "vehicle_id": vid,
                "sim_label": label,
                "make": vehicle.make,
                "model": vehicle.model,
                "segment": vehicle.segment,
                "days_in_inventory": vehicle.days_in_inventory,
                "current_list_price": vehicle.current_list_price,
                "market_value": market_value,
                "internal_estimate": internal,
                "capitalized_cost": cost_basis.capitalized_cost,
                "financing_amount": cost_basis.financing_amount,
                "accounting_break_even": accounting,
                "minimum_safe_list_price": min_safe_list,
                "max_safe_discount": max(0.0, list_price - min_safe_list),
                "recommended_price": None,
                "has_blocking_warning": False,
            }
        )

    if not sim_inputs:
        raise LookupError("No analyzable vehicles in inventory")

    draws = simulate(sim_inputs, config, as_of)

    # Blocking status needs the draws. This is the subset of §19.1 bars evaluable at
    # portfolio level; the vehicle detail view remains authoritative for the full set.
    for record in records:
        i = draws.index_of(record["sim_label"])
        p50_price = float(np.percentile(draws.transaction_price[:, i], 50))
        record["has_blocking_warning"] = bool(
            p50_price < record["accounting_break_even"]
            or (record["current_list_price"] or 0) < record["minimum_safe_list_price"]
        )

    # --- aggregates -------------------------------------------------------------------
    one_month = portfolio_mod.forecast(
        draws, 30, capacity, portfolio_mod.arrivals_within(inbound, as_of, 30),
        config, revenue_target_one_month,
    )
    three_month = portfolio_mod.forecast(
        draws, 90, capacity, portfolio_mod.arrivals_within(inbound, as_of, 90),
        config, revenue_target_three_month,
    )
    valuation_block = portfolio_mod.valuation(draws, records, 30)
    aging = portfolio_mod.aging_profile(draws, records, 30)
    risk = portfolio_mod.risk_ranking(draws, records, config)
    actions = portfolio_mod.recommended_actions(draws, records, config)

    below_break_even = [
        r
        for r in records
        if (r["current_list_price"] or 0) < r["accounting_break_even"]
    ]

    financial_risk = {
        "units_below_break_even": len(below_break_even),
        "pct_below_break_even": len(below_break_even) / len(records),
        "total_exposure_below_break_even": sum(
            r["accounting_break_even"] - (r["current_list_price"] or 0) for r in below_break_even
        ),
        "projected_depreciation_exposure": three_month["depreciation_loss"],
        "projected_cash_holding_exposure": three_month["cash_holding_cost"],
    }

    requested = len(inventory_response.data or [])
    coverage = len(records) / requested if requested else 0.0
    data_coverage = {
        "vehicles_requested": requested,
        "vehicles_analyzed": len(records),
        "missing_cost_basis": missing_cost_basis,
        "missing_market_position": missing_market,
        "missing_comparables": missing_comparables,
        "coverage_ratio": coverage,
        "stale_sources": [
            r.tool for r in freshness.stale_responses(responses)
        ],
    }

    result_warnings = _portfolio_warnings(
        capacity, one_month, three_month, aging, financial_risk, data_coverage,
        revenue_target_one_month, revenue_target_three_month, config,
    )
    result_warnings += freshness.evaluate(responses, config, "PORTFOLIO", dealer_id)
    result_warnings = warnings_mod.sort_by_severity(result_warnings)

    top_contributors = sorted(
        (
            {
                "vehicle_id": r["vehicle_id"],
                "expected_net_economic_value_p50": float(
                    np.percentile(draws.net_economic_value[:, draws.index_of(r["sim_label"])], 50)
                ),
                "expected_gross_p50": float(
                    np.percentile(draws.front_end_gross[:, draws.index_of(r["sim_label"])], 50)
                ),
            }
            for r in records
        ),
        key=lambda x: x["expected_net_economic_value_p50"],
        reverse=True,
    )[: int(config.portfolio["top_contributor_count"])]

    return {
        "dealer_context": {
            "dealer_id": dealer_id,
            "postal_code": None,
            "as_of": as_of_dt.isoformat(),
        },
        "data_coverage": data_coverage,
        "inventory_summary": {
            "active_count": len(records),
            "pending_count": 0,
            "inbound_count": len(inbound),
            "average_days_in_inventory": float(
                np.mean([r["days_in_inventory"] for r in records])
            ),
            "median_days_in_inventory": float(
                np.median([r["days_in_inventory"] for r in records])
            ),
        },
        "portfolio_valuation": valuation_block,
        "aging_profile": aging,
        "capacity_position": capacity,
        "one_month_forecast": one_month,
        "three_month_forecast": three_month,
        "event_adjustments": [],
        "financial_risk": financial_risk,
        "top_contributors": top_contributors,
        "top_risk_vehicles": risk[: int(config.portfolio["top_risk_vehicle_count"])],
        "recommended_actions": actions,
        "warnings": result_warnings,
        "explanation_inputs": collect_explanation_inputs(
            [
                explanation_value("Active inventory", len(records), "UNITS"),
                explanation_value("Current utilization", capacity["current_utilization"], "RATIO"),
                explanation_value("Cash tied up", valuation_block["cash_tied_up"], "USD"),
                explanation_value(
                    "P50 units sold in 30 days", one_month["unit_sales"]["p50"], "UNITS", "unit_sales"
                ),
                explanation_value(
                    "P50 revenue in 30 days", one_month["sales_revenue"]["p50"], "USD", "sales_revenue"
                ),
                explanation_value(
                    "P10 revenue in 30 days", one_month["sales_revenue"]["p10"], "USD", "sales_revenue"
                ),
                explanation_value(
                    "P50 revenue in 90 days", three_month["sales_revenue"]["p50"], "USD", "sales_revenue"
                ),
                explanation_value("Units over 90 days", aging["aged_concentration_pct"], "RATIO"),
                explanation_value("Units below break-even", financial_risk["units_below_break_even"], "UNITS"),
            ],
            [w["code"] for w in result_warnings],
        ),
        "audit": {
            "request_id": request_id,
            "dealer_id": dealer_id,
            "user_id": user_id,
            "input_text": input_text,
            "normalized_request": None,
            "vehicle_identifiers": [r["vehicle_id"] for r in records],
            "mcp_tools_called": transport.audit_calls(),
            "as_of": as_of_dt.isoformat(),
            "config_version": config.config_version,
            "assumption_version": config.assumption_version,
            "model_versions": {"sales_outcome": config.model_version},
            "simulation": draws.reference(),
            "percentile_convention": PERCENTILE_CONVENTION,
            "warning_codes": [w["code"] for w in result_warnings],
            "created_at": as_of_dt.isoformat(),
            "published_at": None,
        },
    }


def _portfolio_warnings(
    capacity, one_month, three_month, aging, financial_risk, data_coverage,
    revenue_target_one, revenue_target_three, config: Config,
) -> list[dict]:
    thresholds = config.portfolio["thresholds"]
    out: list[dict] = []

    def add(**kwargs):
        out.append(warnings_mod.emit(scope="PORTFOLIO", config=config, **kwargs))

    add_target = float(capacity["target_utilization"])
    projected = one_month["ending_utilization"]["p50"]
    if projected > add_target:
        add(
            code="PROJECTED_CAPACITY_OVER_TARGET",
            message=f"Projected 30-day utilization of {projected:.0%} exceeds the {add_target:.0%} target.",
            observed=round(projected, 4), threshold=add_target, unit="RATIO",
            remediation="Consider an event promotion to clear slots.",
        )
    if one_month["risk_probabilities"]["utilization_above_100_percent"] > 0.05:
        add(
            code="PROJECTED_CAPACITY_OVER_100_PERCENT",
            message="There is a material chance the lot exceeds physical capacity within 30 days.",
            observed=round(one_month["risk_probabilities"]["utilization_above_100_percent"], 4),
            threshold=0.05, unit="RATIO",
            remediation="Defer inbound units or accelerate sales.",
        )

    if capacity["effective_open_slots"] < 0:
        add(
            code="INBOUND_CAPACITY_CONFLICT",
            message=(
                f"Committed inbound exceeds available slots by "
                f"{abs(capacity['effective_open_slots'])} unit(s)."
            ),
            observed=float(capacity["effective_open_slots"]), threshold=0.0, unit="UNITS",
            remediation="Reschedule arrivals or wholesale aged units.",
        )

    aged_pct = aging["aged_concentration_pct"]
    if aged_pct > float(thresholds["high_aged_concentration_pct"]):
        add(
            code="HIGH_AGED_INVENTORY_CONCENTRATION",
            message=f"{aged_pct:.0%} of inventory is over 90 days old.",
            observed=round(aged_pct, 4),
            threshold=float(thresholds["high_aged_concentration_pct"]), unit="RATIO",
            remediation="Reprice or promote the aged cohort before it depreciates further.",
        )

    if financial_risk["pct_below_break_even"] > float(thresholds["high_pct_below_break_even"]):
        add(
            code="HIGH_PERCENTAGE_BELOW_BREAK_EVEN",
            message=(
                f"{financial_risk['units_below_break_even']} vehicles are advertised below "
                f"break-even, {financial_risk['pct_below_break_even']:.0%} of the lot."
            ),
            observed=round(financial_risk["pct_below_break_even"], 4),
            threshold=float(thresholds["high_pct_below_break_even"]), unit="RATIO",
            remediation="Each of these books a loss on sale; review individually.",
        )

    for label, forecast_block, target, code in (
        ("30-day", one_month, revenue_target_one, "ONE_MONTH_REVENUE_BELOW_TARGET"),
        ("90-day", three_month, revenue_target_three, "THREE_MONTH_REVENUE_BELOW_TARGET"),
    ):
        risk = forecast_block["risk_probabilities"]["revenue_below_target"]
        if target and risk and risk > 0.5:
            add(
                code=code,
                message=(
                    f"{risk:.0%} chance {label} revenue falls below the "
                    f"${target:,.0f} target."
                ),
                observed=round(risk, 4), threshold=0.5, unit="RATIO",
                remediation="Reprice for velocity or plan a promotion.",
            )

    if data_coverage["coverage_ratio"] < float(thresholds["low_forecast_confidence_coverage"]):
        add(
            code="LOW_PORTFOLIO_FORECAST_CONFIDENCE",
            message=f"Only {data_coverage['coverage_ratio']:.0%} of inventory could be analyzed.",
            observed=round(data_coverage["coverage_ratio"], 4),
            threshold=float(thresholds["low_forecast_confidence_coverage"]), unit="RATIO",
            remediation="Missing cost basis or market data; see data coverage.",
        )
        add(
            code="INCOMPLETE_INVENTORY_DATA",
            message=(
                f"{data_coverage['missing_cost_basis']} vehicle(s) lack cost basis and were "
                "excluded from break-even aggregates."
            ),
            observed=float(data_coverage["missing_cost_basis"]), threshold=0.0, unit="UNITS",
            remediation="Complete the cost records to include them.",
        )

    # Run-off is the default path, so this is always true for the prototype.
    add(
        code="FUTURE_ACQUISITION_DATA_UNAVAILABLE",
        message=(
            "No tool supplies planned acquisitions, so ending inventory, utilization, and "
            "revenue are lower bounds — a real dealer replaces sold units."
        ),
        observed=None, threshold=None, unit="UNITS",
        remediation="Treat the forecast as run-off of current inventory.",
    )
    return out
