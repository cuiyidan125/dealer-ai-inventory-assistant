"""Conversation state and deterministic reference resolution (Slice 2).

The active structured result is the source of truth: state carries the workflow / request /
simulation ids and the raw approvals unchanged, and every dealer reference — "the BMW", "those
two vehicles", an ambiguous "2019 model" — resolves against that result, never by guessing.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pricing_agent.agents import new_state, run_assistant
from pricing_agent.agents.conversation import resolve_reference, vehicle_index

AS_OF = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)

# The 8 deep-analysed candidates (MAX_DEEP_ANALYSIS), in vehicle_evidence order.
BASELINE_ANALYSED = ["V-10005", "V-10002", "V-10012", "V-10006", "V-10019", "V-10004", "V-10013", "V-10003"]
BASELINE_EXCLUDED = ["V-10007", "V-10009", "V-10010", "V-10011", "V-10018"]


@pytest.fixture(scope="module")
def state():
    s = new_state()
    r = run_assistant("Which aging vehicles should I promote?", as_of=AS_OF)
    s.add_user("Which aging vehicles should I promote?")
    s.add_assistant("first", "first_turn", result=r.improve_aging, response=r)
    s.adopt(r)
    return s


# --- 1 & 21–23. history and id preservation -------------------------------------------


def test_conversation_preserves_multiple_turns():
    s = new_state()
    r = run_assistant("Which aging vehicles should I promote?", as_of=AS_OF)
    s.add_user("q1")
    s.add_assistant("a1", "first_turn", result=r.improve_aging, response=r)
    s.add_user("q2")
    s.add_assistant("a2", "explanation")
    assert [m.role for m in s.messages] == ["user", "assistant", "user", "assistant"]
    assert [m.text for m in s.messages] == ["q1", "a1", "q2", "a2"]


def test_state_preserves_workflow_request_and_simulation_ids(state):
    assert state.active_workflow_id and state.active_workflow_id.startswith("iaw_")
    assert state.active_request_id                      # a real single-vehicle request id
    assert len(state.active_simulation_ids) >= 8        # portfolio + 7 vehicles at least
    assert all(state.active_simulation_ids)


def test_state_preserves_raw_approvals(state):
    assert len(state.active_approvals) == 20            # the raw records, unchanged


# --- reference resolution --------------------------------------------------------------


def test_reference_the_bmw_resolves_to_one_vehicle(state):
    ref = resolve_reference("why is the BMW recommended for wholesale?", state)
    assert ref.ids == ("V-10005",)
    assert not ref.ambiguous


def test_reference_group_wholesale(state):
    ref = resolve_reference("show the wholesale vehicles", state)
    assert set(ref.ids) == {"V-10005", "V-10012", "V-10019", "V-10004"}


def test_reference_the_rav4_resolves_to_a_single_vehicle(state):
    # Two "2022 Toyota RAV4 XLE" exist (V-10001, V-10007). Neither is in the deep-analysis cap on
    # the larger lot, so the resolver returns a single concrete RAV4 rather than guessing a set.
    ref = resolve_reference("protect the RAV4", state)
    assert ref.ids == ("V-10007",)
    assert not ref.ambiguous


def test_ambiguous_reference_is_flagged(state):
    # Three 2019 vehicles (Jeep, Silverado, Nissan) — genuinely ambiguous.
    ref = resolve_reference("the 2019 model", state)
    assert set(ref.ids) == {"V-10012", "V-10019", "V-10004"}
    assert ref.ambiguous


def test_those_two_resolves_to_the_recent_pair(state):
    state.last_referenced_vehicle_ids = ("V-10008", "V-10001")
    ref = resolve_reference("those two vehicles", state)
    assert ref.ids == ("V-10008", "V-10001")


# --- 24 & 25. selection / actions are what the result already decided -----------------


def test_index_matches_selection_and_actions(state):
    index = vehicle_index(state.active_result)
    analysed = [vid for vid, r in index.items() if r.analysed]
    assert analysed == BASELINE_ANALYSED
    excluded = [vid for vid, r in index.items() if r.excluded]
    assert excluded == BASELINE_EXCLUDED
    assert index["V-10005"].action_code == "WHOLESALE_OR_LOSS_MINIMIZATION_REVIEW"
    assert index["V-10002"].action_code == "MANAGER_REVIEW"
    assert index["V-10013"].action_code == "NO_ACTION"
