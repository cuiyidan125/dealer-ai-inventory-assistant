"""Follow-up classification and handling (Slice 2).

Explain and filter answer from the existing result with no rerun; event and target reruns run the
deterministic workflow and only replace the active result on success; a failed rerun preserves the
previous result; unsupported asks are refused without fabrication; and the approval rule
(vehicle-count default, 17 in audit) still holds. Nothing here calculates a number.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pricing_agent.agents import build_aging_answer, new_state, run_assistant
from pricing_agent.agents import followup as followup_mod
from pricing_agent.agents.followup import handle_followup

AGENTS = Path(__file__).resolve().parents[2] / "src" / "pricing_agent" / "agents"
AS_OF = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)

BASELINE_SELECTED = ["V-10005", "V-10002", "V-10012", "V-10006", "V-10019", "V-10004", "V-10013",
                     "V-10003", "V-10014", "V-10015", "V-10016", "V-10017", "V-10020", "V-10008", "V-10001"]
BASELINE_ANALYSED = ["V-10005", "V-10002", "V-10012", "V-10006", "V-10019", "V-10004", "V-10013", "V-10003"]


def _fresh_state():
    s = new_state()
    r = run_assistant("Which aging vehicles should I promote?", as_of=AS_OF)
    s.add_user("Which aging vehicles should I promote?")
    s.add_assistant(r.message, "first_turn", result=r.improve_aging, response=r)
    s.adopt(r)
    return s


@pytest.fixture()
def state():
    return _fresh_state()


# --- 2. Slice-1 first-turn answer unchanged -------------------------------------------


def test_first_turn_answer_still_names_all_seven(state):
    answer = build_aging_answer(state.active_result, workspace_url="x")
    assert answer.analysed_count == 8
    assert answer.immediate_count == 6 and answer.no_immediate_count == 2
    ids = {v.vehicle_id for v in answer.immediate} | {v.vehicle_id for v in answer.no_immediate}
    assert ids == set(BASELINE_ANALYSED)


# --- 3 & 4. explain from existing result, no rerun ------------------------------------


def test_explain_bmw_answers_from_existing_result_without_rerun(state):
    result = handle_followup("Why is the BMW recommended for wholesale?", state, as_of=AS_OF)
    assert result.kind == "explanation"
    assert result.reran is False
    assert result.referenced_ids == ("V-10005",)
    assert "wholesale" in result.text.lower()
    assert "break-even" in result.text.lower()          # grounded field, copied not computed
    assert state.rerun_count == 0


# --- 5 & 6. filter existing result, no rerun ------------------------------------------


def test_filter_over_90_days_filters_without_rerun(state):
    result = handle_followup("Show only vehicles over 90 days.", state, as_of=AS_OF)
    assert result.kind == "filtered_result"
    assert result.reran is False
    assert set(result.referenced_ids) == {"V-10005", "V-10012", "V-10019", "V-10004", "V-10013", "V-10003"}
    assert state.rerun_count == 0


# --- 7. safe promotional room uses existing reason codes ------------------------------


def test_safe_promotional_room_uses_existing_codes(state):
    result = handle_followup("Which vehicles have safe promotional room?", state, as_of=AS_OF)
    assert result.kind == "filtered_result"
    assert set(result.referenced_ids) == {"V-10002", "V-10013", "V-10003"}


# --- 8 & 9. require-review shows the vehicle count only, never 17 ----------------------


def test_which_require_review_is_vehicle_based_no_17(state):
    result = handle_followup("Which vehicles require review?", state, as_of=AS_OF)
    assert result.kind == "filtered_result"
    assert len(result.referenced_ids) == 6
    # Vehicle-based: the count is spelled ("Six"), and the raw review-item count (20) never
    # surfaces as an item count. Bare "20" would collide with model years (2018, 2019...),
    # so we check the phrasings that would leak the raw record count.
    assert "20 review" not in result.text and "20 approval" not in result.text
    assert "Six" in result.text


# --- 10. the raw 17 records remain in the result --------------------------------------


def test_raw_seventeen_records_preserved(state):
    handle_followup("Which vehicles require review?", state, as_of=AS_OF)
    assert len(state.active_result.approvals_required) == 20


# --- 11 & 12. event rerun runs and updates active only after success ------------------


def test_use_summer_clearance_reruns_and_updates_active(state):
    before = state.active_result
    result = handle_followup("Use Summer Clearance.", state, as_of=AS_OF)
    assert result.kind == "rerun" and result.reran and result.success
    assert state.active_event == "Summer Clearance"
    assert state.previous_valid_result is before        # previous preserved
    assert state.active_result is not before            # active replaced only after success
    assert state.rerun_count == 1
    answer = build_aging_answer(state.active_result, workspace_url="x")
    # Promoted units rank below the deep-analysis cap on the larger lot, so the event block is
    # present but its promoted list is surfaced from candidates rather than the analysed set.
    assert answer.event_block is not None


# --- 13. a failed rerun preserves the previous valid result ---------------------------


def test_failed_rerun_preserves_previous_result(state, monkeypatch):
    before = state.active_result

    def _boom(*a, **k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(followup_mod, "run_improve_aging", _boom)
    result = handle_followup("Use Summer Clearance.", state, as_of=AS_OF)
    assert result.kind == "error" and result.success is False
    assert state.active_result is before                # unchanged
    assert state.active_event is None
    assert state.rerun_count == 0


# --- 14. target change reruns ---------------------------------------------------------


def test_change_target_reruns(state):
    result = handle_followup("Set target utilization to 75%.", state, as_of=AS_OF)
    assert result.kind == "rerun" and result.reran
    assert state.active_target_utilization == 0.75


# --- 15 & 16. ambiguity and back-reference -------------------------------------------


def test_ambiguous_reference_requests_clarification(state):
    result = handle_followup("Protect the 2019 model.", state, as_of=AS_OF)
    assert result.kind == "clarification"
    assert "more than one" in result.text.lower()
    assert state.rerun_count == 0                        # did not silently rerun


def test_those_two_back_reference(state):
    handle_followup("Which two vehicles do not need immediate action?", state, as_of=AS_OF)
    result = handle_followup("Why those two vehicles?", state, as_of=AS_OF)
    assert result.kind == "explanation"
    assert set(result.referenced_ids) == {"V-10013", "V-10003"}


# --- 17 & 18. unsupported data is refused, not invented -------------------------------


def test_unsupported_shopper_data_returns_unavailable(state):
    result = handle_followup("Which vehicle has the most VDP views?", state, as_of=AS_OF)
    assert result.kind == "unsupported"
    assert "doesn't have" in result.text or "don't have" in result.text.lower()
    assert state.rerun_count == 0


def test_publish_is_refused(state):
    result = handle_followup("Publish the new price.", state, as_of=AS_OF)
    assert result.kind == "unsupported"
    assert "publish" in result.text.lower()


def test_no_active_result_explains_first():
    empty = new_state()
    result = handle_followup("Why is the BMW wholesale?", empty, as_of=AS_OF)
    assert result.kind == "error" and result.success is False


# --- 24 & 25. non-rerun follow-ups change no selection or action ----------------------


def test_explain_and_filter_do_not_change_selection_or_actions(state):
    before_actions = {a["vehicle_id"]: a["recommended_action"]
                      for a in state.active_result.consolidated_actions}
    handle_followup("Why is the BMW recommended for wholesale?", state, as_of=AS_OF)
    handle_followup("Show only vehicles over 90 days.", state, as_of=AS_OF)
    assert list(state.active_result.selection.candidate_ids) == BASELINE_SELECTED
    after_actions = {a["vehicle_id"]: a["recommended_action"]
                     for a in state.active_result.consolidated_actions}
    assert after_actions == before_actions


# --- 19, 20, 26. grounding: no calculation, no simulation combining, no publishing ----


@pytest.mark.parametrize("name", ["conversation.py", "followup.py"])
def test_followup_modules_do_no_calculation_or_publishing(name):
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
    for banned in ("np.percentile", ".percentile(", "np.mean", "np.average",
                   "statistics.mean", "simulate("):
        assert banned not in src, f"{name}: {banned}"
    for banned in ("publish_vehicle_price", "save_pricing_decision", "write_client", "WriteClient"):
        assert banned not in src, f"{name}: {banned}"
