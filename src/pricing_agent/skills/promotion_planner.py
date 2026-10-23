"""Skill 3: dealer event promotion planner. §15.

Everything is measured against a no-promotion baseline simulated under the **same seed**,
so the difference between a plan and doing nothing reflects the price change rather than
sampling noise. A plan selecting zero vehicles yields exactly zero incremental units.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date, datetime

import numpy as np

from pricing_agent.config import Config, expected_discount_rate, load_config
from pricing_agent.domain import promotion as promotion_mod
from pricing_agent.domain import promotion_planner as planner
from pricing_agent.domain import valuation as valuation_mod
from pricing_agent.domain.break_even import current_accounting_break_even
from pricing_agent.domain.depreciation import effective_monthly_rate
from pricing_agent.domain.holding_cost import daily_holding_cost
from pricing_agent.domain.portfolio import arrivals_within, capacity_position
from pricing_agent.domain.summarize import (
    PERCENTILE_CONVENTION,
    collect_explanation_inputs,
    explanation_value,
)
from pricing_agent.domain.vehicle import CostBasis, MissingCostBasis, Vehicle
from pricing_agent.mcp_clients import (
    CapacityClient,
    CostClient,
    EventClient,
    MockTransport,
    PolicyClient,
    MarketDataClient,
)
from pricing_agent.policy import warnings as warnings_mod
from pricing_agent.simulation import EventWindow, VehicleSimInput, simulate


def plan_event(
    transport: MockTransport,
    event_id: str,
    target_utilization: float,
    *,
    config: Config | None = None,
    dealer_id: str = "DEALER-1001",
    user_id: str = "demo-user",
    request_id: str | None = None,
    input_text: str | None = None,
) -> dict:
    """Return a `promotion-plan-result.schema.json`-valid dict."""
    config = config or load_config()
    as_of_dt: datetime = transport.as_of
    as_of: date = as_of_dt.date()
    request_id = request_id or f"req_{uuid.uuid4().hex[:12]}"

    market = MarketDataClient(transport)
    cost = CostClient(transport)

    event_response = EventClient(transport).get_event(event_id)
    if not event_response.ok or not event_response.data:
        raise LookupError(f"Unknown event {event_id}")
    event = event_response.data

    start_day = (date.fromisoformat(event["start_date"]) - as_of).days
    end_day = (date.fromisoformat(event["end_date"]) - as_of).days
    event_days = max(1, end_day - start_day + 1)

    # §26.3 requires an event without validated lift; a null forces the configured
    # default and raises LOW_EXPECTED_EVENT_LIFT.
    lift = event.get("historical_demand_lift")
    if lift is None:
        lift = float(config.simulation["event"]["default_lift_multiplier"])
        lift_source = "CONFIG_DEFAULT"
    else:
        lift = float(lift)
        lift_source = "HISTORICAL"

    window = (EventWindow(start_day=max(0, start_day), end_day=max(0, end_day), lift=lift),)

    capacity_raw = CapacityClient(transport).get_dealer_capacity(dealer_id).data
    inbound = CapacityClient(transport).get_inbound_inventory(dealer_id).data or []
    capacity = capacity_position(capacity_raw, inbound)
    policy = PolicyClient(transport).get_dealer_pricing_policy(dealer_id).data or {}

    records, base_inputs = _build(transport, market, cost, config, as_of, capacity, window)
    if not base_inputs:
        raise LookupError("No analyzable vehicles")

    records_by_id = {r["vehicle_id"]: r for r in records}
    baseline = simulate(base_inputs, config, as_of)

    arrivals = arrivals_within(inbound, as_of, max(end_day, 1))
    target_block = planner.inventory_target(
        capacity, baseline, start_day, max(end_day, 1), target_utilization, arrivals
    )
    required = target_block["incremental_promotional_sales_required"]
    target_ending = target_block["target_ending_inventory"]

    candidates = planner.evaluate_candidates(records, baseline, start_day, policy, config)
    eligible = [c for c in candidates if c["eligible"]]

    # Per-candidate economically sensible discount, from the shared simulated ladder.
    ladder_draws = int(config.simulation["run"].get("ladder_draw_count", 500))
    sensible: dict[str, float] = {}
    for candidate in eligible:
        vid = candidate["vehicle_id"]
        base = next(i for i in base_inputs if i.vehicle_id == records_by_id[vid]["sim_label"])

        def resimulate(discount: float, base=base):
            return simulate(
                [replace(base, promotion_discount=discount)],
                config, as_of, draw_count=ladder_draws, seed=baseline.seed,
            )

        _, best = promotion_mod.discount_ladder(
            resimulate, base.vehicle_id, base.list_price,
            float(candidate["pricing"]["max_safe_discount"]), config,
        )
        sensible[vid] = best

    budget = event.get("promotion_budget")
    partner_funded = float(event.get("partner_funded_incentives") or 0.0)

    plans: dict[str, dict] = {}
    for plan_type in planner.PLAN_TYPES:
        selected = planner.select_for_plan(plan_type, candidates, required, config)
        discounts = planner.assign_discounts(plan_type, selected, sensible, budget, config)

        plan_inputs = [
            replace(item, promotion_discount=discounts.get(_vid_of(item), 0.0))
            for item in base_inputs
        ]
        plan_draws = simulate(plan_inputs, config, as_of, seed=baseline.seed)

        plans[plan_type] = planner.build_plan(
            plan_type, selected, discounts, records_by_id, baseline, plan_draws,
            capacity, target_ending, max(end_day, 1), arrivals, budget, partner_funded,
        )

    feasibility = planner.feasibility(
        plans, required, len(eligible), event_days, event.get("historical_demand_lift"),
        lift_source, config,
    )

    recommended = _recommend(plans, feasibility, config)
    result_warnings = _warnings(
        plans, feasibility, eligible, lift_source, budget, recommended, config
    )

    return {
        "promotion_objective": {
            "request_id": request_id,
            "dealer_id": dealer_id,
            "user_id": user_id,
            "as_of": as_of_dt.isoformat(),
            "event": {
                "event_id": event["event_id"],
                "event_name": event["event_name"],
                "start_date": event["start_date"],
                "end_date": event["end_date"],
                "event_type": event.get("event_type"),
                "date_source": "EVENT_CALENDAR",
            },
            "target": {
                "target_inventory_utilization": target_utilization,
                "target_ending_inventory": target_ending,
                "optimization_priority": "BALANCED",
            },
            "constraints": {
                "max_discount_budget": budget,
                "excluded_vehicle_ids": list(policy.get("excluded_from_promotion") or []),
                "approval_policy": "STANDARD",
            },
        },
        "inventory_target_calculation": target_block,
        "feasibility": feasibility,
        "candidate_ranking": [c for c in candidates if c["eligible"]],
        "excluded_vehicles": [c for c in candidates if not c["eligible"]],
        "plans": [plans[t] for t in planner.PLAN_TYPES],
        "recommended_plan": recommended,
        "per_vehicle_actions": _per_vehicle_actions(candidates, plans[recommended["plan_type"]]),
        "projected_ending_inventory": plans[recommended["plan_type"]]["outcomes"]["ending_inventory"],
        "financial_impact": {
            "total_dealer_funded_discount": plans[recommended["plan_type"]]["totals"]["total_dealer_funded"],
            "total_partner_funded_incentive": partner_funded,
            "gross_impact": plans[recommended["plan_type"]]["outcomes"]["gross_impact"],
            "net_economic_value_impact": plans[recommended["plan_type"]]["outcomes"]["net_economic_value_impact"],
            "cash_holding_cost_savings": plans[recommended["plan_type"]]["outcomes"]["cash_holding_cost_savings"],
            "depreciation_savings": plans[recommended["plan_type"]]["outcomes"]["depreciation_savings"],
        },
        "warnings": warnings_mod.sort_by_severity(result_warnings),
        "approvals_required": plans[recommended["plan_type"]]["approvals_required"],
        "explanation_inputs": collect_explanation_inputs(
            [
                explanation_value("Event", event["event_name"]),
                explanation_value("Target utilization", target_utilization, "RATIO"),
                explanation_value("Target ending inventory", target_ending, "UNITS"),
                explanation_value("Incremental units required", required, "UNITS"),
                explanation_value("Eligible candidates", len(eligible), "UNITS"),
                explanation_value("Feasibility", feasibility["status"]),
                explanation_value(
                    "P(target achieved)", feasibility["probability_target_achieved"], "RATIO"
                ),
                explanation_value(
                    "Recommended plan discount",
                    plans[recommended["plan_type"]]["totals"]["total_dealer_funded"], "USD",
                ),
                explanation_value(
                    "Gross impact P50",
                    plans[recommended["plan_type"]]["outcomes"]["gross_impact"]["p50"],
                    "USD", "front_end_gross",
                ),
            ],
            [w["code"] for w in result_warnings],
        ),
        "audit": {
            "request_id": request_id,
            "dealer_id": dealer_id,
            "user_id": user_id,
            "input_text": input_text,
            "vehicle_identifiers": [r["vehicle_id"] for r in records],
            "mcp_tools_called": transport.audit_calls(),
            "as_of": as_of_dt.isoformat(),
            "config_version": config.config_version,
            "assumption_version": config.assumption_version,
            "model_versions": {"sales_outcome": config.model_version},
            "simulation": baseline.reference(),
            "percentile_convention": PERCENTILE_CONVENTION,
            "warning_codes": [w["code"] for w in result_warnings],
            "created_at": as_of_dt.isoformat(),
            "published_at": None,
        },
    }


def _vid_of(item: VehicleSimInput) -> str:
    return item.vehicle_id.split("@")[0]


def _build(transport, market, cost, config, as_of, capacity, window):
    """Assemble records and simulation inputs for every analyzable vehicle."""
    records: list[dict] = []
    inputs: list[VehicleSimInput] = []
    utilization = capacity["current_utilization"]
    floors = config.pricing["policy_floor"]

    for payload in market.get_dealer_inventory().data or []:
        vehicle = Vehicle.from_payload(payload)
        vid = vehicle.vehicle_id
        try:
            cost_basis = CostBasis.from_payload(cost.get_vehicle_cost_basis(vid).data, vid)
        except MissingCostBasis:
            continue

        position = market.get_vehicle_market_position(vid)
        velocity = market.get_market_sales_velocity(vehicle.segment)
        if not position.ok or not velocity.ok:
            continue

        comparables = valuation_mod.normalize_comparables(
            vehicle.year, vehicle.mileage, vehicle.trim, vehicle.condition,
            market.get_vehicle_comparables(vid).data or [], config,
        )
        internal = valuation_mod.internal_estimate(comparables, config)
        engagement = market.get_shopper_engagement(vid)

        market_value = float(position.data["market_reference_price"])
        holding = daily_holding_cost(cost_basis.financing_amount, config, utilization)
        monthly_rate, _ = effective_monthly_rate(
            vehicle.segment, vehicle.powertrain, vehicle.age_years(as_of), vehicle.mileage, config
        )
        discount_rate = expected_discount_rate(
            config, vehicle.segment, vehicle.current_list_price or market_value
        )
        accounting = current_accounting_break_even(cost_basis)
        min_safe_transaction = max(
            accounting,
            cost_basis.capitalized_cost * float(floors["cost_basis_multiple"]),
            cost_basis.financing_amount,
            market_value * float(floors["risk_floor_pct_of_market"]),
        )
        min_safe_list = min_safe_transaction / (1.0 - discount_rate)
        list_price = vehicle.current_list_price or market_value
        label = f"{vid}@CURRENT"

        inputs.append(
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
                engagement_vdp_views=(engagement.data or {}).get("vdp_views") if engagement.ok else None,
                event_windows=window,
            )
        )
        records.append(
            {
                "vehicle_id": vid, "sim_label": label,
                "make": vehicle.make, "model": vehicle.model, "year": vehicle.year,
                "mileage": vehicle.mileage, "segment": vehicle.segment,
                "days_in_inventory": vehicle.days_in_inventory,
                "current_list_price": list_price,
                "market_value": market_value,
                "accounting_break_even": accounting,
                "minimum_safe_transaction_price": min_safe_transaction,
                "minimum_safe_list_price": min_safe_list,
                "max_safe_discount": max(0.0, list_price - min_safe_list),
                "max_accounting_discount": max(0.0, list_price - accounting / (1 - discount_rate)),
                "campaign_participation": list(vehicle.campaign_participation),
                "deal_rating": position.data.get("deal_rating"),
                "supply_to_sales_ratio": float(velocity.data["supply_to_sales_ratio"]),
                "valuation_confidence": "LOW" if internal is None else "OK",
                "engagement_available": engagement.ok,
            }
        )
    return records, inputs


def _recommend(plans: dict, feasibility: dict, config: Config) -> dict:
    default = config.promotion["recommended_plan_default"]
    codes = [f"FEASIBILITY_{feasibility['status']}"]

    if feasibility["status"] == "ACHIEVABLE":
        chosen = default
        codes.append("TARGET_MET_WITHOUT_MAXIMUM_DISCOUNT")
    elif feasibility["status"] == "ACHIEVABLE_WITH_MARGIN_COST":
        chosen = "CAPACITY_FIRST"
        codes.append("TARGET_REQUIRES_MAXIMUM_SAFE_DISCOUNT")
    else:
        chosen = "CAPACITY_FIRST"
        codes.append("TARGET_NOT_REACHABLE_WITHIN_SAFE_HEADROOM")

    return {"plan_id": plans[chosen]["plan_id"], "plan_type": chosen, "rationale_codes": codes}


def _per_vehicle_actions(candidates: list[dict], plan: dict) -> list[dict]:
    promoted = {v["vehicle_id"] for v in plan["vehicles_selected"]}
    actions = []
    for candidate in candidates:
        vid = candidate["vehicle_id"]
        if vid in promoted:
            price = next(v["promotion_price"] for v in plan["vehicles_selected"] if v["vehicle_id"] == vid)
            actions.append({
                "vehicle_id": vid, "action": "PROMOTE", "promotion_price": price,
                "reason": "Selected for the recommended plan",
            })
        elif candidate["eligible"]:
            actions.append({
                "vehicle_id": vid, "action": "PROTECT_PRICE", "promotion_price": None,
                "reason": "Eligible but not required to hit the target",
            })
        else:
            actions.append({
                "vehicle_id": vid, "action": "EXCLUDE", "promotion_price": None,
                "reason": candidate["exclusion_reason"],
            })
    return actions


def _warnings(plans, feasibility, eligible, lift_source, budget, recommended, config) -> list[dict]:
    out: list[dict] = []

    def add(**kwargs):
        out.append(warnings_mod.emit(scope="PLAN", config=config, **kwargs))

    if feasibility["status"] == "NOT_ACHIEVABLE":
        add(code="UNREALISTIC_INVENTORY_TARGET",
            message=(f"Target needs {feasibility['required_incremental_units']} incremental sales; "
                     f"the most aggressive safe plan delivers {feasibility['p50_achievable_incremental_units']:.0f}."),
            observed=float(feasibility["p50_achievable_incremental_units"]),
            threshold=float(feasibility["required_incremental_units"]), unit="UNITS",
            remediation="See the quantified alternatives.")
    if feasibility["status"] in ("AT_RISK", "NOT_ACHIEVABLE"):
        add(code="CAPACITY_TARGET_UNLIKELY_TO_BE_ACHIEVED",
            message=f"Probability of hitting the target is {feasibility['probability_target_achieved']:.0%}.",
            observed=round(feasibility["probability_target_achieved"], 4),
            threshold=float(config.promotion["feasibility"]["achievable_threshold"]), unit="RATIO",
            remediation="Extend the event, raise budget, or revise the target.")

    minimum = int(config.promotion["thresholds"]["min_safe_candidates"])
    if len(eligible) < minimum:
        add(code="INSUFFICIENT_SAFE_PROMOTION_CANDIDATES",
            message=f"Only {len(eligible)} vehicles have safe headroom and are event-eligible.",
            observed=float(len(eligible)), threshold=float(minimum), unit="UNITS",
            remediation="Widen eligibility or accept a lower target.")

    if lift_source == "CONFIG_DEFAULT":
        add(code="LOW_EXPECTED_EVENT_LIFT",
            message="No validated historical lift for this event; a configured default was used.",
            observed=None, threshold=None, unit="RATIO",
            remediation="Treat the incremental units as indicative only.")

    for plan_type, plan in plans.items():
        if not plan["totals"]["within_budget"]:
            add(code="PROMOTION_BUDGET_EXCEEDED",
                message=(f"{plan_type} requires ${plan['totals']['total_dealer_funded']:,.0f} "
                         f"against a ${budget:,.0f} budget."),
                observed=plan["totals"]["total_dealer_funded"], threshold=float(budget), unit="USD",
                remediation="Plan returned truncated at budget.")

    # Cannibalization: more than one unit discounted inside a duplicate group.
    chosen = plans[recommended["plan_type"]]
    if len(chosen["vehicles_selected"]) > 1:
        add(code="PRICE_CANNIBALIZATION_RISK",
            message=("The recommended plan discounts multiple similar vehicles; they compete "
                     "with one another and the joint simulation reflects that."),
            observed=float(len(chosen["vehicles_selected"])),
            threshold=float(config.promotion["thresholds"]["cannibalization_units_in_group"]),
            unit="UNITS",
            remediation="Consider promoting one unit per duplicate group.")
    return out
