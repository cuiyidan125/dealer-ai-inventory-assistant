"""Conversational Inventory-Portfolio (Acquire) follow-ups.

The 30-day outlook opens a conversation the dealer drills into without leaving the thread: current
lot health, the units needing attention, and what to review before acquiring. Every value is copied
from the stored `inventory_portfolio` result; this layer selects, sorts, parses N, and maps codes to
labels/icons — it computes nothing and never re-identifies the lot. A genuine cross-workflow request
(price a vehicle, free up space) still switches.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pricing_agent.agents import new_state, run_assistant
from pricing_agent.agents.conversation import SOURCE_FIRST_TURN
from pricing_agent.agents.followup import handle_followup

AGENTS = Path(__file__).resolve().parents[2] / "src" / "pricing_agent" / "agents"
AS_OF = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)

P0 = "What will my inventory look like in the next 30 days?"
P1 = "What does my lot look like today?"
P2 = "Show me the top 5 vehicles that need attention and why."
P3 = "Before I acquire more inventory, what should I review first?"
P5 = "Help me create a plan to free up space before I acquire more vehicles."


def _portfolio_state():
    r = run_assistant(P0, as_of=AS_OF)
    s = new_state()
    s.add_user("x")
    s.add_assistant(r.message, SOURCE_FIRST_TURN, result=r.result, response=r)
    s.adopt_response(r)
    return s, r


@pytest.fixture()
def convo():
    return _portfolio_state()


# --- 1. entry opens as outlook --------------------------------------------------------


def test_first_acquire_result_opens_as_outlook(convo):
    s, r = convo
    assert r.workflow.name == "ACQUIRE_INVENTORY"
    assert s.active_workflow_type == "ACQUIRE_INVENTORY"
    assert s.last_portfolio_followup_kind == "outlook"
    # the outlook needs the target-miss probability, which requires the default target to be passed
    assert (r.result["one_month_forecast"].get("risk_probabilities") or {}).get("revenue_below_target")


# --- 2-3. routing ---------------------------------------------------------------------


def test_three_follow_ups_route_to_their_kind(convo):
    s, _ = convo
    assert handle_followup(P1, s, as_of=AS_OF).kind == "portfolio_lot_today"
    assert handle_followup(P2, s, as_of=AS_OF).kind == "portfolio_top_risk"
    assert handle_followup(P3, s, as_of=AS_OF).kind == "portfolio_acquire_review"


# --- 4-7. top-risk selection ----------------------------------------------------------


def test_top_five_is_default_and_leads_with_the_riskiest(convo):
    s, r = convo
    rows = handle_followup(P2, s, as_of=AS_OF).payload["vehicles"]
    assert len(rows) == 5
    ranked = r.result["top_risk_vehicles"]
    assert rows[0]["vehicle_id"] == ranked[0]["vehicle_id"] == "V-10005"
    assert all(row["risk_factors"] for row in rows)              # non-empty why for every card


def test_explicit_count_is_parsed(convo):
    s, _ = convo
    assert handle_followup("show me the top 3 vehicles that need attention", s, as_of=AS_OF).payload["n"] == 3
    assert handle_followup("top five vehicles needing attention", s, as_of=AS_OF).payload["n"] == 5


# --- 8. acquire review surfaces the stored warnings -----------------------------------


def test_acquire_review_surfaces_the_stored_warnings(convo):
    s, _ = convo
    codes = {w["code"] for w in handle_followup(P3, s, as_of=AS_OF).payload["warnings"]}
    assert {"HIGH_PERCENTAGE_BELOW_BREAK_EVEN", "INBOUND_CAPACITY_CONFLICT",
            "HIGH_AGED_INVENTORY_CONCENTRATION"} <= codes


# --- 9-12. context & state ------------------------------------------------------------


def test_context_preserved_and_state_progresses(convo):
    s, r = convo
    before = s.active_result
    steps = [(P1, "lot_today"), (P2, "top_risk"), (P3, "acquire_review")]
    for prompt, kind in steps:
        out = handle_followup(prompt, s, as_of=AS_OF)
        assert out.kind.startswith("portfolio_")               # never a switch or clarification
        assert "which vehicle" not in out.text.lower()          # no re-identification
        assert s.active_workflow_type == "ACQUIRE_INVENTORY"    # same workflow throughout
        assert s.active_result is before                        # same stored result
        assert s.last_portfolio_followup_kind == kind           # progression


def test_lot_today_kpis_and_full_table(convo):
    s, _ = convo
    p = handle_followup(P1, s, as_of=AS_OF).payload
    k = p["kpis"]
    assert k["units_on_lot"] == 20 and k["below_break_even"] == 4 and k["over_90_units"] == 11
    assert len(p["vehicles"]) == 20                             # the full ranked table, inline


# --- 13-14. genuine switches still switch ---------------------------------------------


def test_a_pricing_request_still_switches(convo):
    s, _ = convo
    r = handle_followup("What should I price the 2021 Honda Accord EX?", s, as_of=AS_OF)
    assert r.kind == "workflow_switch"
    assert s.active_workflow_type == "PRICE_INVENTORY"


def test_free_up_space_switches_to_improve_aging(convo):
    s, _ = convo
    r = handle_followup(P5, s, as_of=AS_OF)
    assert r.kind == "workflow_switch"
    assert s.active_workflow_type == "IMPROVE_AGING_INVENTORY"


# --- 15. no calculation layer in the portfolio conversation modules -------------------


@pytest.mark.parametrize("name", ["portfolio_followup.py", "portfolio_copy.py"])
def test_portfolio_modules_do_not_import_the_calculation_layer(name):
    tree = ast.parse((AGENTS / name).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    banned = ("pricing_agent.domain", "pricing_agent.simulation", "numpy", "solver")
    assert not any(m.startswith(b) for m in imported for b in banned), imported


# --- 16-17. no regression -------------------------------------------------------------


def test_pricing_follow_ups_still_work():
    r = run_assistant("What should I price V-10001?", as_of=AS_OF)
    s = new_state()
    s.add_user("x")
    s.add_assistant(r.message, SOURCE_FIRST_TURN, result=r.result, response=r)
    s.adopt_response(r)
    out = handle_followup("Show me three pricing strategies to compare", s, as_of=AS_OF)
    assert out.kind == "pricing_strategies"


def test_aging_follow_ups_still_work():
    r = run_assistant("Which aging vehicles should I promote?", as_of=AS_OF)
    s = new_state()
    s.add_user("x")
    s.add_assistant(r.message, SOURCE_FIRST_TURN, result=r.improve_aging, response=r)
    s.adopt(r)
    out = handle_followup("Why is the BMW recommended for wholesale?", s, as_of=AS_OF)
    assert out.kind == "explanation"
    assert s.active_workflow_type == "IMPROVE_AGING_INVENTORY"
