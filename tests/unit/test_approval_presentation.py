"""Approval presentation — default surfaces show unique vehicle counts; 17 lives in audit.

The value 17 is the number of underlying review-condition records across 5 vehicles, not 17
separate dealer decisions. These tests hold the rule: the primary Assistant response, the
"What should I do next?" step, and the default workspace view show only the vehicle-level count;
the raw 17 (and the raw records) move into "View approval details". The distinction between the
5 review-condition vehicles and the 2 vehicles whose final action is MANAGER_REVIEW is preserved,
and all 17 raw records are unchanged.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pricing_agent.agents import build_aging_answer, run_assistant
from pricing_agent.views import improve_aging_copy as copy

VIEWS = Path(__file__).resolve().parents[2] / "src" / "pricing_agent" / "views"
AS_OF = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def response():
    return run_assistant("Which aging vehicles should I promote?", as_of=AS_OF)


@pytest.fixture(scope="module")
def result(response):
    return response.improve_aging


# --- 17 & 22. default Assistant summary shows the unique vehicle count -----------------


def test_default_summary_is_vehicle_based_and_keeps_the_distinction(response):
    s = response.summary
    assert s["review_vehicle_count"] == 6          # vehicles with review conditions
    assert s["manager_review_count"] == 2          # vehicles whose final action is MANAGER_REVIEW
    assert s["review_item_count"] == 20            # raw records — available, not primary


def test_direct_answer_key_note_is_vehicle_based(result):
    answer = build_aging_answer(result, workspace_url="x")
    assert answer.review_vehicle_count == 6
    assert "6 vehicles require review" in answer.key_review_note
    assert "20" not in answer.key_review_note


# --- 18 & 19. default workspace surfaces do not show 17 --------------------------------


def test_next_steps_do_not_show_the_raw_record_count(result):
    steps = copy.next_steps(result)
    joined = " ".join(s.title + " " + s.detail for s in steps)
    assert "17" not in joined
    # The review step is worded around vehicles, not "17 approvals required".
    assert "approval(s) are required" not in joined
    assert "have review conditions to clear" in joined


def test_next_steps_copy_has_no_raw_approval_count_phrasing():
    src = (VIEWS / "improve_aging_copy.py").read_text(encoding="utf-8")
    assert "approval(s) are required" not in src


def test_workspace_recommended_plan_metric_is_vehicle_based():
    src = (VIEWS / "improve_aging.py").read_text(encoding="utf-8")
    assert "Vehicles requiring review" in src
    # The raw review-item count is only ever rendered inside "View approval details".
    marker = src.index("View approval details")
    assert "rc['review_items']" in src
    assert src.index("rc['review_items']") > marker


def test_home_view_review_item_count_only_in_approval_details():
    src = (VIEWS / "assistant_home.py").read_text(encoding="utf-8")
    assert "key_review_note" in src                 # default line is vehicle-based
    marker = src.index("View approval details")
    assert "review_item_count" in src
    assert src.index("review_item_count") > marker
    # The old combined "(N review item(s))" default line is gone.
    assert "review item(s))" not in src


# --- 20. 17 remains available in "View approval details" ------------------------------


def test_seventeen_is_available_in_approval_details():
    for name in ("assistant_home.py", "improve_aging.py"):
        src = (VIEWS / name).read_text(encoding="utf-8")
        assert "View approval details" in src
        assert "Review conditions triggered" in src


# --- 21. all 17 raw records preserved unchanged ---------------------------------------


def test_all_seventeen_raw_records_preserved(result):
    recs = result.approvals_required
    assert len(recs) == 20
    by_vehicle = Counter(a["vehicle_id"] for a in recs)
    assert dict(by_vehicle) == {"V-10005": 3, "V-10002": 4, "V-10012": 3,
                                "V-10006": 3, "V-10019": 3, "V-10004": 4}
    types = {a.get("approval_type") for a in recs}
    assert types == {"LOSS_MINIMIZATION", "BELOW_PROJECTED_BREAK_EVEN",
                     "NEGATIVE_VALUE_RISK", "AGGRESSIVE_ADJUSTMENT"}


# --- 22. five affected vehicles vs two MANAGER_REVIEW vehicles -------------------------


def test_five_review_vehicles_distinct_from_two_manager_review(result):
    review_vehicles = {a["vehicle_id"] for a in result.approvals_required if a.get("vehicle_id")}
    manager_review = [a["vehicle_id"] for a in result.consolidated_actions
                      if a["recommended_action"] == "MANAGER_REVIEW"]
    assert review_vehicles == {"V-10005", "V-10002", "V-10012", "V-10006", "V-10019", "V-10004"}
    assert manager_review == ["V-10002", "V-10006"]
    assert set(manager_review) < review_vehicles       # strict subset — different concepts


# --- 23–26. selection, actions, ids, numbers unchanged --------------------------------


def test_selection_and_final_actions_unchanged(result):
    assert list(result.selection.candidate_ids) == \
        ["V-10005", "V-10002", "V-10012", "V-10006", "V-10019", "V-10004", "V-10013",
         "V-10014", "V-10015", "V-10016", "V-10017", "V-10020", "V-10008", "V-10001"]
    assert [e.vehicle_id for e in result.selection.exclusions] == \
        ["V-10003", "V-10007", "V-10009", "V-10010", "V-10011", "V-10018"]
    actions = {a["vehicle_id"]: a["recommended_action"] for a in result.consolidated_actions}
    assert actions["V-10013"] == "NO_ACTION" and actions["V-10014"] == "NO_ACTION"
    assert actions["V-10002"] == "MANAGER_REVIEW" and actions["V-10006"] == "MANAGER_REVIEW"
    assert actions["V-10005"] == "WHOLESALE_OR_LOSS_MINIMIZATION_REVIEW"


# --- 27. no price-publishing symbol introduced ---------------------------------------


def test_no_price_publishing_symbol_in_new_surfaces():
    files = [
        Path(__file__).resolve().parents[2] / "src" / "pricing_agent" / "agents" / "aging_answer.py",
        VIEWS / "assistant_home.py",
    ]
    for path in files:
        src = path.read_text(encoding="utf-8")
        for banned in ("publish_vehicle_price", "save_pricing_decision", "write_client", "WriteClient"):
            assert banned not in src
