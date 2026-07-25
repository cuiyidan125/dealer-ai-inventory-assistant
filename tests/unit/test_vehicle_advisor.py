"""Single-vehicle AI orchestration — the enterprise-orchestrator hero slice.

One natural-language question ("I've had this F-150 a while — what should I do?") invokes the
three deterministic skills and synthesizes ONE prioritized, evidence-backed action plan with
explicit trade-offs. The orchestrator computes no number of its own — every figure is copied
from a skill result — and the plan always ends at a human approval, never an automatic action.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

from pricing_agent.agents.vehicle_advisor import (
    CAP_PORTFOLIO,
    CAP_PROMOTION,
    CAP_VALUATION,
    advise_vehicle,
)
from pricing_agent.mcp_clients import MockTransport

AS_OF = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)
SRC = Path(__file__).resolve().parents[2] / "src" / "pricing_agent" / "agents" / "vehicle_advisor.py"

# The hero vehicle: the aged, above-market F-150 that naturally exercises all three skills.
HERO = "V-10003"


def _advise(vid=HERO):
    return advise_vehicle(vid, MockTransport(as_of=AS_OF), as_of=AS_OF)


def test_the_hero_question_invokes_all_three_capabilities():
    adv = _advise()
    assert adv.intent == "Improve Aging Inventory"
    assert adv.capabilities_invoked == (CAP_VALUATION, CAP_PORTFOLIO, CAP_PROMOTION)
    assert "Ford F-150" in adv.description


def test_it_synthesizes_a_three_step_prioritized_plan():
    adv = _advise()
    titles = [a.title for a in adv.actions]
    assert [a.priority for a in adv.actions] == [1, 2, 3]        # contiguous priorities
    assert any("Reduce asking price" in t for t in titles)       # from valuation + portfolio
    assert any("campaign" in t.lower() for t in titles)          # from promotion planner
    assert any("merchandising" in t.lower() for t in titles)
    assert adv.review_in_days == 7


def test_every_action_carries_evidence_and_trade_offs():
    adv = _advise()
    for a in adv.actions:
        assert a.evidence, f"P{a.priority} has no evidence"
        assert a.pros and a.cons, f"P{a.priority} is missing a trade-off"
        assert a.source_skills, f"P{a.priority} does not name a source skill"


def test_the_price_action_is_grounded_in_the_valuation_numbers():
    adv = _advise()
    price = next(a for a in adv.actions if "Reduce asking price" in a.title)
    # The reduction and the day/gross trade-off must reconcile with the valuation scenarios.
    scen = {s["strategy"]: s for s in adv.valuation["pricing_scenarios"]}
    current = adv.valuation["vehicle"]["current_list_price"]
    reduction = current - scen["BALANCED"]["proposed_list_price"]
    assert f"${reduction:,.0f}" in price.title                    # copied, not invented
    assert CAP_VALUATION in price.source_skills and CAP_PORTFOLIO in price.source_skills


def test_the_promotion_action_only_fires_for_an_eligible_vehicle():
    adv = _advise()
    promo = next((a for a in adv.actions if "campaign" in a.title.lower()), None)
    assert promo is not None
    ranked = {c["vehicle_id"]: c for c in adv.promotion["candidate_ranking"]}
    assert ranked[HERO]["eligible"] is True


def test_the_orchestrator_never_computes_a_number_itself():
    """It may select, order, format, and subtract two skill figures — but it must not import the
    calculation or simulation layers, so it can never originate a price, forecast, or probability."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    banned = {"pricing_agent.domain", "pricing_agent.simulation", "numpy"}
    assert not any(m.startswith(b) for m in imported for b in banned), imported
