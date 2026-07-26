"""Conversational Single-Vehicle Pricing follow-ups.

A valuation opens a pricing conversation the dealer can explore without repeating the vehicle:
re-target by days, compare strategies, pick one, ask for the reasoning. Every number is copied from
the stored `single_vehicle` result or a `promotional_headroom` ladder rung — this layer selects and
narrates, it never prices. The dealer stays the decision-maker (a selection is "for review", nothing
is published).
"""

from __future__ import annotations

import ast
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pricing_agent.agents import new_state, run_assistant
from pricing_agent.agents.conversation import SOURCE_FIRST_TURN
from pricing_agent.agents.followup import handle_followup
from pricing_agent.agents.pricing_followup import _select_target_rung

AGENTS = Path(__file__).resolve().parents[2] / "src" / "pricing_agent" / "agents"
AS_OF = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)
HERO = "V-10001"  # 2022 Toyota RAV4 XLE

# The exact live-demo prompts (beat 1 arrives pre-resolved; the RAV4 XLE is intentionally a
# two-way match, so the app resolves it via the pick list first).
P2 = "Thirty days is longer than I'd like. What price gives me the best chance of selling it within about 20 days?"
P3 = "That price feels too aggressive. Show me three pricing strategies so I can compare the profit and turn trade-offs."
P4 = "I prefer the Balanced strategy. Show me the detailed financial impact before I approve it."
P5 = "Before I approve it, walk me through why Balanced is better for this vehicle than Protect Profit or Sell Faster."


def _valuation_state(vid=HERO):
    r = run_assistant(f"What should I price {vid}?", as_of=AS_OF)
    s = new_state()
    s.add_user("price it")
    s.add_assistant(r.message, SOURCE_FIRST_TURN, result=r.result, response=r)
    s.adopt_response(r)
    return s, r


@pytest.fixture()
def convo():
    return _valuation_state()


# --- 1. all five turns produce their beat ---------------------------------------------


def test_all_five_turns_route_to_their_beat(convo):
    s, _ = convo
    assert s.active_workflow_type == "PRICE_INVENTORY"
    assert handle_followup(P2, s, as_of=AS_OF).kind == "pricing_target"
    assert handle_followup(P3, s, as_of=AS_OF).kind == "pricing_strategies"
    assert handle_followup(P4, s, as_of=AS_OF).kind == "pricing_detail"
    assert handle_followup(P5, s, as_of=AS_OF).kind == "pricing_reasoning"


# --- 2 & 3. context preserved, no re-identification -----------------------------------


def test_vehicle_context_preserved_and_never_re_identified(convo):
    s, _ = convo
    for prompt in (P2, P3, P4, P5):
        r = handle_followup(prompt, s, as_of=AS_OF)
        assert s.active_vehicle_ids == (HERO,)                 # same car throughout
        assert r.kind.startswith("pricing_")                   # never a switch or clarification
        assert "which vehicle" not in r.text.lower()           # never asks to re-identify


# --- 4, 5, 7, 8. the 20-day rung ------------------------------------------------------


def test_twenty_day_rung_is_the_highest_price_safe_rung_meeting_the_target(convo):
    s, r = convo
    ladder = r.result["promotional_headroom"]["ladder"]
    rung, reached = _select_target_rung(r.result, 20)
    assert reached is True
    assert rung["list_price"] == 27695.0 and rung["p50_days_to_sale"] == 20.0
    # It is the maximum price among all safe rungs that reach the target (minimum discount).
    qualifying = [x for x in ladder if not x["exceeds_safe_discount"] and x["p50_days_to_sale"] <= 20]
    assert rung["list_price"] == max(x["list_price"] for x in qualifying)


def test_target_day_exploration_updates_active_but_not_baseline(convo):
    s, _ = convo
    baseline_before = s.baseline_pricing_recommendation["proposed_list_price"]
    r = handle_followup(P2, s, as_of=AS_OF)
    p = r.payload
    assert p["reached"] is True
    assert p["target"]["price"] == 27695.0 and p["target"]["p50_days"] == 20.0
    assert p["previous"]["price"] == baseline_before                 # compared to the baseline
    assert p["price_change"] == 27695.0 - baseline_before
    assert p["material_compression"] is True                          # gross compresses materially
    # baseline preserved, active moved
    assert s.baseline_pricing_recommendation["proposed_list_price"] == baseline_before
    assert s.active_pricing_recommendation["proposed_list_price"] == 27695.0


# --- 6. fallback when no safe rung reaches the target ---------------------------------


def test_unreachable_target_returns_fastest_safe_rung(convo):
    s, r = convo
    rung, reached = _select_target_rung(r.result, 5)          # faster than any safe rung
    assert reached is False
    safe = [x for x in r.result["promotional_headroom"]["ladder"] if not x["exceeds_safe_discount"]]
    assert rung["p50_days_to_sale"] == min(x["p50_days_to_sale"] for x in safe)
    out = handle_followup("What price sells it within 5 days?", s, as_of=AS_OF)
    assert out.payload["reached"] is False
    assert s.baseline_pricing_recommendation["proposed_list_price"] == 28995.0 or \
        s.baseline_pricing_recommendation["proposed_list_price"] == 29195.0  # unchanged baseline


# --- 9. selection only after an explicit pick ----------------------------------------


def test_selected_strategy_set_only_on_explicit_selection(convo):
    s, _ = convo
    handle_followup(P2, s, as_of=AS_OF)
    handle_followup(P3, s, as_of=AS_OF)
    assert s.selected_pricing_strategy is None                # exploring did not select
    handle_followup(P4, s, as_of=AS_OF)
    assert s.selected_pricing_strategy == "BALANCED"


# --- 10 & 11. strategies + Balanced maps to BALANCED ---------------------------------


def test_three_strategies_present_and_balanced_maps_to_code(convo):
    s, _ = convo
    rows = handle_followup(P3, s, as_of=AS_OF).payload["rows"]
    assert [x["strategy"] for x in rows] == ["MAXIMIZE_GROSS", "BALANCED", "INCREASE_VELOCITY"]
    assert [x["label"] for x in rows] == ["Protect Profit", "Balanced", "Sell Faster"]
    detail = handle_followup(P4, s, as_of=AS_OF).payload
    assert detail["code"] == "BALANCED" and detail["label"] == "Balanced"


# --- 12. detail reconciles to the stored scenario ------------------------------------


def test_detail_values_reconcile_to_the_stored_balanced_scenario(convo):
    s, r = convo
    handle_followup(P4, s, as_of=AS_OF)
    m = s.messages[-1].payload["metrics"]
    scen = next(x for x in r.result["pricing_scenarios"] if x["strategy"] == "BALANCED")
    assert m["recommended_price"] == scen["proposed_list_price"]
    assert m["p50_gross"] == scen["expected_front_end_gross"]["p50"]
    assert m["p50_holding_cost"] == scen["expected_cash_holding_cost"]["p50"]
    assert m["p50_days"] == scen["additional_days_to_sale"]["p50"]
    assert m["break_even_price"] == r.result["break_even_analysis"]["current_accounting_break_even"]
    assert m["confidence_level"] == r.result["valuation"]["confidence"]["level"]


def test_selection_states_review_not_published(convo):
    s, _ = convo
    r = handle_followup(P4, s, as_of=AS_OF)
    assert r.payload["notice"] == "The Balanced strategy is selected for review. No pricing action has been published."
    assert "great choice" not in r.text.lower()


# --- 13 & 14. reasoning explains without reproducing the card or inventing numbers -----


def test_reasoning_explains_without_reproducing_the_card(convo):
    s, r = convo
    handle_followup(P4, s, as_of=AS_OF)          # select Balanced first
    out = handle_followup(P5, s, as_of=AS_OF)
    p = out.payload
    assert p["code"] == "BALANCED"
    assert len(p["steps"]) == 4
    assert "metrics" not in p                                  # does not render the financial card
    titles = " ".join(t for t, _ in p["steps"]).lower()
    for concept in ("market", "window", "profitability", "trade-off"):
        assert concept in titles
    # Does not reproduce card figures, and introduces no new prices (the prose is qualitative).
    bodies = " ".join(b for _, b in p["steps"])
    for price in (28095, 26148, 28400, 27357):
        assert str(price) not in bodies
    assert not re.search(r"\$\d", bodies)


def test_next_checkpoint_only_on_the_reasoning_turn(convo):
    s, _ = convo
    r2 = handle_followup(P2, s, as_of=AS_OF)
    r4 = handle_followup(P4, s, as_of=AS_OF)
    assert "45 days" not in (r2.text + str(r2.payload))
    assert "45 days" not in (r4.text + str(r4.payload))
    r5 = handle_followup(P5, s, as_of=AS_OF)
    assert "unsold after 45 days" in r5.payload["next_checkpoint"]


# --- 15. cross-workflow switch still takes precedence --------------------------------


def test_switch_takes_precedence_for_a_genuine_new_workflow():
    s, _ = _valuation_state()
    r = handle_followup("What will my inventory look like in the next 30 days?", s, as_of=AS_OF)
    assert r.kind == "workflow_switch"
    assert s.active_workflow_type == "ACQUIRE_INVENTORY"


def test_switch_takes_precedence_for_a_new_vehicle():
    s, _ = _valuation_state()
    r = handle_followup("What should I price the 2021 Honda Accord EX?", s, as_of=AS_OF)
    assert r.kind == "workflow_switch"
    assert s.active_vehicle_ids == ("V-10002",)


# --- 16. no calculation layer in the pricing conversation modules ---------------------


@pytest.mark.parametrize("name", ["pricing_followup.py", "pricing_copy.py"])
def test_pricing_modules_do_not_import_the_calculation_layer(name):
    tree = ast.parse((AGENTS / name).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    banned = ("pricing_agent.domain", "pricing_agent.simulation", "numpy")
    assert not any(m.startswith(b) for m in imported for b in banned), imported


# --- 17. no regression: a plain valuation and an aging follow-up still work -----------


def test_first_turn_valuation_still_produces_a_result():
    r = run_assistant(f"What should I price {HERO}?", as_of=AS_OF)
    assert r.workflow.name == "PRICE_INVENTORY"
    assert r.result and r.result["pricing_scenarios"]


def test_aging_followup_unaffected():
    r = run_assistant("Which aging vehicles should I promote?", as_of=AS_OF)
    s = new_state()
    s.add_user("q")
    s.add_assistant(r.message, SOURCE_FIRST_TURN, result=r.improve_aging, response=r)
    s.adopt(r)
    out = handle_followup("Why is the BMW recommended for wholesale?", s, as_of=AS_OF)
    assert out.kind == "explanation"
    assert s.active_workflow_type == "IMPROVE_AGING_INVENTORY"
