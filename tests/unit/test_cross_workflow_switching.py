"""Cross-workflow intent switching.

With an Improve Aging conversation active, a new top-level *pricing* request switches to the
Single Vehicle Valuation workflow instead of being forced into the aging result as a follow-up —
while genuine aging follow-ups (explain, filter, rerun) stay put. The switch runs the existing
deterministic valuation, replaces the active result only on success, and preserves the prior
aging result in history. Nothing here computes a valuation.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pricing_agent.agents import new_state, run_assistant
from pricing_agent.agents import followup as followup_mod
from pricing_agent.agents.followup import detect_new_workflow, handle_followup

AGENTS = Path(__file__).resolve().parents[2] / "src" / "pricing_agent" / "agents"
AS_OF = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)


def _aging_state():
    s = new_state()
    r = run_assistant("Which aging vehicles should I promote?", as_of=AS_OF)
    s.add_user("Which aging vehicles should I promote?")
    s.add_assistant(r.message, "first_turn", result=r.improve_aging, response=r)
    s.adopt(r)
    return s


@pytest.fixture()
def state():
    return _aging_state()


# --- 1–5. the core switch -------------------------------------------------------------


def test_pricing_request_switches_to_valuation(state):
    result = handle_followup("What should I price 2021 Honda Accord EX?", state, as_of=AS_OF)
    assert result.kind == "workflow_switch"
    assert result.success is True
    assert state.active_workflow_type == "PRICE_INVENTORY"
    assert state.active_vehicle_ids == ("V-10002",)


def test_pricing_request_is_not_an_aging_explanation(state):
    result = handle_followup("What should I price 2021 Honda Accord EX?", state, as_of=AS_OF)
    assert result.kind != "explanation"
    # detect_new_workflow classifies it as a new workflow, before follow-up handlers run.
    assert detect_new_workflow("What should I price 2021 Honda Accord EX?", state) is not None


def test_switch_runs_the_existing_valuation_workflow(state):
    handle_followup("What should I price 2021 Honda Accord EX?", state, as_of=AS_OF)
    # The active result is now a single-vehicle valuation dict carrying its own audit/simulation.
    res = state.active_result
    assert isinstance(res, dict)
    assert res["vehicle"]["vehicle_id"] == "V-10002"
    assert res["recommended_strategy"]["strategy"]
    assert state.active_simulation_ids and all(state.active_simulation_ids)


def test_active_updates_only_after_success_and_prior_is_preserved(state):
    before = state.active_result
    handle_followup("What should I price 2021 Honda Accord EX?", state, as_of=AS_OF)
    assert state.active_result is not before
    assert len(state.prior_workflows) == 1
    prior = state.prior_workflows[0]
    assert prior.workflow_type == "IMPROVE_AGING_INVENTORY"
    assert prior.result is before                      # the aging result is kept, not discarded


def test_prior_aging_messages_remain_in_history(state):
    handle_followup("What should I price 2021 Honda Accord EX?", state, as_of=AS_OF)
    texts = " ".join(m.text for m in state.messages)
    assert "Which aging vehicles should I promote?" in texts   # first turn preserved
    assert any(m.source == "workflow_switch" for m in state.messages)


# --- 6 & 7. aging questions remain follow-ups -----------------------------------------


def test_manager_review_question_stays_aging_explanation(state):
    result = handle_followup("Why does the Accord require manager review?", state, as_of=AS_OF)
    assert result.kind == "explanation"
    assert state.active_workflow_type == "IMPROVE_AGING_INVENTORY"
    assert result.referenced_ids == ("V-10002",)


def test_below_break_even_question_stays_existing_result_explanation(state):
    result = handle_followup("Is the Accord below break-even?", state, as_of=AS_OF)
    assert result.kind == "explanation"
    assert result.referenced_ids == ("V-10002",)
    assert state.active_workflow_type == "IMPROVE_AGING_INVENTORY"


# --- 8 & 9. more pricing phrasings switch ---------------------------------------------


def test_list_the_accord_for_switches(state):
    result = handle_followup("What should I list the Accord for?", state, as_of=AS_OF)
    assert result.kind == "workflow_switch"
    assert state.active_vehicle_ids == ("V-10002",)


def test_revalue_the_bmw_switches(state):
    result = handle_followup("Revalue the BMW", state, as_of=AS_OF)
    assert result.kind == "workflow_switch"
    assert state.active_vehicle_ids == ("V-10005",)


# --- 10. event stays an aging rerun ---------------------------------------------------


def test_use_summer_clearance_stays_aging_rerun(state):
    result = handle_followup("Use Summer Clearance.", state, as_of=AS_OF)
    assert result.kind == "rerun"
    assert state.active_workflow_type == "IMPROVE_AGING_INVENTORY"
    assert state.active_event == "Summer Clearance"


# --- 11. ambiguous pricing target asks -------------------------------------------------


def test_ambiguous_pricing_request_asks_for_clarification(state):
    result = handle_followup("Price the 2019 model", state, as_of=AS_OF)
    assert result.kind == "clarification"
    assert state.active_workflow_type == "IMPROVE_AGING_INVENTORY"   # did not switch
    assert set(result.referenced_ids) == {"V-10012", "V-10019", "V-10004"}


def test_the_rav4_uses_documented_analysed_preference(state):
    # Two identical RAV4s exist (V-10001, V-10007). On the larger lot neither falls inside the
    # deep-analysis cap, so the resolver returns a single concrete RAV4 rather than asking —
    # pricing "the RAV4" resolves without a clarification.
    result = handle_followup("Price the RAV4", state, as_of=AS_OF)
    assert result.kind == "workflow_switch"
    assert state.active_vehicle_ids == ("V-10007",)


# --- 12 & 13. failed valuation preserves the prior aging result -----------------------


def test_failed_valuation_preserves_prior_and_does_not_fall_back(state, monkeypatch):
    before = state.active_result

    def _boom(*a, **k):
        raise RuntimeError("valuation failure")

    monkeypatch.setattr(followup_mod, "run_assistant", _boom)
    result = handle_followup("What should I price 2021 Honda Accord EX?", state, as_of=AS_OF)
    assert result.kind == "error" and result.success is False
    assert state.active_result is before                       # unchanged
    assert state.active_workflow_type == "IMPROVE_AGING_INVENTORY"
    assert len(state.prior_workflows) == 0                     # no switch happened
    # It did not silently fall back to an aging explanation.
    assert "valuation" in result.text.lower()


# --- 14. no calculation added to the routing / follow-up layer ------------------------


@pytest.mark.parametrize("name", ["router.py", "followup.py", "conversation.py"])
def test_routing_and_followup_add_no_pricing_calculation(name):
    src = (AGENTS / name).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for mod in imported:
        assert not mod.startswith(("pricing_agent.domain", "pricing_agent.simulation")), mod
    for banned in ("np.percentile", ".percentile(", "np.mean", "np.average", "simulate("):
        assert banned not in src, f"{name}: {banned}"
    for banned in ("publish_vehicle_price", "save_pricing_decision", "write_client", "WriteClient"):
        assert banned not in src, f"{name}: {banned}"


# --- detection unit checks ------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "What should I price 2021 Honda Accord EX?",
    "Price the Honda Accord.",
    "Run a valuation for V-10002.",
    "Is the BMW priced competitively?",
])
def test_detect_flags_pricing_requests(state, text):
    assert detect_new_workflow(text, state) is not None


@pytest.mark.parametrize("text", [
    "Why does the Accord require manager review?",
    "Why was the Accord selected?",
    "Which aging risks apply to the Accord?",
    "Should the Accord be included in Summer Clearance?",
    "Show me the aging evidence for the Accord.",
    "Use Summer Clearance",
])
def test_detect_leaves_aging_followups_alone(state, text):
    assert detect_new_workflow(text, state) is None


# --- any workflow (not only aging) seeds a multi-turn conversation ---------------------
# The assistant home now opens a conversation around any structured result, so every question
# enters the thread with a follow-up box. These lock that contract at the state-model level (the
# generic adopter the view calls), independent of Streamlit.


def test_a_pricing_first_turn_seeds_a_conversation():
    s = new_state()
    r = run_assistant("What should I price 2020 Ford F-150 XLT?", as_of=AS_OF)
    assert r.improve_aging is None and r.result is not None      # a single-skill valuation
    s.add_user("What should I price 2020 Ford F-150 XLT?")
    s.add_assistant(r.message, "first_turn", result=r.result, response=r)
    s.adopt_response(r)
    assert s.has_active_result
    assert s.active_workflow_type == "PRICE_INVENTORY"


def test_followup_on_a_non_aging_first_turn_is_honest_not_fabricated():
    s = new_state()
    r = run_assistant("What should I price 2020 Ford F-150 XLT?", as_of=AS_OF)
    s.add_user("q")
    s.add_assistant(r.message, "first_turn", result=r.result, response=r)
    s.adopt_response(r)
    result = handle_followup("Why protect profit?", s, as_of=AS_OF)
    # Non-aging results have no rich follow-up engine yet: the reply is an honest clarification,
    # never a fabricated aging-style answer, and it never reran a workflow.
    assert result.kind == "clarification"
    assert "Single Vehicle Valuation" in result.text
    assert not result.reran
