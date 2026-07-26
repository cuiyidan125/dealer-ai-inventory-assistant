"""Dealer-friendly terminology pass — presentation only.

Two things at once: the copy is genuinely dealer-friendly and consistent (business meaning
before the statistic, P-terms parenthesised, no raw snake_case or enum in the default view,
raw codes preserved in audit), and **nothing the engine produced changed** — same numbers,
same selected/excluded vehicles, same recommended plan.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pricing_agent.mcp_clients import MockTransport
from pricing_agent.skills import inventory_portfolio, promotion_planner, single_vehicle
from pricing_agent.views import terminology as T
from pricing_agent.workflows.improve_aging import ImproveAgingRequest, run_improve_aging

REPO = Path(__file__).resolve().parents[2]
VIEWS = REPO / "src" / "pricing_agent" / "views"
AS_OF = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)

VIEW_FILES = ["assistant_home.py", "dashboard.py", "vehicle_detail.py", "promotion.py",
              "improve_aging.py"]


# --- 1–5. numbers, selection, plan, warnings, approvals unchanged ---------------------


def test_numbers_and_selection_and_plan_unchanged():
    r = run_improve_aging(MockTransport(as_of=AS_OF), ImproveAgingRequest(
        target_utilization=0.70, event_requested=True, event_id="EVT-SUMMER-2026",
        event_name="Summer Clearance", available_events=("Summer Clearance", "Labor Day Sales Event")))
    assert list(r.selection.candidate_ids) == \
        ["V-10005", "V-10002", "V-10012", "V-10006", "V-10019", "V-10004", "V-10013",
         "V-10003", "V-10014", "V-10015", "V-10016", "V-10017", "V-10020", "V-10008", "V-10001"]
    assert [e.vehicle_id for e in r.selection.exclusions] == \
        ["V-10007", "V-10009", "V-10010", "V-10011", "V-10018"]
    assert r.promotion_result["recommended_plan"]["plan_type"] == "CAPACITY_FIRST"
    assert r.portfolio_summary["required_unit_reduction"] == 1
    assert round(r.portfolio_summary["probability_target_achieved"], 4) == 0.676


def test_price_and_portfolio_numbers_unchanged():
    sv = single_vehicle.analyze("V-10001", MockTransport(as_of=AS_OF))
    strat = sv["recommended_strategy"]["strategy"]
    scen = next(s for s in sv["pricing_scenarios"] if s["strategy"] == strat)
    assert sv["vehicle"]["current_list_price"] == 28995.0
    assert scen["proposed_list_price"] == 29195.0
    assert scen["additional_days_to_sale"]["p50"] == 30.0
    assert sv["break_even_analysis"]["current_accounting_break_even"] == 26148.0
    pf = inventory_portfolio.analyze(MockTransport(as_of=AS_OF))
    assert pf["capacity_position"]["current_inventory"] == 20
    assert pf["financial_risk"]["units_below_break_even"] == 4


def test_warning_and_approval_results_unchanged():
    sv = single_vehicle.analyze("V-10005", MockTransport(as_of=AS_OF))
    # V-10005 (aged BMW) still carries its approvals from the skill — the copy layer only relabels.
    assert sv["approvals_required"]
    types = {a["approval_type"] for a in sv["approvals_required"]}
    # Every approval type has a friendly label and a "why".
    for t in types:
        assert T.approval_label(t) == "Manager review required"
        assert T.approval_why(t)


# --- 6–8. no snake_case / raw codes in default view; codes in audit -------------------

# Display strings rendered to the dealer. The negative lookbehind for `T` excludes
# `T.metric("units_on_lot")` — that argument is a terminology-lookup *key*, not a label; it
# resolves to "Units on lot". Streamlit calls (st.metric, c1.metric, column_config keys) are
# the real display strings.
_DISPLAY_CALL = re.compile(
    r'(?<!T)\.(?:metric|subheader|header|title)\(\s*"([^"]+)"'
    r'|column_config\s*=\s*\{[^}]*?"([^"]+)"\s*:'
)
_SNAKE = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")
_ENUM = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")

# Raw reason/warning/approval codes that must not appear as primary display text.
RAW_CODES = set(T.SELECTION_LABELS) | set(T.EXCLUSION_LABELS) | set(T.WARNING) | set(T.APPROVAL)


def _default_display_strings(source: str) -> list[str]:
    """Display strings outside any `expander(... audit ... / technical / trace)` block is hard
    to scope by regex, so we take a simpler, strict rule: no raw code token may appear in ANY
    double-quoted display label produced by .metric/.subheader/column_config keys."""
    out = []
    for m in _DISPLAY_CALL.finditer(source):
        out.append(m.group(1) or m.group(2))
    return [s for s in out if s]


@pytest.mark.parametrize("name", VIEW_FILES)
def test_default_labels_have_no_raw_enum_codes(name):
    for label in _default_display_strings((VIEWS / name).read_text(encoding="utf-8")):
        assert not _ENUM.search(label), f"{name}: raw enum in label {label!r}"
        assert label not in RAW_CODES, f"{name}: raw code as label {label!r}"


@pytest.mark.parametrize("name", VIEW_FILES)
def test_table_columns_have_no_snake_case(name):
    for label in _default_display_strings((VIEWS / name).read_text(encoding="utf-8")):
        # Allow snake_case only inside an explicit audit expander context — but display labels
        # here are metric/column headers, which must never be snake_case.
        assert not _SNAKE.search(label), f"{name}: snake_case in label {label!r}"


def test_raw_reason_codes_remain_available_in_audit():
    # The raw reason-code expander now lives once in the shared review panel, which every
    # warnings-bearing view renders through. Assert the expander exists at that single source,
    # and that each of those views routes its warnings through the shared panel.
    panel_src = (VIEWS / "review_panel.py").read_text(encoding="utf-8")
    assert "View technical reason codes" in panel_src, "the shared review panel lost its audit expander"
    for name in ("vehicle_detail.py", "dashboard.py", "promotion.py", "improve_aging.py"):
        src = (VIEWS / name).read_text(encoding="utf-8")
        assert "render_review_panel" in src, f"{name} no longer surfaces warnings via the shared panel"


# --- 9–14. business-before-P and P-definitions ---------------------------------------

# Primary labels that include a P-term must lead with words and parenthesise the P-term.
_P_LABEL = re.compile(r"(P10|P50|P90)")


@pytest.mark.parametrize("name", VIEW_FILES)
def test_business_meaning_before_percentiles(name):
    for label in _default_display_strings((VIEWS / name).read_text(encoding="utf-8")):
        if _P_LABEL.search(label):
            # The P-term must be parenthesised and not be the first token.
            assert re.search(r"\((?:P10|P50|P90)\)", label), \
                f"{name}: P-term not parenthesised in {label!r}"
            assert not label.strip().startswith(("P10", "P50", "P90")), \
                f"{name}: label leads with a percentile: {label!r}"


def test_glossary_defines_the_three_estimates_correctly():
    terms = dict(T.GLOSSARY)
    p50 = terms["Expected estimate (P50)"].lower()
    p90 = terms["Conservative estimate (P90)"].lower()
    p10 = terms["Downside estimate (P10)"].lower()
    assert "median" in p50
    assert "cautious" in p90 or "conservative" in p90
    # P90 is explicitly not a guarantee and not the worst case.
    assert "not a guarantee" in p90
    assert "not the worst" in p90
    assert "downside" in p10


def test_p50_not_called_average_and_p90_not_positively_worst_or_guaranteed():
    joined = " ".join(d for _, d in T.GLOSSARY).lower()
    # "worst" and "guarantee" appear only inside their negated P90 phrasing.
    assert "not the worst possible case" in joined
    assert re.search(r"(?<!not the )worst possible", joined) is None
    assert joined.count("guarantee") == 1 and "not a guarantee" in joined
    # P50 is called the median, never an average.
    assert "median" in dict(T.GLOSSARY)["Expected estimate (P50)"].lower()


# --- 15–18. no new metric / no MDS / no logic / no publishing ------------------------


@pytest.mark.parametrize("name", VIEW_FILES + ["terminology.py", "glossary.py"])
def test_no_market_days_supply_introduced(name):
    src = (VIEWS / name).read_text(encoding="utf-8").lower()
    assert "market days supply" not in src
    assert "days_supply" not in src
    assert "mds" not in re.findall(r"\bmds\b", src)


@pytest.mark.parametrize("name", VIEW_FILES + ["terminology.py", "glossary.py"])
def test_no_price_publishing_introduced(name):
    src = (VIEWS / name).read_text(encoding="utf-8")
    for banned in ("publish_vehicle_price", "save_pricing_decision", "write_client", "WriteClient"):
        assert banned not in src


def test_terminology_and_copy_modules_do_no_calculation():
    for name in ("terminology.py", "glossary.py"):
        src = (VIEWS / name).read_text(encoding="utf-8")
        for banned in ("np.percentile", ".percentile(", "simulate(", "np.mean"):
            assert banned not in src
        for mod in ("pricing_agent.domain", "pricing_agent.simulation"):
            assert f"import {mod}" not in src and f"from {mod}" not in src


# --- 19. consistency: one term, one source ------------------------------------------


def test_plan_and_strategy_and_state_names_come_from_terminology():
    assert T.plan_name("CAPACITY_FIRST") == "Prioritize freeing inventory space"
    assert T.plan_name("MARGIN_PROTECT") == "Prioritize profit protection"
    assert T.strategy_name("MAXIMIZE_GROSS") == "Protect profit"
    assert T.state_label("ROUTED_AND_EXECUTED") == "Analysis completed"
    assert T.feasibility_label("NOT_ACHIEVABLE") == "Unlikely under the current constraints"


def test_views_import_the_centralized_terminology():
    for name in VIEW_FILES:
        src = (VIEWS / name).read_text(encoding="utf-8")
        assert "terminology" in src, f"{name} does not use the centralized terminology module"


def test_audit_label_humanizes_ids():
    assert T.audit_label("request_id") == "Request ID"
    assert T.audit_label("simulation_id") == "Simulation ID"
    assert T.audit_label("workflow_id") == "Workflow ID"
