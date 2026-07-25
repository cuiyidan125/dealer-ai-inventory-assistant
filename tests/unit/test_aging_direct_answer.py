"""Conversational Result Exploration — the grounded first-turn direct answer.

These hold two lines at once: the Assistant now answers *in the conversation* — naming the actual
vehicles, their recommended actions, and a concise reason each, read from the structured result —
and it invents nothing. Every vehicle, count, and reason traces to a field the workflow produced;
the answer builder computes no number and imports no calculation layer.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pricing_agent.agents import build_aging_answer, run_assistant
from pricing_agent.mcp_clients import MockTransport
from pricing_agent.workflows.improve_aging import ImproveAgingRequest, run_improve_aging

AGENTS = Path(__file__).resolve().parents[2] / "src" / "pricing_agent" / "agents"
AS_OF = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)

IMMEDIATE_IDS = ["V-10005", "V-10002", "V-10012", "V-10006", "V-10019", "V-10004"]
NO_IMMEDIATE_IDS = ["V-10013", "V-10003"]
ANALYSED_IDS = IMMEDIATE_IDS + NO_IMMEDIATE_IDS

SUMMER = ImproveAgingRequest(
    target_utilization=0.70, event_requested=True, event_id="EVT-SUMMER-2026",
    event_name="Summer Clearance", available_events=("Summer Clearance", "Labor Day Sales Event"))


@pytest.fixture(scope="module")
def no_event_response():
    return run_assistant("Which aging vehicles should I promote?", as_of=AS_OF)


@pytest.fixture(scope="module")
def answer(no_event_response):
    return build_aging_answer(no_event_response.improve_aging,
                              workspace_url=no_event_response.target_url)


@pytest.fixture(scope="module")
def event_answer():
    r = run_improve_aging(MockTransport(as_of=AS_OF), SUMMER)
    return build_aging_answer(r, workspace_url="improve-aging-inventory")


# --- 1. names the actual vehicles -----------------------------------------------------


def test_answer_names_all_seven_analysed_vehicles(answer):
    ids = {v.vehicle_id for v in answer.immediate} | {v.vehicle_id for v in answer.no_immediate}
    assert ids == set(ANALYSED_IDS)
    assert answer.analysed_count == 8
    # Each vehicle is named with its dealer description (year + make …), not just an id.
    for v in list(answer.immediate) + list(answer.no_immediate):
        assert v.description and v.description.split()[0].isdigit()


# --- 2. all five immediate-action vehicles, each with an action and a reason -----------


def test_answer_lists_all_five_immediate_action_vehicles(answer):
    assert [v.vehicle_id for v in answer.immediate] == IMMEDIATE_IDS
    assert answer.immediate_count == 6
    for v in answer.immediate:
        assert v.action_label and v.reason


# --- 3. the two no-immediate-action vehicles ------------------------------------------


def test_answer_identifies_the_two_no_immediate_vehicles(answer):
    assert {v.vehicle_id for v in answer.no_immediate} == set(NO_IMMEDIATE_IDS)
    assert answer.no_immediate_count == 2
    for v in answer.no_immediate:
        assert "sale-event candidate" in v.action_label.lower()


# --- 4. never implies all seven should be promoted ------------------------------------


def test_answer_does_not_imply_all_seven_should_be_promoted(answer):
    assert answer.event_selected is False
    assert answer.promotion_finalized is False
    # None of the immediate actions is a promotion, and the buckets stay 6 + 2.
    assert all(v.action_code != "EVENT_PROMOTION" for v in answer.immediate)
    assert answer.immediate_count == 6 and answer.no_immediate_count == 2
    text = " ".join([answer.understood, answer.promotion_note, answer.key_review_note]).lower()
    assert "all seven" not in text and "7 vehicles are recommended for promotion" not in text


# --- 5. promotion eligibility pending without an event --------------------------------


def test_answer_states_promotion_pending_without_event(answer):
    assert "not finalized" in answer.promotion_note.lower()
    assert answer.event_block is None


# --- reasons and counts are grounded --------------------------------------------------


def test_reasons_are_built_from_existing_reason_labels(answer):
    # The 120-day Jeep cites its over-120 label; the Bolt cites depreciation risk.
    jeep = next(v for v in answer.immediate if v.vehicle_id == "V-10012")
    bolt = next(v for v in answer.immediate if v.vehicle_id == "V-10006")
    assert "120 days" in jeep.reason
    assert "value loss" in bolt.reason.lower() or "depreciation" in bolt.reason.lower()


def test_answer_review_counts_are_vehicle_based(answer):
    assert answer.review_vehicle_count == 6
    assert answer.manager_review_count == 2
    assert answer.review_item_count == 20
    assert "6 vehicles require review" in answer.key_review_note
    # The default dealer text never leads with the raw record count.
    assert "20" not in answer.key_review_note
    assert "20" not in answer.understood and "20" not in answer.promotion_note


def test_answer_invents_no_vehicle(no_event_response, answer):
    valid = {a["vehicle_id"] for a in no_event_response.improve_aging.consolidated_actions}
    for v in list(answer.immediate) + list(answer.no_immediate):
        assert v.vehicle_id in valid


# --- event-enabled behaviour distinguishes promoted from analysed --------------------


def test_event_answer_distinguishes_promoted_and_finalizes(event_answer):
    assert event_answer.event_selected is True
    assert event_answer.promotion_finalized is True
    assert event_answer.event_block is not None
    # With the event applied, both hold-gross analysed units are picked up for promotion and
    # move into the immediate side, leaving no no-immediate units; the promoted units surface
    # via the event block.
    assert event_answer.no_immediate_count == 0
    assert "not finalized" not in event_answer.promotion_note.lower()


# --- grounding: the answer builder does no calculation --------------------------------


def test_aging_answer_imports_no_calculation_layer():
    src = (AGENTS / "aging_answer.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for mod in imported:
        assert not mod.startswith(("pricing_agent.domain", "pricing_agent.simulation")), mod
    for banned in ("np.percentile", ".percentile(", "np.mean", "simulate("):
        assert banned not in src
    for banned in ("publish_vehicle_price", "save_pricing_decision", "write_client", "WriteClient"):
        assert banned not in src


def test_missing_result_yields_no_fabricated_answer():
    assert build_aging_answer(None, workspace_url="x") is None
