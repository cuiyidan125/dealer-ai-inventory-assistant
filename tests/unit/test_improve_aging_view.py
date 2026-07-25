"""Improve Aging workspace presentation. Phase 5.1.

The polish is presentation-only, so these tests hold two lines at once: the new copy and
layout are grounded in the workflow result (labels, next-steps, exec metrics all trace to a
field), and **nothing the workflow decided has changed** — same selected and excluded
vehicles, same recommended plan, same numbers — because the view computes nothing.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pricing_agent.agents import run_assistant
from pricing_agent.agents.assistant import AssistantState
from pricing_agent.mcp_clients import MockTransport
from pricing_agent.views import improve_aging as view
from pricing_agent.views import improve_aging_copy as copy
from pricing_agent.workflows.improve_aging import (
    ImproveAgingRequest,
    WorkflowState,
    run_improve_aging,
)

VIEWS = Path(__file__).resolve().parents[2] / "src" / "pricing_agent" / "views"
AS_OF = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)

SUMMER = ImproveAgingRequest(
    target_utilization=0.70, event_requested=True, event_id="EVT-SUMMER-2026",
    event_name="Summer Clearance", available_events=("Summer Clearance", "Labor Day Sales Event"))

# The Phase 5 baseline this polish must not disturb.
BASELINE_SELECTED = ["V-10005", "V-10002", "V-10012", "V-10006", "V-10019", "V-10004", "V-10013",
                     "V-10014", "V-10015", "V-10016", "V-10017", "V-10020", "V-10008", "V-10001"]
BASELINE_EXCLUDED = ["V-10003", "V-10007", "V-10009", "V-10010", "V-10011", "V-10018"]
BASELINE_PLAN = "CAPACITY_FIRST"


@pytest.fixture(scope="module")
def result():
    return run_improve_aging(MockTransport(as_of=AS_OF), SUMMER)


@pytest.fixture(scope="module")
def no_event():
    return run_improve_aging(MockTransport(as_of=AS_OF), ImproveAgingRequest(target_utilization=0.70))


@pytest.fixture(scope="module")
def not_achievable():
    """A tighter 60% target that remains unreachable inside the new 2026-08-17 event window,
    kept as a probe so the TARGET_NOT_ACHIEVABLE copy and gap machinery stay covered. The
    canonical 70% demo now resolves to ACHIEVABLE_WITH_MARGIN_COST on the larger lot; this probe
    does not change that demo, it only exercises the not-achievable state."""
    return run_improve_aging(MockTransport(as_of=AS_OF), ImproveAgingRequest(
        target_utilization=0.55, event_requested=True, event_id="EVT-SUMMER-2026",
        event_name="Summer Clearance", available_events=SUMMER.available_events))


# --- 1. executive-summary values come from the result --------------------------------


def test_executive_metrics_come_straight_from_the_result(result):
    m = view.executive_metrics(result)
    d = result.portfolio_summary
    assert m["current_utilization"] == d["current_utilization"]
    assert m["target_utilization"] == d["target_utilization"]
    assert m["required_unit_reduction"] == d["required_unit_reduction"]
    assert m["candidate_count"] == len(result.selection.candidates)
    assert m["target_status"] == result.promotion_result["feasibility"]["status"]
    assert m["probability_target_achieved"] == d["probability_target_achieved"]


# --- 2. TARGET_NOT_ACHIEVABLE copy reflects the actual gap ----------------------------


def test_canonical_demo_is_achievable_with_margin_cost(result):
    """On the 20-unit lot the 70% target inside the Summer Clearance window is reachable, but
    only at a gross-margin cost — ACHIEVABLE_WITH_MARGIN_COST — so the three plans trade off on
    total front-end gross: profit-protection keeps the most gross, freeing space costs the most."""
    assert result.state is WorkflowState.ROUTED_AND_EXECUTED
    assert result.promotion_result["feasibility"]["status"] == "ACHIEVABLE_WITH_MARGIN_COST"
    assert result.portfolio_summary["required_unit_reduction"] == 1


def test_target_not_achievable_recommendation_and_gap(not_achievable):
    assert not_achievable.state is WorkflowState.TARGET_NOT_ACHIEVABLE
    statement = copy.recommendation_statement(not_achievable)
    assert "not achievable" in statement.lower()
    # The gap the view shows is required minus what the safe plan can release, from the result.
    required = not_achievable.portfolio_summary["required_unit_reduction"]
    achievable = not_achievable.promotion_result["feasibility"]["p50_achievable_incremental_units"]
    assert required == 4 and achievable == 2.0       # 55% probe on the 20-unit lot
    assert (required - achievable) == 2              # the gap the copy narrates


def test_gap_reasons_are_only_those_the_result_supports(not_achievable):
    reasons = view._gap_reasons(not_achievable)
    joined = " ".join(reasons).lower()
    # Present in this result:
    assert "recently-acquired" in joined            # V-10007, V-10010
    assert "campaign" in joined                     # V-10011
    assert "price floor" in joined                  # below-break-even approvals
    # Absent reasons must not be invented — there is no low-engagement signal in this result.
    assert "engagement" not in joined


# --- 3. actions map only from existing result fields ---------------------------------


def test_next_steps_are_grounded_and_counted(result):
    steps = copy.next_steps(result)
    assert 3 <= len(steps) <= 5
    counts: dict[str, int] = {}
    for a in result.consolidated_actions:
        counts[a["recommended_action"]] = counts.get(a["recommended_action"], 0) + 1
    titles = " ".join(s.title for s in steps)
    assert str(counts.get("MANAGER_REVIEW", 0)) in titles
    assert str(counts.get("WHOLESALE_OR_LOSS_MINIMIZATION_REVIEW", 0)) in titles
    for step in steps:
        assert step.grounded_in         # every action names its source


def test_next_steps_omit_plan_action_when_no_event(no_event):
    steps = copy.next_steps(no_event)
    joined = " ".join(s.title.lower() for s in steps)
    assert "approve the" not in joined      # no promotion plan exists without an event


# --- 4 & 5. selected / excluded vehicles unchanged -----------------------------------


def test_selected_vehicle_ids_unchanged(result):
    assert list(result.selection.candidate_ids) == BASELINE_SELECTED


def test_excluded_vehicle_ids_unchanged(result):
    assert [e.vehicle_id for e in result.selection.exclusions] == BASELINE_EXCLUDED


# --- 6. recommended plan unchanged ---------------------------------------------------


def test_recommended_plan_unchanged(result):
    assert result.promotion_result["recommended_plan"]["plan_type"] == BASELINE_PLAN


# --- 7. the view and copy layers do no calculation -----------------------------------


@pytest.mark.parametrize("name", ["improve_aging.py", "improve_aging_copy.py"])
def test_presentation_layer_does_no_calculation(name):
    tree = ast.parse((VIEWS / name).read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for mod in imported:
        assert not mod.startswith(("pricing_agent.domain", "pricing_agent.simulation")), \
            f"{name} imports the calculation layer: {mod}"
    source = (VIEWS / name).read_text(encoding="utf-8")
    for banned in ("np.percentile", ".percentile(", "np.mean", "simulate("):
        assert banned not in source, f"{name} references {banned}"


# --- 8. raw reason codes map to dealer-friendly labels -------------------------------


def test_reason_codes_map_to_friendly_labels():
    assert copy.selection_label("CURRENTLY_OVER_90_DAYS") == "Already over 90 days on lot"
    assert copy.exclusion_label("RECENTLY_ACQUIRED") == "Recently acquired — protect the current strategy"
    assert copy.exclusion_label("ALREADY_ASSIGNED_TO_CAMPAIGN") == "Already included in another campaign"
    # Unknown codes degrade to a readable title, never a crash.
    assert copy.selection_label("SOME_NEW_CODE") == "Some New Code"


def test_exclusion_categories_are_classified():
    assert copy.exclusion_category(("INSUFFICIENT_DATA",)) == copy.DATA_LIMITATION
    assert copy.exclusion_category(("NO_SAFE_DISCOUNT_HEADROOM",)) == copy.SAFETY_RULE
    assert copy.exclusion_category(("RECENTLY_ACQUIRED",)) == copy.BUSINESS_RULE


def test_every_reason_code_in_the_result_has_a_label(result):
    for c in result.selection.candidates:
        for code in c.reason_codes:
            assert copy.selection_label(code) != code or code in copy.SELECTION_LABELS
    for e in result.selection.exclusions:
        for code in e.reason_codes:
            assert code in copy.EXCLUSION_LABELS, f"no label for exclusion code {code}"


# --- 9. exactly five business steps --------------------------------------------------


def test_default_summary_has_exactly_five_business_steps():
    assert len(view.BUSINESS_STEPS) == 5
    labels = [label for label, _, _ in view.BUSINESS_STEPS]
    assert labels == [
        "Review the lot", "Identify vehicles requiring action", "Evaluate pricing options",
        "Build the sale-event plan", "Create the dealer action plan",
    ]


def test_business_steps_map_to_real_trace_step_names(result):
    trace_names = {t.step_name for t in result.trace}
    for _, step_name, _ in view.BUSINESS_STEPS:
        assert step_name in trace_names


# --- 10. full trace remains accessible -----------------------------------------------


def test_full_trace_is_preserved(result):
    # The engine's trace is untouched — every step with its ids and timestamps is available.
    assert len(result.trace) >= 11          # 1 portfolio + 1 select + 7 vehicles + 1 promo + 1 consolidate
    ok = [t for t in result.trace if t.skill_called and t.status == "OK"]
    assert all(t.request_id and t.simulation_id for t in ok)
    assert all(t.start_timestamp and t.end_timestamp for t in result.trace)
    # The view renders the full trace in an expander, preserving the audit columns.
    source = (VIEWS / "improve_aging.py").read_text(encoding="utf-8")
    assert "View full workflow execution trace" in source
    assert "simulation_id" in source and "request_id" in source


# --- 11. assistant summary links to the workspace ------------------------------------


def test_assistant_summary_links_to_the_workspace():
    response = run_assistant(
        "reduce my inventory utilization to 70% during the Summer Clearance event", as_of=AS_OF)
    assert response.workflow.value == "IMPROVE_AGING_INVENTORY"
    assert response.target_url == "improve-aging-inventory"
    s = response.summary
    for key in ("current_utilization", "target_utilization", "recommended_plan",
                "candidate_count", "approvals_required", "target_status"):
        assert key in s


# --- 12. no price publishing introduced ----------------------------------------------


@pytest.mark.parametrize("name", ["improve_aging.py", "improve_aging_copy.py"])
def test_no_price_publishing_introduced(name):
    source = (VIEWS / name).read_text(encoding="utf-8")
    for banned in ("publish_vehicle_price", "save_pricing_decision", "write_client", "WriteClient"):
        assert banned not in source, f"{name} references {banned}"


# --- disclosure copy ------------------------------------------------------------------


# --- count reconciliation (7 analysed vs 5 shown; 17 records vs 5 vehicles) -----------


def test_reconciled_counts_no_event_shows_all_analysed(no_event):
    """Without an event, 8 vehicles are analysed but two (V-10013, V-10014) carry no immediate
    action. The reconciliation surfaces all 8 and names the two, instead of silently hiding them."""
    rc = view.reconciled_counts(no_event)
    assert rc["analysed"] == 8
    assert rc["immediate_action"] == 6
    assert rc["no_immediate_action"] == 2
    assert set(rc["no_immediate_ids"]) == {"V-10013", "V-10014"}
    # Invariant the workspace relies on: the two buckets partition the analysed set.
    assert rc["immediate_action"] + rc["no_immediate_action"] == rc["analysed"]
    assert set(rc["immediate_ids"]).isdisjoint(rc["no_immediate_ids"])


def test_reconciled_review_counts_distinguish_vehicles_from_records(no_event):
    """The '20' is a count of approval records across 6 vehicles — the reconciliation reports both
    so the metric stops implying 20 separate manager reviews."""
    rc = view.reconciled_counts(no_event)
    assert rc["review_items"] == len(no_event.approvals_required) == 20
    assert rc["review_vehicles"] == 6
    assert rc["review_vehicles"] == len(rc["review_vehicle_ids"])
    # Every review vehicle is one the workflow already flagged with a non-empty approvals list.
    flagged = {a["vehicle_id"] for a in no_event.consolidated_actions if a["approvals_required"]}
    assert set(rc["review_vehicle_ids"]) == flagged


def test_reconciled_counts_with_event_stay_partitioned(result):
    """With the Summer Clearance event the reconciliation invariants still hold: the analysed set
    partitions cleanly into immediate and no-immediate, and review items reconcile to the records.
    (The promoted units are lower-ranked candidates beyond the deep-analysis cap, so the two
    hold-gross analysed vehicles remain no-immediate here.)"""
    rc = view.reconciled_counts(result)
    assert rc["analysed"] == 8
    assert rc["immediate_action"] + rc["no_immediate_action"] == rc["analysed"]
    assert rc["review_items"] == len(result.approvals_required)


def test_reconciliation_changes_no_classification(no_event):
    """The reconciliation must not move any vehicle between action buckets — the per-vehicle
    recommended_action the workflow produced is unchanged."""
    actions = {a["vehicle_id"]: a["recommended_action"] for a in no_event.consolidated_actions}
    assert actions["V-10013"] == "NO_ACTION"
    assert actions["V-10014"] == "NO_ACTION"
    # The immediate-action vehicles keep their action labels.
    assert actions["V-10005"] == "WHOLESALE_OR_LOSS_MINIMIZATION_REVIEW"
    assert actions["V-10002"] == "MANAGER_REVIEW"


def test_assistant_summary_carries_reconciled_counts():
    response = run_assistant("Which aging vehicles should I promote?", as_of=AS_OF)
    s = response.summary
    assert s["deep_analysed_count"] == 8
    assert s["immediate_action_count"] == 6
    assert s["no_immediate_action_count"] == 2
    assert s["review_vehicle_count"] == 6
    assert s["review_item_count"] == 20
    # The raw approval-record count is still available and unchanged.
    assert s["approvals_required"] == 20


def test_disclosure_states_the_four_safeguards():
    source = (VIEWS / "improve_aging.py").read_text(encoding="utf-8")
    lowered = source.lower()
    assert "synthetic" in lowered or "mocked" in lowered
    assert "prototype simulation" in lowered
    assert "review" in lowered
    assert "no price" in lowered
