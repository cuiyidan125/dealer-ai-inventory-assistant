"""Skill 1: single-vehicle valuation. §13, skills/single-vehicle-valuation/SKILL.md.

Orchestration only — it sequences MCP calls and domain calls and assembles the result.
Every number comes from `domain` or `simulation`; nothing is computed here.

One simulation covers all three pricing strategies by treating each candidate price as a
column in the same draw matrix. They therefore share a seed and a market factor, so the
differences between them reflect the price change rather than sampling noise (D2).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Sequence

import numpy as np

from pricing_agent.config import Config, expected_discount_rate, load_config
from pricing_agent.domain import break_even as break_even_mod
from pricing_agent.domain import depreciation as depreciation_mod
from pricing_agent.domain import promotion as promotion_mod
from pricing_agent.domain import sales_forecast, valuation as valuation_mod
from pricing_agent.domain.holding_cost import daily_holding_cost
from pricing_agent.domain.summarize import (
    PERCENTILE_CONVENTION,
    collect_explanation_inputs,
    explanation_value,
    percentile_set,
    probability,
)
from pricing_agent.domain.vehicle import CostBasis, MissingCostBasis, Vehicle
from pricing_agent.mcp_clients import (
    CapacityClient,
    CostClient,
    EventClient,
    MockTransport,
    PolicyClient,
    ToolResponse,
    MarketDataClient,
)
from pricing_agent.policy import approvals, freshness, price_floor
from pricing_agent.policy import warnings as warnings_mod
from pricing_agent.simulation import VehicleSimInput, simulate

STRATEGIES = ("MAXIMIZE_GROSS", "BALANCED", "INCREASE_VELOCITY")


@dataclass
class SingleVehicleContext:
    """Everything fetched for one vehicle. Assembled once, reused by every step."""

    vehicle: Vehicle
    cost_basis: CostBasis
    market_position: dict
    recommendation: dict | None
    comparables: list[dict]
    velocity: dict
    engagement: dict | None
    price_history: dict | None
    policy: dict | None
    utilization: float | None
    responses: list[ToolResponse]


def _fetch(
    vehicle_id: str, transport: MockTransport, include_capacity: bool = True
) -> SingleVehicleContext:
    market = MarketDataClient(transport)
    cost = CostClient(transport)
    policy_client = PolicyClient(transport)

    inventory = market.get_dealer_inventory()
    payload = next(
        (v for v in (inventory.data or []) if v["vehicle_id"] == vehicle_id), None
    )
    if payload is None:
        raise LookupError(f"{vehicle_id} is not in the active inventory")
    vehicle = Vehicle.from_payload(payload)

    cost_response = cost.get_vehicle_cost_basis(vehicle_id)
    # The one hard stop: without cost basis there is no floor (architecture.md §9).
    cost_basis = CostBasis.from_payload(cost_response.data, vehicle_id)

    position = market.get_vehicle_market_position(vehicle_id)
    recommendation = market.get_vehicle_pricing_recommendation(vehicle_id)
    comparables = market.get_vehicle_comparables(vehicle_id)
    velocity = market.get_market_sales_velocity(vehicle.segment)
    engagement = market.get_shopper_engagement(vehicle_id)
    history = market.get_vehicle_price_history(vehicle_id)
    policy = policy_client.get_dealer_pricing_policy()

    utilization = None
    if include_capacity:
        capacity = CapacityClient(transport).get_dealer_capacity()
        if capacity.ok and capacity.data:
            slots = float(capacity.data["total_physical_slots"])
            utilization = float(capacity.data["current_inventory"]) / slots if slots else None

    if not velocity.ok or not velocity.data:
        raise LookupError(f"No market velocity for segment {vehicle.segment}; cannot forecast")

    return SingleVehicleContext(
        vehicle=vehicle,
        cost_basis=cost_basis,
        market_position=position.data if position.ok else {},
        recommendation=recommendation.data if recommendation.ok else None,
        comparables=comparables.data if comparables.ok else [],
        velocity=velocity.data,
        engagement=engagement.data if engagement.ok else None,
        price_history=history.data if history.ok else None,
        policy=policy.data if policy.ok else None,
        utilization=utilization,
        responses=[
            inventory, cost_response, position, recommendation,
            comparables, velocity, engagement, history, policy,
        ],
    )


def _sim_input(
    ctx: SingleVehicleContext,
    label: str,
    list_price: float,
    market_value: float,
    discount_rate: float,
    monthly_rate: float,
    daily_cash: float,
    daily_slot: float,
    as_of: date,
) -> VehicleSimInput:
    return VehicleSimInput(
        vehicle_id=label,
        list_price=list_price,
        market_value=market_value,
        days_in_inventory=ctx.vehicle.days_in_inventory,
        mileage=ctx.vehicle.mileage,
        condition=ctx.vehicle.condition,
        segment=ctx.vehicle.segment,
        vehicle_age_years=ctx.vehicle.age_years(as_of),
        median_days_to_sale=float(ctx.velocity["median_days_to_sale"]),
        supply_to_sales_ratio=float(ctx.velocity["supply_to_sales_ratio"]),
        total_cost=ctx.cost_basis.capitalized_cost,
        direct_selling_costs=ctx.cost_basis.direct_selling_costs,
        daily_cash_holding_cost=daily_cash,
        daily_slot_opportunity_cost=daily_slot,
        monthly_depreciation_rate=monthly_rate,
        expected_discount_rate=discount_rate,
        engagement_vdp_views=(ctx.engagement or {}).get("vdp_views"),
    )


def analyze(
    vehicle_id: str,
    transport: MockTransport,
    *,
    config: Config | None = None,
    requested_discount: float | None = None,
    user_id: str = "demo-user",
    dealer_id: str = "DEALER-1001",
    request_id: str | None = None,
    input_text: str | None = None,
) -> dict:
    """Run the skill, returning a `single-vehicle-result.schema.json`-valid dict."""
    config = config or load_config()
    as_of_dt: datetime = transport.as_of
    as_of = as_of_dt.date()
    request_id = request_id or f"req_{uuid.uuid4().hex[:12]}"

    ctx = _fetch(vehicle_id, transport)
    vehicle = ctx.vehicle

    # --- valuation (D5) ---------------------------------------------------------------
    comparables = valuation_mod.normalize_comparables(
        vehicle.year, vehicle.mileage, vehicle.trim, vehicle.condition, ctx.comparables, config
    )
    internal = valuation_mod.internal_estimate(comparables, config)

    position_response = next(
        (r for r in ctx.responses if r.tool == "get_vehicle_market_position"), None
    )
    external_stale = bool(position_response and position_response.meta and position_response.meta.is_stale)
    external_available = bool(ctx.market_position) and position_response is not None and position_response.ok

    external_estimate = None
    external_range = None
    methodology = None
    if external_available:
        external_estimate = float(ctx.market_position["market_reference_price"])
        if ctx.recommendation:
            external_estimate = float(ctx.recommendation["recommended_price"])
            rng = ctx.recommendation.get("recommended_range") or {}
            if rng:
                external_range = (float(rng["low"]), float(rng["high"]))
            methodology = ctx.recommendation.get("source_methodology")

    data_age = position_response.meta.age_hours if position_response and position_response.meta else 0.0
    valuation = valuation_mod.reconcile(
        external_estimate=external_estimate,
        external_range=external_range,
        external_methodology=methodology,
        internal=internal,
        comparables=comparables,
        config=config,
        data_age_hours=data_age,
        external_stale=external_stale and not external_available,
    )
    market_value = valuation.market_value

    # --- shared inputs ----------------------------------------------------------------
    holding = daily_holding_cost(ctx.cost_basis.financing_amount, config, ctx.utilization)
    monthly_rate, rate_components = depreciation_mod.effective_monthly_rate(
        vehicle.segment, vehicle.powertrain, vehicle.age_years(as_of), vehicle.mileage, config
    )
    discount_rate = expected_discount_rate(
        config, vehicle.segment, vehicle.current_list_price or market_value
    )

    accounting = break_even_mod.current_accounting_break_even(ctx.cost_basis)
    min_safe_transaction = max(
        accounting,
        ctx.cost_basis.capitalized_cost
        * float(config.pricing["policy_floor"]["cost_basis_multiple"]),
        ctx.cost_basis.financing_amount,
        market_value * float(config.pricing["policy_floor"]["risk_floor_pct_of_market"]),
    )
    min_safe_list = min_safe_transaction / (1.0 - discount_rate)

    # --- candidate prices, one column each --------------------------------------------
    candidates: list[VehicleSimInput] = []
    labels: dict[str, str] = {}
    for strategy in STRATEGIES:
        ratio = float(config.pricing["strategies"][strategy]["price_to_market_target"])
        raw = market_value * ratio
        # Rounding is a presentation rule applied while constructing the candidate; the
        # price is NOT clamped up to the floor. A violation is reported, not corrected.
        priced = price_floor.round_to_price_point(raw, config)
        label = f"{vehicle_id}@{strategy}"
        labels[strategy] = label
        candidates.append(
            _sim_input(ctx, label, priced, market_value, discount_rate, monthly_rate,
                       holding.cash, holding.slot_opportunity, as_of)
        )

    draws = simulate(candidates, config, as_of)
    prices = {s: candidates[i].list_price for i, s in enumerate(STRATEGIES)}

    # --- scenarios --------------------------------------------------------------------
    poor_threshold = float(ctx.market_position.get("poor_deal_threshold", 1.03))
    thresholds = {
        "good_deal": float(ctx.market_position.get("good_deal_threshold", 0.97)),
        "fair_deal": float(ctx.market_position.get("fair_deal_threshold", 1.03)),
        "poor_deal": poor_threshold,
        "source": "PROVIDER" if ctx.market_position.get("good_deal_threshold") else "CONFIG_FALLBACK",
    }

    scenarios: list[dict] = []
    for strategy in STRATEGIES:
        label = labels[strategy]
        block = sales_forecast.scenario_block(draws, label, vehicle.days_in_inventory)
        ratio = prices[strategy] / market_value if market_value else 1.0
        scenarios.append(
            {
                "strategy": strategy,
                "proposed_list_price": prices[strategy],
                "price_to_market_ratio": round(ratio, 4),
                "deal_rating": _deal_rating(ratio, thresholds),
                **block,
                "warnings": [],
            }
        )

    # --- recommended strategy ---------------------------------------------------------
    best, rationale = _select_strategy(scenarios, config)
    recommended_strategy = best["strategy"]
    recommended_price = best["proposed_list_price"]
    recommended_label = labels[recommended_strategy]

    # --- analyses on the recommended column -------------------------------------------
    idx = draws.index_of(recommended_label)
    sales_block = sales_forecast.build(draws, recommended_label, vehicle.days_in_inventory)
    break_even_block = break_even_mod.analyze(
        ctx.cost_basis, market_value, draws, recommended_label, holding.cash,
        discount_rate, ctx.policy, config,
    )
    depreciation_block = depreciation_mod.forecast(
        draws, recommended_label, market_value, monthly_rate, rate_components, valuation.confidence
    )

    # --- headroom and the simulated discount ladder -----------------------------------
    head = promotion_mod.headroom(
        reference_list_price=recommended_price,
        minimum_safe_list_price=break_even_block["minimum_safe_list_price"],
        accounting_break_even=accounting,
        expected_discount_rate=discount_rate,
        original_list_price=vehicle.original_list_price,
        config=config,
    )

    ladder_draws = int(config.simulation["run"].get("ladder_draw_count", 500))

    def resimulate(discount: float):
        probe = _sim_input(
            ctx, recommended_label, recommended_price, market_value, discount_rate,
            monthly_rate, holding.cash, holding.slot_opportunity, as_of,
        )
        probe = VehicleSimInput(**{**probe.__dict__, "promotion_discount": discount})
        return simulate(
            [probe], config, as_of, draw_count=ladder_draws, seed=draws.seed
        )

    ladder, sensible = promotion_mod.discount_ladder(
        resimulate, recommended_label, recommended_price, head.max_safe_discount, config
    )
    recommended_discount = promotion_mod.recommended_discount(sensible, head.max_safe_discount)
    headroom_block = head.as_dict(sensible, recommended_discount, ladder)

    # --- policy (runs last, adds only) ------------------------------------------------
    net_p10 = float(np.percentile(draws.net_economic_value[:, idx], 10))
    prob_negative = probability(draws.net_economic_value[:, idx] < 0)

    result_warnings = warnings_mod.evaluate_single_vehicle(
        vehicle_id=vehicle_id,
        current_list_price=vehicle.current_list_price,
        recommended_price=recommended_price,
        market_value=market_value,
        deal_thresholds=thresholds,
        break_even=break_even_block,
        sales=sales_block,
        depreciation=depreciation_block,
        net_value_p10=net_p10,
        probability_negative_net_value=prob_negative,
        valuation_warnings=valuation.warnings,
        requested_discount=requested_discount,
        max_safe_discount=head.max_safe_discount,
        config=config,
    )
    result_warnings += freshness.evaluate(ctx.responses, config, "VEHICLE", vehicle_id)
    result_warnings = warnings_mod.sort_by_severity(result_warnings)

    required_approvals = approvals.evaluate_single_vehicle(
        current_list_price=vehicle.current_list_price,
        proposed_price=recommended_price,
        accounting_break_even=accounting,
        projected_break_even_p50=break_even_block["projected_break_even"]["p50"],
        transaction_price_p50=sales_block["transaction_price"]["p50"],
        probability_negative_net_value=prob_negative,
        uses_emergency_reserve=(
            recommended_discount
            > head.reserves["negotiation_reserve"] + head.reserves["event_promotion_reserve"]
        ),
        policy=ctx.policy,
        config=config,
    )

    # --- assembly ---------------------------------------------------------------------
    return {
        "vehicle": _vehicle_block(vehicle),
        "valuation": valuation.as_dict(),
        "market_position": {
            "current_list_price": vehicle.current_list_price,
            "price_to_market_ratio": (
                round(vehicle.current_list_price / market_value, 4)
                if vehicle.current_list_price and market_value
                else None
            ),
            "market_percentile": ctx.market_position.get("market_percentile"),
            "deal_rating": ctx.market_position.get("deal_rating", "NO_RATING"),
            "thresholds": thresholds,
        },
        "comparables": [c.as_dict() for c in comparables],
        "break_even_analysis": break_even_block,
        "promotional_headroom": headroom_block,
        "sales_outcome_distribution": sales_block,
        "depreciation_forecast": depreciation_block,
        "pricing_scenarios": scenarios,
        "recommended_strategy": {
            "strategy": recommended_strategy,
            "rationale_codes": rationale,
        },
        "warnings": result_warnings,
        "approvals_required": required_approvals,
        "explanation_inputs": _explanation_inputs(
            vehicle, market_value, recommended_price, recommended_strategy,
            sales_block, break_even_block, depreciation_block, headroom_block,
            best, result_warnings,
        ),
        "audit": _audit(
            request_id, dealer_id, user_id, as_of_dt, transport, config, draws,
            vehicle_id, recommended_price, result_warnings, valuation, input_text,
        ),
    }


def _select_strategy(scenarios: list[dict], config: Config) -> tuple[dict, list[str]]:
    """Pick the recommended strategy, and say why in deterministic codes.

    Median net economic value is the default objective. It is the wrong objective for a
    vehicle at real risk of never selling: past the tail threshold the dealer's exposure
    is dominated by the bad case, and a median-maximizing rule keeps recommending a high
    price on an aged unit — an answer no used-vehicle manager would accept. Beyond the
    threshold the objective switches to the downside percentile.

    The LLM does not make this choice; it renders these codes as prose.
    """
    rules = config.pricing["strategy_selection"]
    age_key = f"over_{int(rules['tail_risk_age_days'])}_days"
    tail_probability = float(rules["tail_risk_probability"])

    # Aging risk is a property of the vehicle, so read it off the least aggressive
    # scenario rather than whichever one happens to win.
    conservative = max(scenarios, key=lambda s: s["proposed_list_price"])
    at_tail_risk = (
        conservative.get("projected_age_exceedance", {}).get(age_key, 0.0) > tail_probability
        or conservative["projected_total_inventory_age"]["p90"]
        > float(rules["tail_risk_age_days"])
    )

    if at_tail_risk:
        percentile = f"p{int(rules['tail_risk_percentile'])}"
        best = max(scenarios, key=lambda s: s["expected_net_economic_value"][percentile])
        codes = [
            "TAIL_RISK_OBJECTIVE",
            f"MAXIMIZES_P{int(rules['tail_risk_percentile'])}_NET_ECONOMIC_VALUE",
            "AGED_INVENTORY_PRESSURE",
        ]
    else:
        percentile = f"p{int(rules['objective_percentile'])}"
        best = max(scenarios, key=lambda s: s["expected_net_economic_value"][percentile])
        codes = [f"MAXIMIZES_P{int(rules['objective_percentile'])}_NET_ECONOMIC_VALUE"]

    codes.append(f"STRATEGY_{best['strategy']}")
    return best, codes


def _deal_rating(ratio: float, thresholds: dict) -> str:
    if ratio <= thresholds["good_deal"] - 0.04:
        return "GREAT"
    if ratio <= thresholds["good_deal"]:
        return "GOOD"
    if ratio <= thresholds["fair_deal"]:
        return "FAIR"
    return "HIGH"


def _vehicle_block(vehicle: Vehicle) -> dict:
    return {
        "vehicle_id": vehicle.vehicle_id,
        "vin": vehicle.vin,
        "year": vehicle.year,
        "make": vehicle.make,
        "model": vehicle.model,
        "trim": vehicle.trim,
        "mileage": vehicle.mileage,
        "segment": vehicle.segment,
        "powertrain": vehicle.powertrain,
        "condition": vehicle.condition,
        "current_list_price": vehicle.current_list_price,
        "original_list_price": vehicle.original_list_price,
        "days_in_inventory": vehicle.days_in_inventory,
        "status": vehicle.status,
        "campaign_participation": list(vehicle.campaign_participation),
        "image_url": vehicle.image_url,
    }


def _explanation_inputs(
    vehicle, market_value, price, strategy, sales, break_even, depreciation,
    headroom, scenario, result_warnings,
) -> dict:
    """The narration allow-list. The LLM sees only this (architecture.md §3.4)."""
    values = [
        explanation_value("Vehicle", vehicle.description),
        explanation_value("Days in inventory", vehicle.days_in_inventory, "DAYS"),
        explanation_value("Current list price", vehicle.current_list_price, "USD"),
        explanation_value("Market value", market_value, "USD"),
        explanation_value("Recommended price", price, "USD"),
        explanation_value("Strategy", strategy),
        explanation_value(
            "P50 days to sale", sales["additional_days_to_sale"]["p50"], "DAYS",
            "additional_days_to_sale",
        ),
        explanation_value(
            "P90 days to sale", sales["additional_days_to_sale"]["p90"], "DAYS",
            "additional_days_to_sale",
        ),
        explanation_value(
            "Probability sold within 30 days", sales["sale_probabilities"]["within_30_days"], "RATIO"
        ),
        explanation_value("Break-even", break_even["current_accounting_break_even"], "USD"),
        explanation_value("Minimum safe list price", break_even["minimum_safe_list_price"], "USD"),
        explanation_value(
            "P50 front-end gross", scenario["expected_front_end_gross"]["p50"], "USD",
            "front_end_gross",
        ),
        explanation_value(
            "P50 net economic value", scenario["expected_net_economic_value"]["p50"], "USD",
            "net_economic_value",
        ),
        explanation_value(
            "P90 depreciation loss", depreciation["depreciation_loss"]["p90"], "USD",
            "depreciation_loss",
        ),
        explanation_value("Maximum safe discount", headroom["max_safe_discount"], "USD"),
    ]
    return collect_explanation_inputs(values, [w["code"] for w in result_warnings])


def _audit(
    request_id, dealer_id, user_id, as_of, transport, config, draws,
    vehicle_id, recommended_price, result_warnings, valuation, input_text,
) -> dict:
    stamps = config.version_stamps()
    return {
        "request_id": request_id,
        "dealer_id": dealer_id,
        "user_id": user_id,
        "input_text": input_text,
        "normalized_request": None,
        "vehicle_identifiers": [vehicle_id],
        "mcp_tools_called": transport.audit_calls(),
        "as_of": as_of.isoformat(),
        "config_version": stamps["config_version"],
        "assumption_version": stamps["assumption_version"],
        "model_versions": {"sales_outcome": stamps["model_version"]},
        "simulation": draws.reference(),
        "percentile_convention": PERCENTILE_CONVENTION,
        "valuation_source": {
            "anchor": valuation.anchor,
            "internal_check_computed": valuation.internal_estimate is not None,
            "divergence": valuation.divergence,
            "provider_thresholds_used": True,
        },
        "system_recommendation": recommended_price,
        "selected_action": None,
        "user_selected_price": None,
        "override_reason": None,
        "warning_codes": [w["code"] for w in result_warnings],
        "approving_manager": None,
        "approval_id": None,
        "final_price": None,
        "created_at": as_of.isoformat(),
        "published_at": None,
    }
