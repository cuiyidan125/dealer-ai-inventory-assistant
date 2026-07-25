"""Dealer-facing terminology — the one place user-visible copy lives.

Every metric label, table column, chart label, plan/strategy/state name, warning and approval
label, and glossary entry is defined here so the views read a label instead of hand-assembling
strings. The rule this module exists to keep: **business meaning first, the statistic in
parentheses** — "Expected days to sale (P50)", not "P50 Days to Sale".

It maps codes and fields to words. It computes nothing — no price, no percentile, no forecast.
Raw codes and IDs are preserved for audit sections via `audit_label` and the reason-code maps.

Reason-code and exclusion labels are sourced from `improve_aging_copy` (already centralized)
and re-exported here so a view has a single import.
"""

from __future__ import annotations

from pricing_agent.views.improve_aging_copy import (  # noqa: F401  (re-exported)
    ACTION_LABELS,
    BUSINESS_RULE,
    CANDIDATE_CATEGORIES,
    DATA_LIMITATION,
    EXCLUSION_CATEGORY,
    EXCLUSION_LABELS,
    SAFETY_RULE,
    SELECTION_LABELS,
    action_label,
    candidate_categories,
    exclusion_category,
    exclusion_label,
    next_steps,
    recommendation_statement,
    selection_label,
)

# --- pricing approaches (single-vehicle strategies) -----------------------------------

STRATEGY: dict[str, dict[str, str]] = {
    "MAXIMIZE_GROSS": {"name": "Protect profit",
                       "trade_off": "Holds the price to protect front-end gross, accepting a slower sale."},
    "BALANCED": {"name": "Balance profit and sales speed",
                 "trade_off": "Trades a little gross for a faster, more predictable sale."},
    "INCREASE_VELOCITY": {"name": "Sell faster",
                          "trade_off": "Prices to sell sooner, giving up some gross to reduce time on lot."},
}


def strategy_name(code: str) -> str:
    return STRATEGY.get(code, {}).get("name", _humanize(code))


# --- sale-event plans (promotion plans) -----------------------------------------------

PLAN: dict[str, dict[str, str]] = {
    "MARGIN_PROTECT": {"name": "Prioritize profit protection",
                       "trade_off": "Discounts the fewest vehicles to protect gross; slower to free space."},
    "BALANCED": {"name": "Balance sales and profit",
                 "trade_off": "A middle path between protecting gross and freeing space."},
    "CAPACITY_FIRST": {"name": "Prioritize freeing inventory space",
                       "trade_off": "Discounts the most vehicles to free space fastest, at more gross cost."},
}


def plan_name(code: str) -> str:
    return PLAN.get(code, {}).get("name", _humanize(code))


def plan_trade_off(code: str) -> str:
    return PLAN.get(code, {}).get("trade_off", "")


# --- analysis / workflow states -------------------------------------------------------

STATE: dict[str, str] = {
    "ROUTED_AND_EXECUTED": "Analysis completed",
    "NEEDS_CLARIFICATION": "More information is needed",
    "NO_MATCH": "No matching vehicle or event was found",
    "AMBIGUOUS_MATCH": "More than one possible match was found",
    "PARTIAL_RESULT": "Some analysis completed",
    "TARGET_NOT_ACHIEVABLE": "Target is unlikely to be reached",
    "NO_SAFE_ACTIONS": "No action meets the current safety rules",
    "WORKFLOW_NOT_YET_AVAILABLE": "This analysis is not available yet",
    "EXECUTION_ERROR": "The analysis could not be completed",
}


def state_label(code: str) -> str:
    return STATE.get(code, _humanize(code))


# --- feasibility (promotion) ----------------------------------------------------------

FEASIBILITY: dict[str, str] = {
    "ACHIEVABLE": "Likely to reach the target",
    "ACHIEVABLE_WITH_MARGIN_COST": "Reachable, but at a gross cost",
    "AT_RISK": "At risk — uncertain",
    "NOT_ACHIEVABLE": "Unlikely under the current constraints",
}


def feasibility_label(code: str) -> str:
    return FEASIBILITY.get(code, _humanize(code))


# --- approvals ------------------------------------------------------------------------
# label + the boundary that triggered it. No role is invented — the result does not name one.

APPROVAL: dict[str, dict[str, str]] = {
    "LOSS_MINIMIZATION": {"label": "Manager review required",
                          "why": "The recommendation would sell at a loss to reduce aging."},
    "BELOW_PROJECTED_BREAK_EVEN": {"label": "Manager review required",
                                   "why": "The expected sale price is below break-even."},
    "NEGATIVE_VALUE_RISK": {"label": "Manager review required",
                            "why": "There is a meaningful chance of a negative total economic value."},
    "AGGRESSIVE_ADJUSTMENT": {"label": "Manager review required",
                              "why": "The price change crosses the configured discount-approval threshold."},
}


def approval_label(code: str) -> str:
    return APPROVAL.get(code, {}).get("label", "Manager review required")


def approval_why(code: str) -> str:
    return APPROVAL.get(code, {}).get("why", "This recommendation crosses a configured approval boundary.")


# --- warnings -------------------------------------------------------------------------
# The policy layer already provides a plain message and remediation. Here we give the code an
# action-oriented label; the message + remediation still explain the risk and the action.

WARNING: dict[str, str] = {
    "HIGH_PERCENTAGE_BELOW_BREAK_EVEN": "Several vehicles are priced below break-even",
    "INBOUND_CAPACITY_CONFLICT": "Incoming vehicles exceed open spaces",
    "FUTURE_ACQUISITION_DATA_UNAVAILABLE": "Planned purchases are not available to the forecast",
    "CAPACITY_TARGET_UNLIKELY_TO_BE_ACHIEVED": "Target may require a longer event window",
    "PRICE_CANNIBALIZATION_RISK": "Discounting more vehicles risks competing with each other",
    "UNREALISTIC_INVENTORY_TARGET": "The target may be too aggressive for this window",
    "STALE_MARKET_DATA": "Some market data is older than expected",
    "P90_PROJECTED_INVENTORY_AGE_OVER_90_DAYS": "Meaningful risk of exceeding 90 days on lot",
    "P50_PROJECTED_INVENTORY_AGE_OVER_90_DAYS": "Expected to exceed 90 days on lot",
    "P90_PROJECTED_INVENTORY_AGE_OVER_120_DAYS": "Meaningful risk of exceeding 120 days on lot",
    "P50_PROJECTED_INVENTORY_AGE_OVER_120_DAYS": "Expected to exceed 120 days on lot",
    "BREAK_EVEN_EXCEEDS_MARKET_VALUE": "Break-even is above estimated market value",
    "BREAK_EVEN_MARKET_CROSSOVER_RISK": "Break-even is close to market value",
    "MINIMUM_SAFE_LIST_PRICE_VIOLATION": "Price would fall below the lowest safe asking price",
    "PRICE_BELOW_CURRENT_BREAK_EVEN": "Price would fall below break-even",
    "P50_TRANSACTION_PRICE_BELOW_BREAK_EVEN": "Expected sale price is below break-even",
    "HIGH_PROBABILITY_OF_NEGATIVE_NET_VALUE": "Meaningful chance of a negative total economic value",
    "LOW_EXPECTED_EVENT_LIFT": "Limited evidence for this event's sales lift",
    "LOW_VALUATION_CONFIDENCE": "Lower confidence in the market estimate",
    "INSUFFICIENT_VEHICLE_DATA": "Not enough vehicle data to complete this step",
}


def warning_label(code: str) -> str:
    return WARNING.get(code, _humanize(code))


# --- metrics: label (business-first, statistic in parens) + short definition ----------

METRIC: dict[str, dict[str, str]] = {
    # pricing
    "current_price": {"label": "Current asking price"},
    "recommended_price": {"label": "Recommended asking price"},
    "market_value": {"label": "Estimated market value",
                     "def": "A market-based estimate for this vehicle."},
    "expected_gross_p50": {"label": "Expected front-end gross (P50)",
                           "def": "The typical front-end gross across simulated sales."},
    "downside_gross_p10": {"label": "Downside front-end gross (P10)"},
    "expected_days_p50": {"label": "Expected days to sale (P50)",
                          "def": "The typical additional days until the vehicle sells."},
    "conservative_days_p90": {"label": "Conservative days to sale (P90)",
                              "def": "A cautious planning estimate for time to sale."},
    "chance_30d": {"label": "Chance of selling within 30 days"},
    "break_even": {"label": "Break-even price",
                   "def": "Where expected proceeds cover the configured cost basis and costs."},
    "min_safe": {"label": "Lowest safe asking price",
                 "def": "The lowest asking price before crossing a financial or approval boundary."},
    "max_safe_discount": {"label": "Maximum safe discount",
                          "def": "The most you can discount before crossing the safety boundary."},
    "expected_total_value_p50": {"label": "Expected total economic value (P50)",
                                 "def": "Front-end gross adjusted for expected aging-related costs."},
    "downside_total_value_p10": {"label": "Downside total economic value (P10)"},
    # inventory / capacity
    "units_on_lot": {"label": "Units on lot"},
    "lot_capacity_used": {"label": "Lot capacity used",
                          "def": "The share of inventory spaces occupied."},
    "target_capacity": {"label": "Target lot capacity"},
    "expected_capacity_used_p50": {"label": "Expected lot capacity used (P50)"},
    "conservative_capacity_used_p90": {"label": "Conservative lot capacity used (P90)"},
    "open_slots": {"label": "Expected available spaces"},
    "vehicles_arriving": {"label": "Vehicles arriving"},
    "vehicles_to_release": {"label": "Vehicles to sell or release"},
    "over_90": {"label": "Over 90 days on lot"},
    "below_break_even": {"label": "Priced below break-even"},
    "days_on_lot": {"label": "Days on lot"},
    "expected_ending_inventory_p50": {"label": "Expected ending inventory (P50)"},
    "conservative_ending_inventory_p90": {"label": "Conservative ending inventory (P90)"},
    # forecasts
    "units_sold_p50": {"label": "Expected vehicles sold (P50)"},
    "revenue_p50": {"label": "Expected revenue (P50)"},
    "cash_tied_up": {"label": "Cash tied up in inventory"},
    # promotion
    "target_likelihood": {"label": "Likelihood of reaching the target"},
    "required_reduction": {"label": "Vehicles to sell or release"},
    "dealer_funded": {"label": "Dealer-funded discount"},
    "gross_impact_p50": {"label": "Expected gross impact (P50)"},
    "total_front_end_gross_p50": {"label": "Expected total front-end gross (P50)"},
    "holding_savings_p50": {"label": "Expected holding-cost savings (P50)"},
    "depreciation_savings_p50": {"label": "Expected depreciation savings (P50)"},
    "approvals_required": {"label": "Manager reviews required"},
}


def metric(key: str) -> str:
    return METRIC.get(key, {}).get("label", _humanize(key))


def metric_def(key: str) -> str:
    return METRIC.get(key, {}).get("def", "")


# --- glossary: "How to read these estimates" ------------------------------------------

GLOSSARY: list[tuple[str, str]] = [
    ("Expected estimate (P50)",
     "The median outcome across simulations — half of outcomes are sooner or lower, half later or higher."),
    ("Conservative estimate (P90)",
     "A cautious planning estimate. Most simulated outcomes fall within this value, but it is "
     "not a guarantee and not the worst possible case."),
    ("Downside estimate (P10)",
     "A lower-end economic outcome, used to understand downside risk — only a smaller share of "
     "outcomes is expected to be lower."),
    ("Days on lot", "How long the vehicle has been in inventory."),
    ("Expected days to sale", "Estimated additional time until the vehicle sells."),
    ("Break-even price",
     "The price where expected proceeds cover the configured vehicle cost basis and applicable costs."),
    ("Lowest safe asking price",
     "The lowest recommended asking price before crossing a configured financial or approval boundary."),
    ("Total economic value",
     "Front-end gross adjusted for the expected aging-related costs the model represents."),
    ("Lot capacity used",
     "The share of available inventory spaces currently or expected to be occupied."),
    ("Human approval", "A recommendation that requires review before action."),
]

GLOSSARY_TITLE = "How to read these estimates"


# --- audit-only helper ----------------------------------------------------------------


def audit_label(raw: str) -> str:
    """Title-case a snake_case key for an audit table — the only place raw keys surface."""
    mapping = {
        "request_id": "Request ID",
        "simulation_id": "Simulation ID",
        "workflow_id": "Workflow ID",
        "assumption_version": "Assumption version",
        "config_version": "Config version",
        "model_version": "Model version",
        "percentile_convention": "Percentile convention",
    }
    return mapping.get(raw, raw.replace("_", " ").capitalize())


def _humanize(code: str) -> str:
    return str(code).replace("_", " ").capitalize()
