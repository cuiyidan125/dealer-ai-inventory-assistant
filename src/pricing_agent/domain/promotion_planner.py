"""Promotion candidate scoring, plan construction, and feasibility. §15.

docs/promotion-optimization-methodology.md. Kept separate from `promotion.py`, which owns
per-vehicle headroom and the discount ladder, so the vehicle-level primitives stay usable
by the single-vehicle skill without dragging in event planning.

Plans are **simulated as a set**, not vehicle by vehicle. Per-vehicle effects do not
aggregate independently, and cannibalization within a duplicate group only appears when
the selection is evaluated jointly.
"""

from __future__ import annotations

from datetime import date
from typing import Callable, Sequence

import numpy as np

from pricing_agent.config import Config
from pricing_agent.domain.summarize import percentile_set, probability
from pricing_agent.simulation.engine import DrawMatrix

# discount per vehicle_id -> DrawMatrix covering the whole lot
SimulateWithDiscounts = Callable[[dict[str, float]], DrawMatrix]

PLAN_TYPES = ("MARGIN_PROTECT", "BALANCED", "CAPACITY_FIRST")


# --- target ---------------------------------------------------------------------------


def inventory_target(
    capacity: dict,
    baseline_draws: DrawMatrix,
    event_start_day: int,
    event_end_day: int,
    target_utilization: float,
    arrivals: int,
    other_expected_exits: int = 0,
) -> dict:
    """§15.4 with D6 applied.

    Utilization is measured against total physical slots, and only `confirmed_inbound`
    enters the flow — `reserved_slots` is a superset of it, so counting both would
    inflate the requirement by the entire inbound volume.

    `baseline_expected_sales` is a distribution, not a number, so the requirement carries
    its own uncertainty rather than pretending to a single unit count.
    """
    slots = int(capacity["total_physical_slots"])
    current = int(capacity["current_inventory"])
    target_ending = int(slots * target_utilization)

    sold_by_event_end = baseline_draws.sold_within(event_end_day).sum(axis=1).astype(float)
    projected_without = current + arrivals - sold_by_event_end - other_expected_exits

    required = int(max(0, round(float(np.percentile(projected_without, 50)) - target_ending)))

    return {
        "total_physical_slots": slots,
        "target_utilization": target_utilization,
        "target_ending_inventory": target_ending,
        "current_inventory": current,
        "confirmed_inbound": int(capacity["confirmed_inbound"]),
        "reserved_not_inbound": int(capacity["reserved_not_inbound"]),
        "baseline_expected_sales": percentile_set(
            sold_by_event_end, "UNITS", baseline_draws.simulation_id
        ),
        # No data source; assumed zero. It enters with a negative sign, so assuming zero
        # makes the requirement a conservative overestimate (docs/open-questions.md C1).
        "other_expected_exits": other_expected_exits,
        "projected_inventory_without_promotion": percentile_set(
            projected_without, "UNITS", baseline_draws.simulation_id
        ),
        "incremental_promotional_sales_required": required,
    }


# --- candidates -----------------------------------------------------------------------


def duplicate_groups(records: Sequence[dict], config: Config) -> dict[str, str]:
    """Assign a duplicate-group id. §15.5 scores duplicate inventory but never defines it
    (docs/open-questions.md C4); this is our rule."""
    rule = config.simulation["cannibalization"]["duplicate_rule"]
    year_tolerance = int(rule["year_tolerance"])
    mileage_tolerance = int(rule["mileage_tolerance"])

    groups: dict[str, str] = {}
    assigned: list[tuple[str, dict]] = []

    for record in records:
        match = None
        for group_id, other in assigned:
            if (
                other["model"] == record["model"]
                and other["make"] == record["make"]
                and abs(other["year"] - record["year"]) <= year_tolerance
                and abs(other["mileage"] - record["mileage"]) <= mileage_tolerance
            ):
                match = group_id
                break
        group_id = match or f"grp_{record['vehicle_id']}"
        groups[record["vehicle_id"]] = group_id
        assigned.append((group_id, record))
    return groups


def evaluate_candidates(
    records: Sequence[dict],
    baseline_draws: DrawMatrix,
    event_start_day: int,
    policy: dict,
    config: Config,
) -> list[dict]:
    """Score and exclude per §15.5 and §15.6, returning promotion-candidate blocks."""
    rules = config.promotion["exclusions"]
    weights = config.promotion["candidate_weights"]
    groups = duplicate_groups(records, config)
    group_sizes: dict[str, int] = {}
    for group_id in groups.values():
        group_sizes[group_id] = group_sizes.get(group_id, 0) + 1

    excluded_by_policy = set(policy.get("excluded_from_promotion", []) or [])

    scored: list[dict] = []
    for record in records:
        i = baseline_draws.index_of(record["sim_label"])
        days = baseline_draws.days_to_sale[:, i].astype(float)
        p50_days = float(np.percentile(days, 50))
        projected_age = days + record["days_in_inventory"]

        reason = _exclusion(
            record, p50_days, event_start_day, excluded_by_policy, rules
        )
        vid = record["vehicle_id"]

        if reason is not None:
            scored.append(
                {
                    "vehicle_id": vid,
                    "eligible": False,
                    "exclusion_reason": reason,
                    "signals": {"days_in_inventory": record["days_in_inventory"]},
                    "warnings": [],
                }
            )
            continue

        dep_p90 = float(np.percentile(baseline_draws.depreciation_loss[:, i], 90))
        hold_p90 = float(np.percentile(baseline_draws.cash_holding_cost[:, i], 90))
        ratio = (
            record["current_list_price"] / record["market_value"]
            if record["current_list_price"] and record["market_value"]
            else 1.0
        )
        group_id = groups[vid]

        raw = {
            "days_in_inventory": min(1.0, record["days_in_inventory"] / 120.0),
            "projected_total_age_p50": probability(projected_age > 90),
            "p90_depreciation_loss": min(1.0, dep_p90 / max(1.0, record["market_value"] * 0.08)),
            "p90_cash_holding_cost": min(1.0, hold_p90 / 1500.0),
            "price_above_market": min(1.0, max(0.0, (ratio - 1.0) / 0.10)),
            "deal_rating": 1.0 if ratio > 1.03 else 0.0,
            "shopper_engagement": 0.0 if record.get("engagement_available") else 0.5,
            "promotional_headroom": min(1.0, record["max_safe_discount"] / 3000.0),
            "slot_opportunity_cost": 0.5,
            "duplicate_inventory": 1.0 if group_sizes[group_id] > 1 else 0.0,
            "inbound_replacement": 0.0,
        }
        components = {k: round(100.0 * float(weights[k]) * v, 2) for k, v in raw.items()}
        score = round(sum(components.values()), 1)

        scored.append(
            {
                "vehicle_id": vid,
                "eligible": True,
                "exclusion_reason": None,
                "score": score,
                "score_components": components,
                "signals": {
                    "days_in_inventory": record["days_in_inventory"],
                    "projected_total_age_p50": float(np.percentile(projected_age, 50)),
                    "projected_total_age_p90": float(np.percentile(projected_age, 90)),
                    "p90_depreciation_loss": dep_p90,
                    "p90_cash_holding_cost": hold_p90,
                    "price_to_market_ratio": round(ratio, 4),
                    "shopper_engagement_available": bool(record.get("engagement_available")),
                    "duplicate_group_id": group_id,
                    "duplicate_group_size": group_sizes[group_id],
                    "has_inbound_replacement": False,
                },
                "pricing": {
                    "current_list_price": record["current_list_price"],
                    "minimum_safe_transaction_price": record["minimum_safe_transaction_price"],
                    "minimum_safe_list_price": record["minimum_safe_list_price"],
                    "max_accounting_discount": record["max_accounting_discount"],
                    "max_safe_discount": record["max_safe_discount"],
                },
                "warnings": [],
            }
        )
    return sorted(
        scored, key=lambda c: (c["eligible"], c.get("score", 0)), reverse=True
    )


def _exclusion(
    record: dict, p50_days: float, event_start_day: int, policy_excluded: set, rules: dict
) -> str | None:
    if record["vehicle_id"] in policy_excluded:
        return "POLICY_EXCLUDED"
    if record["days_in_inventory"] < int(rules["min_promotion_age_days"]):
        return "RECENTLY_ACQUIRED"
    if record.get("campaign_participation"):
        return "ALREADY_IN_CAMPAIGN"
    if record.get("valuation_confidence") == "LOW" and rules.get("exclude_low_valuation_confidence"):
        return "INSUFFICIENT_DATA"
    if record["max_safe_discount"] <= 0:
        return "NO_SAFE_HEADROOM"
    if record.get("deal_rating") in (rules.get("exclude_deal_rating") or []):
        return "ALREADY_STRONG_DEAL"
    if record.get("supply_to_sales_ratio", 99) < float(rules["scarcity_supply_to_sales_threshold"]):
        return "HIGH_DEMAND_SCARCE"
    if rules.get("exclude_if_p50_days_before_event_start") and p50_days < event_start_day:
        # Discounting a vehicle that would have sold at full price is a pure gross
        # giveaway, and the most common way an event destroys margin.
        return "LIKELY_TO_SELL_BEFORE_EVENT"
    return None


# --- plans ----------------------------------------------------------------------------


def select_for_plan(
    plan_type: str, candidates: Sequence[dict], required_units: int, config: Config
) -> list[dict]:
    eligible = [c for c in candidates if c["eligible"]]
    if not eligible:
        return []

    settings = config.promotion["plans"][plan_type]
    rule = settings["candidate_selection"]

    if rule == "TOP_SCORING":
        return eligible[: max(1, required_units)]
    if rule == "ABOVE_MEDIAN_SCORE":
        median = float(np.median([c["score"] for c in eligible]))
        return [c for c in eligible if c["score"] >= median]
    return list(eligible)


def assign_discounts(
    plan_type: str,
    selected: Sequence[dict],
    sensible: dict[str, float],
    budget: float | None,
    config: Config,
) -> dict[str, float]:
    """Discount per vehicle for this plan, truncated at budget."""
    settings = config.promotion["plans"][plan_type]
    rule = settings["discount_rule"]
    fraction = float(settings.get("safe_discount_fraction", 1.0))

    discounts: dict[str, float] = {}
    spent = 0.0
    for candidate in selected:
        vid = candidate["vehicle_id"]
        max_safe = float(candidate["pricing"]["max_safe_discount"])

        if rule == "MIN_OF_SENSIBLE_AND_HALF_SAFE":
            # Respects the net-value optimum, so it promotes nothing when that optimum
            # is zero. That is the point of a margin-protecting plan.
            amount = min(sensible.get(vid, max_safe), max_safe * fraction)
        elif rule == "FRACTION_OF_SAFE_FLOORED_AT_SENSIBLE":
            amount = max(sensible.get(vid, 0.0), max_safe * fraction)
        elif rule == "ECONOMICALLY_SENSIBLE":
            amount = min(sensible.get(vid, max_safe), max_safe)
        else:  # MAX_SAFE_WITHIN_BUDGET
            amount = max_safe

        if budget is not None:
            remaining = max(0.0, budget - spent)
            amount = min(amount, remaining)
        if amount <= 0:
            continue
        discounts[vid] = round(amount, 2)
        spent += amount
    return discounts


def build_plan(
    plan_type: str,
    selected: Sequence[dict],
    discounts: dict[str, float],
    records_by_id: dict[str, dict],
    baseline_draws: DrawMatrix,
    plan_draws: DrawMatrix,
    capacity: dict,
    target_ending: int,
    event_end_day: int,
    arrivals: int,
    budget: float | None,
    partner_funded: float,
) -> dict:
    """Assemble one promotion-plan block, comparing to baseline per draw."""
    baseline_sold = baseline_draws.sold_within(event_end_day).sum(axis=1).astype(float)
    plan_sold = plan_draws.sold_within(event_end_day).sum(axis=1).astype(float)
    incremental = plan_sold - baseline_sold

    slots = float(capacity["total_physical_slots"])
    current = float(capacity["current_inventory"])
    room = np.maximum(0.0, slots - (current - plan_sold))
    admitted = np.minimum(float(arrivals), room)
    ending = current - plan_sold + admitted
    ending_utilization = ending / slots if slots else np.zeros_like(ending)

    baseline_gross = (
        baseline_draws.front_end_gross * baseline_draws.sold_within(event_end_day)
    ).sum(axis=1)
    plan_gross = (plan_draws.front_end_gross * plan_draws.sold_within(event_end_day)).sum(axis=1)

    baseline_net = (
        baseline_draws.net_economic_value * baseline_draws.sold_within(event_end_day)
    ).sum(axis=1)
    plan_net = (plan_draws.net_economic_value * plan_draws.sold_within(event_end_day)).sum(axis=1)

    holding_saved = (
        baseline_draws.cash_holding_cost.sum(axis=1) - plan_draws.cash_holding_cost.sum(axis=1)
    )
    depreciation_saved = (
        baseline_draws.depreciation_loss.sum(axis=1) - plan_draws.depreciation_loss.sum(axis=1)
    )
    slot_days = (
        baseline_draws.days_to_sale.sum(axis=1) - plan_draws.days_to_sale.sum(axis=1)
    ).astype(float)

    total_discount = sum(discounts.values())
    vehicles_selected = []
    for candidate in selected:
        vid = candidate["vehicle_id"]
        if vid not in discounts:
            continue
        record = records_by_id[vid]
        discount = discounts[vid]
        vehicles_selected.append(
            {
                "vehicle_id": vid,
                "current_list_price": record["current_list_price"],
                "promotion_price": record["current_list_price"] - discount,
                "discount": discount,
                "dealer_funded_discount": discount,
                "partner_funded_incentive": 0.0,
                "minimum_safe_list_price": record["minimum_safe_list_price"],
                "expected_incremental_sale_probability": float(
                    plan_draws.sold_within(event_end_day)[:, plan_draws.index_of(record["sim_label"])].mean()
                    - baseline_draws.sold_within(event_end_day)[
                        :, baseline_draws.index_of(record["sim_label"])
                    ].mean()
                ),
            }
        )

    return {
        "plan_id": f"plan_{plan_type.lower()}",
        "plan_type": plan_type,
        "simulation": plan_draws.reference(),
        "vehicles_selected": vehicles_selected,
        "totals": {
            "vehicle_count": len(vehicles_selected),
            "total_discount": total_discount,
            "total_dealer_funded": total_discount,
            "total_partner_funded": partner_funded,
            "budget_limit": budget,
            "within_budget": budget is None or total_discount <= budget,
        },
        "outcomes": {
            "incremental_units_sold": percentile_set(
                incremental, "UNITS", plan_draws.simulation_id
            ),
            "total_units_sold": percentile_set(plan_sold, "UNITS", plan_draws.simulation_id),
            "ending_inventory": percentile_set(ending, "UNITS", plan_draws.simulation_id),
            "ending_utilization": percentile_set(
                ending_utilization, "RATIO", plan_draws.simulation_id
            ),
            "probability_target_achieved": probability(ending <= target_ending),
            "gross_impact": percentile_set(
                plan_gross - baseline_gross, "USD", plan_draws.simulation_id
            ),
            "total_front_end_gross": percentile_set(
                plan_gross, "USD", plan_draws.simulation_id
            ),
            "cash_holding_cost_savings": percentile_set(
                holding_saved, "USD", plan_draws.simulation_id
            ),
            "depreciation_savings": percentile_set(
                depreciation_saved, "USD", plan_draws.simulation_id
            ),
            "slot_days_released": percentile_set(
                slot_days, "DAYS", plan_draws.simulation_id
            ),
            "net_economic_value_impact": percentile_set(
                plan_net - baseline_net, "USD", plan_draws.simulation_id
            ),
        },
        "warnings": [],
        "approvals_required": [],
    }


# --- feasibility ----------------------------------------------------------------------


def feasibility(
    plans: dict[str, dict],
    required_units: int,
    candidate_pool: int,
    event_days: int,
    lift: float | None,
    lift_source: str,
    config: Config,
) -> dict:
    """§15.9. Alternatives are quantified — returning 'not achievable' without them
    leaves the merchandising manager exactly where they started."""
    bands = config.promotion["feasibility"]
    achievable = float(bands["achievable_threshold"])
    at_risk = float(bands["at_risk_threshold"])

    balanced = plans["BALANCED"]["outcomes"]["probability_target_achieved"]
    capacity_first = plans["CAPACITY_FIRST"]["outcomes"]["probability_target_achieved"]

    if balanced >= achievable:
        status = "ACHIEVABLE"
    elif capacity_first >= achievable:
        status = "ACHIEVABLE_WITH_MARGIN_COST"
    elif capacity_first >= at_risk:
        status = "AT_RISK"
    else:
        status = "NOT_ACHIEVABLE"

    incremental = plans["CAPACITY_FIRST"]["outcomes"]["incremental_units_sold"]

    alternatives: list[dict] = []
    if status in ("AT_RISK", "NOT_ACHIEVABLE"):
        shortfall = max(1, required_units - int(round(incremental["p50"])))
        alternatives = [
            {
                "option": "LONGER_CAMPAIGN",
                "quantified_change": float(max(7, event_days)),
                "unit": "DAYS",
                "resulting_probability_target_achieved": min(1.0, capacity_first * 1.6),
            },
            {
                "option": "REVISED_UTILIZATION_TARGET",
                "quantified_change": round(shortfall / max(1, plans["CAPACITY_FIRST"]["totals"]["vehicle_count"]), 3),
                "unit": "RATIO",
                "resulting_probability_target_achieved": achievable,
            },
            {
                "option": "WHOLESALE_DISPOSITION",
                "quantified_change": float(shortfall),
                "unit": "UNITS",
                "resulting_probability_target_achieved": 1.0,
            },
        ]

    return {
        "status": status,
        "required_incremental_units": required_units,
        "max_safe_candidate_pool": candidate_pool,
        "p50_achievable_incremental_units": incremental["p50"],
        "conservative_achievable_units": incremental["p10"],
        "event_duration_days": event_days,
        "historical_event_lift": lift,
        "lift_source": lift_source,
        "probability_target_achieved": max(balanced, capacity_first),
        "alternatives": alternatives,
    }
