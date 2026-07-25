"""Improve Aging Inventory orchestration. Phase 5.

Fifteen proofs that the workflow coordinates the three skills without becoming a fourth: it
runs them in order, reuses their results without recalculating, keeps their simulations
separate, degrades to a partial result on failure, and never publishes a price. The demo
scenario (Summer Clearance, 70%, injected clock) is exercised end to end.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pricing_agent.agents import run_assistant
from pricing_agent.agents.assistant import AssistantState
from pricing_agent.mcp_clients import MockTransport
from pricing_agent.workflows import improve_aging as workflow_mod
from pricing_agent.workflows.improve_aging import (
    ImproveAgingRequest,
    WorkflowState,
    run_improve_aging,
)

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / "src" / "pricing_agent" / "workflows"
AS_OF = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)

SUMMER = dict(
    target_utilization=0.70, event_requested=True, event_id="EVT-SUMMER-2026",
    event_name="Summer Clearance", available_events=("Summer Clearance", "Labor Day Sales Event"),
)


def run(**kwargs):
    return run_improve_aging(MockTransport(as_of=AS_OF), ImproveAgingRequest(**kwargs))


# --- 1. portfolio first and exactly once ----------------------------------------------


def test_portfolio_runs_first_and_exactly_once(monkeypatch):
    calls = []
    real = workflow_mod.inventory_portfolio.analyze
    monkeypatch.setattr(workflow_mod.inventory_portfolio, "analyze",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    result = run(**SUMMER)
    assert calls == [1]
    assert result.execution_order[0] == "PORTFOLIO_FORECAST"
    assert result.skill_invocation_counts["inventory-portfolio-forecast"] == 1


# --- 2. candidate selection uses the portfolio result ---------------------------------


def test_candidate_selection_step_runs_after_portfolio():
    result = run(**SUMMER)
    order = list(result.execution_order)
    assert order.index("PORTFOLIO_FORECAST") < order.index("CANDIDATE_SELECTION")
    assert result.selection is not None and result.selection.candidates


# --- 3. single-vehicle runs only for selected candidates ------------------------------


def test_single_vehicle_runs_only_for_selected_candidates(monkeypatch):
    seen = []
    real = workflow_mod.single_vehicle.analyze
    monkeypatch.setattr(workflow_mod.single_vehicle, "analyze",
                        lambda vid, t, **k: (seen.append(vid), real(vid, t, **k))[1])
    result = run(**SUMMER)
    selected = set(result.selection.candidate_ids)
    assert set(seen) <= selected
    assert set(e.vehicle_id for e in result.vehicle_evidence) <= selected


# --- 4. promotion runs only when an event is resolved ---------------------------------


def test_promotion_runs_only_with_a_resolved_event(monkeypatch):
    calls = []
    real = workflow_mod.promotion_planner.plan_event
    monkeypatch.setattr(workflow_mod.promotion_planner, "plan_event",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    run(target_utilization=0.70)                        # no event
    assert calls == []
    run(**SUMMER)                                        # resolved event
    assert calls == [1]


# --- 5. July 4th does not resolve to Summer Clearance ---------------------------------


def test_unresolved_event_does_not_substitute_and_asks(monkeypatch):
    calls = []
    monkeypatch.setattr(workflow_mod.promotion_planner, "plan_event",
                        lambda *a, **k: calls.append(1))
    result = run(target_utilization=0.70, event_requested=True, event_id=None,
                 available_events=("Summer Clearance", "Labor Day Sales Event"))
    assert result.state is WorkflowState.NEEDS_CLARIFICATION
    assert calls == []                                   # promotion never ran
    assert "summer clearance" in result.message.lower()  # offered, not silently chosen


# --- 6. no numerical calculation duplicated -------------------------------------------


def test_workflow_modules_do_not_recalculate():
    for name in ("improve_aging.py", "candidate_selection.py"):
        tree = ast.parse((WORKFLOWS / name).read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        for mod in imported:
            assert not mod.startswith(
                ("pricing_agent.domain", "pricing_agent.simulation", "numpy", "pandas")
            ), f"{name} imports {mod}"
        # Target actual calculation calls, not the docstrings that describe the rule.
        source = (WORKFLOWS / name).read_text(encoding="utf-8")
        for banned in ("np.percentile", ".percentile(", "np.mean", "simulate("):
            assert banned not in source, f"{name} references {banned}"


# --- 7. request ids and simulation ids preserved --------------------------------------


def test_request_and_simulation_ids_preserved():
    result = run(**SUMMER)
    for ev in result.vehicle_evidence:
        assert ev.request_id and ev.simulation_id
        assert ev.simulation_id == ev.result["audit"]["simulation"]["simulation_id"]
    trace_sims = [t.simulation_id for t in result.trace if t.skill_called and t.status == "OK"]
    assert all(trace_sims)


# --- 8. percentiles from independent simulations are not summed -----------------------


def test_independent_simulations_are_kept_separate():
    result = run(**SUMMER)
    sims = {result.portfolio_result["audit"]["simulation"]["simulation_id"]}
    sims |= {e.simulation_id for e in result.vehicle_evidence}
    sims.add(result.promotion_result["audit"]["simulation"]["simulation_id"])
    # Several distinct simulations were involved…
    assert len(sims) >= 3
    # …and the joint outcome figures all come from a single one — the recommended plan's
    # simulation — and are never combined with the per-vehicle or portfolio simulations.
    outcome_sim = result.portfolio_summary["outcome_simulation_id"]
    recommended = result.promotion_result["recommended_plan"]["plan_type"]
    plan_sim = next(p["simulation"]["simulation_id"]
                    for p in result.promotion_result["plans"]
                    if p["plan_type"] == recommended)
    assert outcome_sim == plan_sim
    assert result.portfolio_summary["expected_ending_inventory"]["simulation_id"] == outcome_sim
    # The joint-outcome simulation is not any of the per-vehicle simulations.
    assert outcome_sim not in {e.simulation_id for e in result.vehicle_evidence}


def test_consolidation_source_does_not_sum_percentiles():
    """The consolidation reads whole result objects and does no arithmetic across them."""
    source = (WORKFLOWS / "improve_aging.py").read_text(encoding="utf-8")
    # No summation of p50/p90 fields across results anywhere in the engine.
    assert "p50" not in source or "sum(" not in source.split("def _consolidate")[1]


# --- 9. skill failure returns PARTIAL_RESULT ------------------------------------------


def test_single_vehicle_failure_returns_partial_result(monkeypatch):
    def boom(vid, transport, **k):
        raise RuntimeError("valuation failed")

    monkeypatch.setattr(workflow_mod.single_vehicle, "analyze", boom)
    result = run(**SUMMER)
    assert result.state is WorkflowState.PARTIAL_RESULT
    # Completed results are preserved; the failure is named, not fabricated over.
    assert result.portfolio_result is not None
    assert "some_vehicle_analyses" in result.unavailable
    assert any(t.status == "ERROR" for t in result.trace)


def test_portfolio_failure_returns_execution_error(monkeypatch):
    monkeypatch.setattr(workflow_mod.inventory_portfolio, "analyze",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    result = run(**SUMMER)
    assert result.state is WorkflowState.EXECUTION_ERROR


# --- 10. warnings and approvals preserved ---------------------------------------------


def test_warnings_and_approvals_are_preserved():
    result = run(**SUMMER)
    for ev in result.vehicle_evidence:
        assert ev.warnings == ev.result["warnings"]
        assert ev.approvals_required == ev.result["approvals_required"]
    # Approvals are collected and tagged with their source.
    for approval in result.approvals_required:
        assert approval["source"] in {"single-vehicle-valuation", "dealer-event-promotion-planner"}


# --- 11. no price publishing ----------------------------------------------------------


def test_workflow_never_touches_the_write_path():
    for name in ("improve_aging.py", "candidate_selection.py"):
        source = (WORKFLOWS / name).read_text(encoding="utf-8")
        for banned in ("publish_vehicle_price", "save_pricing_decision", "write_client", "WriteClient"):
            assert banned not in source, f"{name} references {banned}"


# --- 12. assistant executes end to end ------------------------------------------------


def test_assistant_executes_the_workflow_end_to_end():
    response = run_assistant(
        "reduce my inventory utilization to 70% during the Summer Clearance event",
        as_of=AS_OF,
    )
    assert response.workflow.value == "IMPROVE_AGING_INVENTORY"
    assert response.state in (AssistantState.ROUTED_AND_EXECUTED, AssistantState.TARGET_NOT_ACHIEVABLE)
    assert response.improve_aging is not None
    assert response.summary["candidate_count"] > 0
    assert response.target_url == "improve-aging-inventory"


def test_assistant_aging_diagnosis_without_event_executes():
    response = run_assistant("my lot is full of aging vehicles, what should I do?", as_of=AS_OF)
    assert response.state is AssistantState.ROUTED_AND_EXECUTED
    assert response.summary["target_status"] == "NO_EVENT"


# --- 13. missing information → NEEDS_CLARIFICATION -------------------------------------


def test_named_but_unresolvable_event_needs_clarification():
    response = run_assistant("reduce inventory utilization to 70% by the July 4th event", as_of=AS_OF)
    assert response.state is AssistantState.NEEDS_CLARIFICATION


# --- 14. unrealistic target → TARGET_NOT_ACHIEVABLE -----------------------------------


def test_unrealistic_target_returns_target_not_achievable():
    # On the 20-car lot the canonical 70% target is achievable, so a much tighter 55% target
    # (which would require selling far more units than the window allows) exercises the
    # not-achievable state. Selection is identical.
    result = run(**{**SUMMER, "target_utilization": 0.55})
    assert result.state is WorkflowState.TARGET_NOT_ACHIEVABLE
    assert result.promotion_result["feasibility"]["status"] == "NOT_ACHIEVABLE"
    # Full evidence is still present.
    assert result.vehicle_evidence and result.promotion_result


# --- 15. protected vehicles are not aggressively promoted -----------------------------


def test_protected_vehicles_are_not_promoted():
    result = run(**SUMMER)
    protected = {
        e.vehicle_id for e in result.selection.exclusions
        if any(r in result.selection.PROTECTED_REASONS for r in e.reason_codes)
    }
    assert protected, "the scenario must contain protected vehicles"
    # Not candidates, and not in the workflow's *effective* promotion (its authoritative
    # plan), even if the promotion skill's own eligibility would have included them.
    assert protected.isdisjoint(set(result.selection.candidate_ids))
    assert protected.isdisjoint(set(result.effective_promotion_ids))
    # No consolidated action promotes a protected vehicle.
    promoted_actions = {
        a["vehicle_id"] for a in result.consolidated_actions
        if a["recommended_action"] == "EVENT_PROMOTION"
    }
    assert protected.isdisjoint(promoted_actions)


def test_workflow_holds_back_protected_vehicles_the_skill_would_promote():
    """When the promotion skill's raw plan selects a protected vehicle, the workflow records
    the override rather than silently promoting it."""
    result = run(**SUMMER)
    raw = workflow_mod._promoted_ids(result.promotion_result)
    protected = {
        e.vehicle_id for e in result.selection.exclusions
        if any(r in result.selection.PROTECTED_REASONS for r in e.reason_codes)
    }
    # Any overlap must appear in held_from_promotion and be removed from the effective set.
    assert set(result.held_from_promotion) == (raw & protected)
    assert set(result.effective_promotion_ids) == (raw - protected)


# --- execution trace shape ------------------------------------------------------------


def test_execution_trace_is_ordered_and_complete():
    result = run(**SUMMER)
    steps = [t.step_number for t in result.trace]
    assert steps == sorted(steps)
    names = [t.step_name for t in result.trace]
    assert names[0] == "PORTFOLIO_FORECAST"
    assert "CONSOLIDATE" in names
    for t in result.trace:
        assert t.start_timestamp and t.end_timestamp and t.status


def test_no_event_run_marks_joint_outcomes_unavailable():
    result = run(target_utilization=0.70)
    assert result.state is WorkflowState.ROUTED_AND_EXECUTED
    assert result.portfolio_summary["expected_ending_inventory"] is None
    assert "joint_gross_impact" in result.unavailable
