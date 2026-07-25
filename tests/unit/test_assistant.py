"""The assistant orchestrator: route → resolve → execute one skill. Phase 4.

These tests cover the six response states and the boundaries that make the assistant safe
to connect to real skills: exactly one skill runs, the skill's result is preserved
untouched, every summary number is copied from that result rather than invented, no model is
ever called, and no price is ever published.

The demo request — "What should I price 2020 Ford F-150 XLT?" — is exercised end to end.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pricing_agent.agents import assistant as assistant_mod
from pricing_agent.agents.assistant import (
    AssistantState,
    parse_target_utilization,
    resolve_event,
    run_assistant,
)
from pricing_agent.workflows.context import WorkflowContext

AGENTS = Path(__file__).resolve().parents[2] / "src" / "pricing_agent" / "agents"
AS_OF = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)

DEMO_PROMPT = "What should I price 2020 Ford F-150 XLT?"


def run(text: str):
    return run_assistant(text, as_of=AS_OF)


# --- the six states -------------------------------------------------------------------


def test_successful_pricing_is_routed_and_executed():
    response = run(DEMO_PROMPT)
    assert response.state is AssistantState.ROUTED_AND_EXECUTED
    assert response.workflow is WorkflowContext.PRICE_INVENTORY
    assert response.skill == "single-vehicle-valuation"
    assert response.resolved_vehicle_id == "V-10003"


def test_missing_vehicle_needs_clarification():
    response = run("What should I price this vehicle?")
    assert response.state is AssistantState.NEEDS_CLARIFICATION
    assert "which vehicle" in response.message.lower()


def test_no_matching_vehicle():
    response = run("What should I price a 2019 Tesla Model 3?")
    assert response.state is AssistantState.NO_MATCH
    assert "inventory" in response.message.lower()
    assert response.resolved_vehicle_id is None


def test_ambiguous_vehicle():
    response = run("What should I price the 2022 Toyota RAV4 XLE?")
    assert response.state is AssistantState.AMBIGUOUS_MATCH
    assert {c["vehicle_id"] for c in response.candidates} == {"V-10001", "V-10007"}


def test_partial_make_surfaces_candidates_instead_of_forcing_exact_input():
    # A make alone lists the matching vehicles for the dealer to pick from — no full
    # year/make/model/trim required. (The view renders these as clickable "Analyze this one".)
    response = run("price toyota")
    assert response.state is AssistantState.AMBIGUOUS_MATCH
    makes = {c["make"] for c in response.candidates}
    assert makes == {"Toyota"} and len(response.candidates) >= 3


def test_partial_model_surfaces_candidates():
    response = run("price the RAV4")
    assert response.state is AssistantState.AMBIGUOUS_MATCH
    assert {c["vehicle_id"] for c in response.candidates} == {"V-10001", "V-10007"}


def test_partial_make_that_is_unique_prices_directly():
    # Only one Ford on the lot, so a make alone resolves straight to it.
    response = run("price a Ford")
    assert response.state is AssistantState.ROUTED_AND_EXECUTED
    assert response.resolved_vehicle_id == "V-10003"


def test_improve_aging_now_executes():
    """Phase 5: the aging orchestration is connected. It runs the portfolio diagnosis and
    single-vehicle analysis for selected candidates."""
    response = run("Which aging vehicles should I promote?")
    assert response.state is AssistantState.ROUTED_AND_EXECUTED
    assert response.workflow is WorkflowContext.IMPROVE_AGING_INVENTORY
    assert response.summary["candidate_count"] > 0
    assert response.improve_aging is not None


def test_unclassifiable_request_needs_clarification():
    response = run("hello there")
    assert response.state is AssistantState.NEEDS_CLARIFICATION
    assert response.workflow is None


def test_execution_error_is_a_state_not_a_crash(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("skill blew up")

    monkeypatch.setattr(assistant_mod.single_vehicle, "analyze", boom)
    response = run(DEMO_PROMPT)
    assert response.state is AssistantState.EXECUTION_ERROR
    assert response.workflow is WorkflowContext.PRICE_INVENTORY


# --- portfolio and promotion ----------------------------------------------------------


def test_portfolio_request_executes():
    response = run("What will my inventory look like in the next 30 days?")
    assert response.state is AssistantState.ROUTED_AND_EXECUTED
    assert response.workflow is WorkflowContext.ACQUIRE_INVENTORY
    assert response.summary["units_on_lot"] == 20


def test_promotion_with_named_event_executes():
    response = run("Plan the Summer Clearance event to reach 70% utilization.")
    assert response.state is AssistantState.ROUTED_AND_EXECUTED
    assert response.workflow is WorkflowContext.MERCHANDISE_INVENTORY
    assert response.summary["event_name"] == "Summer Clearance"


def test_promotion_without_a_calendar_event_asks_which_one():
    response = run("Create a July 4th promotion plan.")
    assert response.state is AssistantState.NEEDS_CLARIFICATION
    assert "event" in response.message.lower()


def test_parse_target_utilization():
    assert parse_target_utilization("reach 70% utilization") == pytest.approx(0.70)
    assert parse_target_utilization("hit 85 percent") == pytest.approx(0.85)
    assert parse_target_utilization("no number here") == pytest.approx(0.70)  # default


def test_resolve_event_matches_by_name_and_by_holiday_window():
    events = [
        {"event_id": "EVT-SUMMER-2026", "event_name": "Summer Clearance",
         "start_date": "2026-08-17", "end_date": "2026-08-21"},
        {"event_id": "EVT-LABOR-2026", "event_name": "Labor Day Sales Event",
         "start_date": "2026-09-04", "end_date": "2026-09-07"},
    ]
    assert resolve_event("plan the summer clearance", events)["event_id"] == "EVT-SUMMER-2026"
    assert resolve_event("labor day event", events)["event_id"] == "EVT-LABOR-2026"
    # July 4th falls in no window, so it does not silently become the late-July sale.
    assert resolve_event("july 4th sale", events) is None


# --- exactly one skill runs -----------------------------------------------------------


def test_pricing_invokes_the_single_vehicle_skill_exactly_once(monkeypatch):
    calls: list[tuple] = []
    real = assistant_mod.single_vehicle.analyze

    def counting(vehicle_id, transport, **kwargs):
        calls.append((vehicle_id,))
        return real(vehicle_id, transport, **kwargs)

    monkeypatch.setattr(assistant_mod.single_vehicle, "analyze", counting)
    run(DEMO_PROMPT)
    assert len(calls) == 1


def test_a_single_request_touches_only_its_own_skill(monkeypatch):
    portfolio_calls, promo_calls = [], []
    monkeypatch.setattr(
        assistant_mod.inventory_portfolio, "analyze",
        lambda *a, **k: portfolio_calls.append(1) or {},
    )
    monkeypatch.setattr(
        assistant_mod.promotion_planner, "plan_event",
        lambda *a, **k: promo_calls.append(1) or {},
    )
    run(DEMO_PROMPT)  # a pricing request
    assert portfolio_calls == [] and promo_calls == []


# --- the orchestrator generates no numbers --------------------------------------------


def test_every_summary_number_comes_from_the_skill_result():
    response = run(DEMO_PROMPT)
    result = response.result
    scenario = next(
        s for s in result["pricing_scenarios"]
        if s["strategy"] == result["recommended_strategy"]["strategy"]
    )
    s = response.summary
    assert s["current_list_price"] == result["vehicle"]["current_list_price"]
    assert s["recommended_price"] == scenario["proposed_list_price"]
    assert s["p50_days_to_sale"] == scenario["additional_days_to_sale"]["p50"]
    assert s["p90_days_to_sale"] == scenario["additional_days_to_sale"]["p90"]
    assert s["break_even_price"] == result["break_even_analysis"]["current_accounting_break_even"]
    assert s["promotional_headroom"] == result["promotional_headroom"]["max_safe_discount"]


def test_the_skill_result_is_preserved_intact():
    response = run(DEMO_PROMPT)
    # The response carries the schema-valid skill result untouched, not a reshaped copy.
    assert response.result is not None
    assert "pricing_scenarios" in response.result
    assert "break_even_analysis" in response.result
    assert "audit" in response.result or "explanation_inputs" in response.result


def test_top_warnings_are_a_subset_of_the_result_warnings():
    response = run(DEMO_PROMPT)
    result_codes = {w["code"] for w in response.result["warnings"]}
    for warning in response.warnings:
        assert warning["code"] in result_codes
    assert len(response.warnings) <= 3


# --- no model, no publishing ----------------------------------------------------------


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.append(node.module)
    return names


@pytest.mark.parametrize("module", ["router.py", "resolver.py", "assistant.py"])
def test_routing_layer_imports_no_model(module):
    for name in _imports(AGENTS / module):
        assert not name.startswith(("anthropic", "openai", "pricing_agent.llm")), (
            f"{module} imports {name}; Phase 4 routing is deterministic"
        )


def test_no_llm_is_called_even_when_one_is_available(monkeypatch):
    """Belt and braces: make any model call explode, and the pricing path still executes."""
    import pricing_agent.llm.client as client

    monkeypatch.setattr(
        client, "complete",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("the assistant called a model")),
    )
    assert run(DEMO_PROMPT).state is AssistantState.ROUTED_AND_EXECUTED


def test_orchestrator_never_touches_the_write_path():
    source = (AGENTS / "assistant.py").read_text(encoding="utf-8")
    for banned in ("publish_vehicle_price", "save_pricing_decision", "write_client", "WriteClient"):
        assert banned not in source, f"the orchestrator must not reference {banned}"


# --- the demo request, end to end -----------------------------------------------------


def test_demo_request_resolves_to_an_existing_fixture_vehicle():
    """Guards the demo: the exact prompt in the UI must resolve, price, and summarise."""
    response = run(DEMO_PROMPT)
    assert response.state is AssistantState.ROUTED_AND_EXECUTED
    assert response.resolved_vehicle_id == "V-10003"
    assert response.summary["vehicle"] == "2020 Ford F-150 XLT"
    assert response.summary["current_list_price"] > 0
    assert response.target_url == "price-inventory"
