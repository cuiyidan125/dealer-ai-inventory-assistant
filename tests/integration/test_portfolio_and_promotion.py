"""PF and PR scenarios from tests/scenarios/, executed against the real skills."""

from __future__ import annotations

import pytest

from pricing_agent.mcp_clients import FixtureStore, MockTransport, Mutation
from pricing_agent.skills.inventory_portfolio import analyze as analyze_portfolio
from pricing_agent.skills.promotion_planner import plan_event
from tests.conftest import DEFAULT_AS_OF


def _transport(mutations=()):
    parsed = [m if isinstance(m, Mutation) else Mutation(**m) for m in mutations]
    return MockTransport(as_of=DEFAULT_AS_OF, store=FixtureStore(mutations=parsed))


def _codes(result: dict) -> set[str]:
    return {w["code"] for w in result["warnings"]}


@pytest.fixture(scope="module")
def portfolio_suite(scenarios):
    return scenarios("portfolio")


@pytest.fixture(scope="module")
def promotion_suite(scenarios):
    return scenarios("promotion")


@pytest.fixture(scope="module")
def baseline_portfolio():
    return analyze_portfolio(_transport(), revenue_target_one_month=150_000)


# --- portfolio ------------------------------------------------------------------------


def test_portfolio_result_is_schema_valid(baseline_portfolio, validator_for):
    errors = list(
        validator_for("inventory-portfolio-result.schema.json").iter_errors(baseline_portfolio)
    )
    assert not errors, errors[:2]


def test_pf02_current_utilization_is_over_target(baseline_portfolio):
    capacity = baseline_portfolio["capacity_position"]
    assert capacity["current_utilization"] > capacity["target_utilization"]


def test_projected_capacity_warning_keys_on_the_projection_not_today(baseline_portfolio):
    """PROJECTED_CAPACITY_OVER_TARGET checks the *forecast*, not the lot as it stands.

    Under run-off — the prototype's default, since no tool supplies planned acquisitions
    — the lot empties, so projected utilization falls below target even when today's is
    above it. The warning therefore stays silent here, correctly but counter-intuitively.
    A production version with acquisition data would exercise this path properly; see
    docs/open-questions.md C1.
    """
    projected = baseline_portfolio["one_month_forecast"]["ending_utilization"]["p50"]
    target = baseline_portfolio["capacity_position"]["target_utilization"]

    fired = "PROJECTED_CAPACITY_OVER_TARGET" in _codes(baseline_portfolio)
    assert fired == (projected > target)


def test_pf02_reserved_slots_are_not_double_counted(baseline_portfolio):
    """D6: reserved_slots is a superset of confirmed_inbound. Counting both would inflate
    every downstream capacity figure."""
    capacity = baseline_portfolio["capacity_position"]
    assert capacity["reserved_slots"] >= capacity["confirmed_inbound"]
    assert (
        capacity["reserved_not_inbound"]
        == capacity["reserved_slots"] - capacity["confirmed_inbound"]
    )


def test_pf04_aging_buckets_account_for_every_vehicle(baseline_portfolio):
    buckets = baseline_portfolio["aging_profile"]["buckets"]
    counted = sum(b["unit_count"] for b in buckets)
    assert counted == baseline_portfolio["inventory_summary"]["active_count"]


def test_pf05_revenue_target_risk_is_reported(baseline_portfolio):
    risk = baseline_portfolio["one_month_forecast"]["risk_probabilities"]["revenue_below_target"]
    assert risk is not None and 0.0 <= risk <= 1.0


def test_pf06_run_off_forecast_is_labelled_a_lower_bound(baseline_portfolio):
    """§14.6. Run-off is the default path, not an edge case: no tool supplies planned
    acquisitions. A manager reading a falling utilization must be told it is a floor."""
    for horizon in ("one_month_forecast", "three_month_forecast"):
        basis = baseline_portfolio[horizon]["forecast_basis"]
        assert basis["mode"] == "RUN_OFF"
        assert basis["includes_expected_acquisitions"] is False
        assert basis["lower_bound_note"]
    assert "FUTURE_ACQUISITION_DATA_UNAVAILABLE" in _codes(baseline_portfolio)


def test_pf08_below_break_even_exposure_is_quantified(baseline_portfolio):
    risk = baseline_portfolio["financial_risk"]
    assert risk["units_below_break_even"] >= 1
    assert risk["total_exposure_below_break_even"] > 0


def test_pf09_missing_cost_basis_is_counted_not_dropped(validator_for):
    result = analyze_portfolio(
        _transport(
            [
                {"op": "remove", "path": "cost_basis.V-10003"},
                {"op": "remove", "path": "cost_basis.V-10007"},
                {"op": "remove", "path": "cost_basis.V-10009"},
                {"op": "remove", "path": "cost_basis.V-10010"},
                {"op": "remove", "path": "cost_basis.V-10011"},
            ]
        )
    )
    coverage = result["data_coverage"]
    # On the 20-car lot five removals drop coverage to 0.75, below the 0.80 confidence floor.
    assert coverage["missing_cost_basis"] == 5
    assert coverage["vehicles_analyzed"] == coverage["vehicles_requested"] - 5
    assert coverage["coverage_ratio"] < 0.8
    assert {"LOW_PORTFOLIO_FORECAST_CONFIDENCE", "INCOMPLETE_INVENTORY_DATA"} <= _codes(result)


def test_pf10_every_forecast_figure_shares_one_simulation(baseline_portfolio):
    """§12.5: figures may be combined only when they came from the same draws."""
    forecast = baseline_portfolio["one_month_forecast"]
    ids = {
        forecast[field]["simulation_id"]
        for field in (
            "unit_sales", "sales_revenue", "front_end_gross",
            "net_economic_value", "ending_inventory", "ending_utilization",
        )
    }
    assert len(ids) == 1


# --- promotion ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def summer_plan():
    return plan_event(_transport(), "EVT-SUMMER-2026", 0.70)


@pytest.fixture(scope="module")
def labor_plan():
    return plan_event(_transport(), "EVT-LABOR-2026", 0.75)


def test_promotion_result_is_schema_valid(summer_plan, validator_for):
    errors = list(
        validator_for("promotion-plan-result.schema.json").iter_errors(summer_plan)
    )
    assert not errors, errors[:2]


def test_pr02_unreachable_target_returns_quantified_alternatives():
    """Returning 'not achievable' without them leaves the manager where they started.

    On the 20-car lot the canonical 70% target is achievable, so a much tighter 55% target
    (requiring far more sales than the window allows) exercises the unreachable path.
    """
    plan = plan_event(_transport(), "EVT-SUMMER-2026", 0.55)
    feasibility = plan["feasibility"]
    assert feasibility["status"] in ("AT_RISK", "NOT_ACHIEVABLE")
    assert feasibility["alternatives"]
    for alternative in feasibility["alternatives"]:
        assert alternative["quantified_change"] > 0
        assert alternative["unit"]


def test_pr04_validated_lift_is_used_when_available(summer_plan):
    assert summer_plan["feasibility"]["lift_source"] == "HISTORICAL"
    assert "LOW_EXPECTED_EVENT_LIFT" not in _codes(summer_plan)


def test_pr05_missing_lift_falls_back_and_warns(labor_plan):
    """§26.3 requires an event without validated lift."""
    assert labor_plan["feasibility"]["lift_source"] == "CONFIG_DEFAULT"
    assert "LOW_EXPECTED_EVENT_LIFT" in _codes(labor_plan)


def test_exclusions_always_carry_a_reason(summer_plan):
    """A plan is not reviewable if the reader cannot see what was left out."""
    assert summer_plan["excluded_vehicles"]
    for candidate in summer_plan["excluded_vehicles"]:
        assert candidate["eligible"] is False
        assert candidate["exclusion_reason"]


def test_no_promotion_price_breaches_its_floor(summer_plan):
    """The §19.1 headroom bar, checked on every selected vehicle in every plan."""
    for plan in summer_plan["plans"]:
        for vehicle in plan["vehicles_selected"]:
            assert vehicle["promotion_price"] >= vehicle["minimum_safe_list_price"] - 0.01, (
                f"{vehicle['vehicle_id']} in {plan['plan_type']} priced below its floor"
            )


def test_pr11_vehicles_likely_to_sell_before_the_event_are_excluded(labor_plan):
    """Discounting a car that would have sold at full price is a pure gross giveaway."""
    reasons = {c["exclusion_reason"] for c in labor_plan["excluded_vehicles"]}
    assert "LIKELY_TO_SELL_BEFORE_EVENT" in reasons


def test_pr12_plans_and_baseline_share_a_seed(summer_plan):
    """Without a shared seed an empty plan would show nonzero incremental units from
    sampling noise alone."""
    seeds = {plan["simulation"]["seed"] for plan in summer_plan["plans"]}
    assert len(seeds) == 1

    margin_protect = next(
        p for p in summer_plan["plans"] if p["plan_type"] == "MARGIN_PROTECT"
    )
    if margin_protect["totals"]["vehicle_count"] == 0:
        assert margin_protect["outcomes"]["incremental_units_sold"]["mean"] == 0.0


def test_target_uses_physical_slots_not_effective_capacity(summer_plan):
    """D6: '70% utilization' means 70% of the lot, which is what the manager said."""
    target = summer_plan["inventory_target_calculation"]
    assert target["target_ending_inventory"] == int(
        target["total_physical_slots"] * target["target_utilization"]
    )
