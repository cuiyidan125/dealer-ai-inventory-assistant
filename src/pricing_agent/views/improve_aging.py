"""Improve Aging Inventory — evidence workspace. Phase 5, polished in Phase 5.1.

The page tells a story in the order a dealer or a product interviewer reads it: what is the
problem, can the target be met, what does the Agent recommend, what should I do next, which
vehicles and why, then the supporting evidence, warnings, five-step summary, and the full
audit trace at the bottom.

If the assistant routed a specific aging request, its stored result is shown; otherwise a
reproducible scenario (Summer Clearance, 70%, injected clock) runs by default.

Nothing here computes a number. Every figure is read from a skill result carried through the
orchestration; the dealer-facing labels and the "what to do next" actions come from
`improve_aging_copy`, which maps codes to words and counts items the workflow already decided.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from pricing_agent.mcp_clients import MockTransport, VautoClient
from pricing_agent.views import improve_aging_copy as copy
from pricing_agent.views import terminology as T
from pricing_agent.views.glossary import render_glossary
from pricing_agent.views.workflow_copy import render_workflow_header
from pricing_agent.workflows.context import WorkflowContext
from pricing_agent.workflows.improve_aging import (
    ImproveAgingRequest,
    WorkflowState,
    run_improve_aging,
)

AS_OF = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)

DEMO_REQUEST = ImproveAgingRequest(
    target_utilization=0.70,
    event_requested=True,
    event_id="EVT-SUMMER-2026",
    event_name="Summer Clearance",
    available_events=("Summer Clearance", "Labor Day Sales Event"),
)

# The five business steps the default summary shows, mapped from the technical trace.
BUSINESS_STEPS = (
    ("Review the lot", "PORTFOLIO_FORECAST", "inventory-portfolio-forecast"),
    ("Identify vehicles requiring action", "CANDIDATE_SELECTION", None),
    ("Evaluate pricing options", "SINGLE_VEHICLE_VALUATION", "single-vehicle-valuation"),
    ("Build the sale-event plan", "PROMOTION_PLAN", "dealer-event-promotion-planner"),
    ("Create the dealer action plan", "CONSOLIDATE", None),
)

# Recommended-plan card ordering: conservative → recommended → aggressive.
PLAN_STANCE = {"MARGIN_PROTECT": "Conservative", "BALANCED": "Balanced", "CAPACITY_FIRST": "Aggressive"}

# Actions that ask the dealer to do something now. Everything else an analysed vehicle can be
# assigned (NO_ACTION, PROTECT_PRICE) means "no immediate action". This is a display grouping of
# the classification the workflow already made — it does not reclassify anything.
_IMMEDIATE_ACTIONS = frozenset({
    "REPRICE_NOW", "EVENT_PROMOTION", "MANAGER_REVIEW",
    "WHOLESALE_OR_LOSS_MINIMIZATION_REVIEW",
})


def md(text: str) -> str:
    return str(text).replace("$", r"\$")


def _pct(value) -> str:
    return f"{value:.0%}" if isinstance(value, (int, float)) else "—"


def _usd(value) -> str:
    return f"\\${value:,.0f}" if isinstance(value, (int, float)) else "—"


@st.cache_data(show_spinner="Running the Improve Aging orchestration…")
def _run_demo(target: float, event_id: str | None):
    request = ImproveAgingRequest(
        target_utilization=target,
        event_requested=event_id is not None,
        event_id=event_id,
        event_name="Summer Clearance" if event_id else None,
        available_events=DEMO_REQUEST.available_events,
    )
    return run_improve_aging(MockTransport(as_of=AS_OF), request)


@st.cache_data(show_spinner=False)
def _inventory_facts(as_of: datetime) -> dict:
    """vehicle_id → static facts (age, price, status) for excluded-vehicle display. A fixture
    read, not a calculation."""
    data = VautoClient(MockTransport(as_of=as_of)).get_dealer_inventory().data or []
    return {v["vehicle_id"]: v for v in data}


def render_improve_aging(workflow_context: WorkflowContext | None = None) -> None:
    render_workflow_header(
        workflow_context,
        fallback_title="Improve Aging Inventory",
        fallback_subtitle=(
            "Coordinate portfolio forecasting, single-vehicle diagnostics, and event "
            "promotion planning against the aged cohort."
        ),
    )

    result = st.session_state.get("improve_aging_result")
    if result is None:
        result = _run_demo(0.70, "EVT-SUMMER-2026")

    facts = _inventory_facts(AS_OF)

    # Narrative order: situation → recommendation → impact → actions → evidence → audit.
    _executive_summary(result)
    _achievability(result)
    _next_steps(result)
    _recommended_plan(result)
    _vehicles_requiring_action(result)
    _vehicles_excluded(result, facts)
    _why_these_vehicles(result)
    _plan_comparison(result)
    _warnings_and_approvals(result)
    _five_step_summary(result)
    render_glossary()
    _full_trace(result)
    _disclosure()


# --- 1. executive summary -------------------------------------------------------------


def executive_metrics(result) -> dict:
    """The five executive-summary values, every one read straight from the result. Kept as a
    pure function so a test can assert it invents nothing."""
    d = result.portfolio_summary or {}
    promotion = result.promotion_result
    return {
        "current_utilization": d.get("current_utilization"),
        "target_utilization": d.get("target_utilization"),
        "required_unit_reduction": d.get("required_unit_reduction"),
        "candidate_count": len(result.selection.candidates) if result.selection else 0,
        "target_status": (promotion["feasibility"]["status"] if promotion else "NO_EVENT"),
        "probability_target_achieved": d.get("probability_target_achieved"),
    }


def reconciled_counts(result) -> dict:
    """Reconcile the three counts the workspace shows, all read from the existing result.

    The bug this addresses is conflation, not classification: the exec summary counted analysed
    vehicles, the action table counted a filtered subset, and the approvals metric counted
    approval *records*. This helper derives every count from the same result so the three agree.
    Nothing here decides an action or recomputes a number — it only groups and counts what the
    workflow already produced.
    """
    analysed_ids = [e.vehicle_id for e in result.vehicle_evidence]
    action_by_id = {a["vehicle_id"]: a["recommended_action"] for a in result.consolidated_actions}
    immediate = [vid for vid in analysed_ids if action_by_id.get(vid) in _IMMEDIATE_ACTIONS]
    no_immediate = [vid for vid in analysed_ids if vid not in set(immediate)]
    review_vehicle_ids = sorted(
        {a.get("vehicle_id") for a in result.approvals_required if a.get("vehicle_id")}
    )
    return {
        "analysed": len(analysed_ids),
        "analysed_ids": analysed_ids,
        "immediate_action": len(immediate),
        "immediate_ids": immediate,
        "no_immediate_action": len(no_immediate),
        "no_immediate_ids": no_immediate,
        "review_vehicles": len(review_vehicle_ids),
        "review_vehicle_ids": review_vehicle_ids,
        "review_items": len(result.approvals_required),
    }


def _executive_summary(result) -> None:
    m = executive_metrics(result)
    status = "No event" if m["target_status"] == "NO_EVENT" else T.feasibility_label(m["target_status"])

    c = st.columns(5)
    c[0].metric("Current lot capacity used", _pct(m["current_utilization"]))
    c[1].metric("Target lot capacity",
                _pct(m["target_utilization"]) if m["target_utilization"] is not None else "—")
    c[2].metric("Vehicles to sell or release",
                m["required_unit_reduction"] if m["required_unit_reduction"] is not None else "—")
    rc = reconciled_counts(result)
    c[3].metric("Aging vehicles analysed", rc["analysed"])
    prob = m["probability_target_achieved"]
    c[4].metric("Target likelihood", status,
                _pct(prob) + " likely" if isinstance(prob, (int, float)) else None,
                delta_color="off")

    if rc["no_immediate_action"]:
        st.caption(md(
            f"Of {rc['analysed']} analysed, **{rc['immediate_action']}** need immediate action "
            f"and **{rc['no_immediate_action']}** have no immediate action "
            "(eligible for a sale event)."
        ))
    else:
        st.caption(md(
            f"All {rc['analysed']} analysed vehicles need immediate action."
        ))

    statement = copy.recommendation_statement(result)
    if result.state is WorkflowState.TARGET_NOT_ACHIEVABLE:
        st.error(md(f"**{statement}**"), icon="🚫")
    elif result.state in (WorkflowState.NEEDS_CLARIFICATION, WorkflowState.NO_SAFE_ACTIONS):
        st.warning(md(f"**{statement}**"), icon="❓")
    else:
        st.success(md(f"**{statement}**"), icon="✅")


# --- 2. achievability -----------------------------------------------------------------


def _achievability(result) -> None:
    if result.state is not WorkflowState.TARGET_NOT_ACHIEVABLE or not result.promotion_result:
        return
    st.subheader("Why the target is not achievable")
    f = result.promotion_result["feasibility"]
    d = result.portfolio_summary
    required = d.get("required_unit_reduction")
    achievable = f.get("p50_achievable_incremental_units")
    gap = None
    if isinstance(required, (int, float)) and isinstance(achievable, (int, float)):
        gap = required - achievable   # subtraction of two result figures, for display only

    c = st.columns(3)
    c[0].metric("Units the target needs", required if required is not None else "—")
    c[1].metric("The safe plan can release", f"{achievable:.0f}" if isinstance(achievable, (int, float)) else "—")
    c[2].metric("Remaining gap", f"{gap:.0f}" if gap is not None else "—")

    reasons = _gap_reasons(result)
    if reasons:
        st.markdown("**Why the gap exists**")
        for line in reasons:
            st.markdown(f"- {line}")

    alts = f.get("alternatives", [])
    if alts:
        st.markdown("**What would close it**")
        for a in alts:
            st.markdown(f"- {_alternative_line(a)}")
    st.caption("All figures from the promotion simulation "
               f"`{d.get('outcome_simulation_id')}` and the portfolio diagnosis.")


def _gap_reasons(result) -> list[str]:
    """Only reasons the actual result supports."""
    reasons: list[str] = []
    sel = result.selection
    if sel:
        protected = [e for e in sel.exclusions
                     if any(r in sel.PROTECTED_REASONS for r in e.reason_codes)]
        recently = [e for e in protected if "RECENTLY_ACQUIRED" in e.reason_codes]
        campaign = [e for e in protected if "ALREADY_ASSIGNED_TO_CAMPAIGN" in e.reason_codes]
        if recently:
            reasons.append(f"{len(recently)} recently-acquired vehicle(s) are protected from discounting.")
        if campaign:
            reasons.append(f"{len(campaign)} vehicle(s) are already committed to another campaign.")
    # Price-floor: candidates that need approval because they are below break-even.
    review = sum(1 for a in result.consolidated_actions
                 if a["recommended_action"] in ("MANAGER_REVIEW", "WHOLESALE_OR_LOSS_MINIMIZATION_REVIEW"))
    if review:
        reasons.append(f"{review} aged vehicle(s) are at or below their price floor, so they "
                       "cannot be discounted safely.")
    if result.promotion_result:
        codes = {w["code"] for w in result.promotion_result.get("warnings", [])}
        if "PRICE_CANNIBALIZATION_RISK" in codes:
            reasons.append("Discounting more units risks cannibalizing sales of the others.")
        window = result.promotion_result["feasibility"].get("event_duration_days")
        if isinstance(window, (int, float)):
            reasons.append(f"The event window is only {window} days.")
    if result.portfolio_result:
        pcodes = {w["code"] for w in result.portfolio_result.get("warnings", [])}
        if "INBOUND_CAPACITY_CONFLICT" in pcodes:
            reasons.append("Committed inbound units add capacity pressure on top of the aged cohort.")
    return reasons


def _alternative_line(a: dict) -> str:
    option = a.get("option")
    change, unit = a.get("quantified_change"), a.get("unit")
    prob = a.get("resulting_probability_target_achieved")
    prob_txt = f" → {prob:.0%} likely" if isinstance(prob, (int, float)) else ""
    label = copy._ALTERNATIVE_COPY.get(option, str(option).replace("_", " ").lower())
    if unit == "DAYS":
        return f"Extend the window to {change:.0f} days{prob_txt}."
    if unit == "RATIO":
        return f"Revise the target to {change:.0%}{prob_txt}."
    if unit == "UNITS":
        return f"Wholesale {change:.0f} unit(s){prob_txt}."
    return f"{label.capitalize()}{prob_txt}."


# --- 3. what should I do next? --------------------------------------------------------


def _next_steps(result) -> None:
    steps = copy.next_steps(result)
    if not steps:
        return
    st.subheader("What should I do next?")
    for i, step in enumerate(steps, start=1):
        st.markdown(f"**{i}. {step.title}**")
        st.caption(md(step.detail))


# --- 4. recommended plan --------------------------------------------------------------


def _recommended_plan(result) -> None:
    promotion = result.promotion_result
    if promotion is None:
        return
    st.subheader("Recommended sale-event approach")
    rec = promotion["recommended_plan"]
    plan = next((p for p in promotion["plans"] if p["plan_type"] == rec["plan_type"]), None)
    if plan is None:
        return
    o = plan["outcomes"]
    d = result.portfolio_summary

    st.markdown(f"### {T.plan_name(rec['plan_type'])}")
    st.caption(T.plan_trade_off(rec["plan_type"]))
    with st.expander("View technical reason codes"):
        st.caption("Rationale codes: " + ", ".join(f"`{c}`" for c in rec.get("rationale_codes", [])))

    c = st.columns(4)
    c[0].metric("Expected ending inventory (P50)", f"{o['ending_inventory']['p50']:.0f}",
                f"lot capacity used {_pct(o['ending_utilization']['p50'])}", delta_color="off")
    c[1].metric("Likelihood of reaching the target", _pct(o["probability_target_achieved"]))
    c[2].metric("Expected total front-end gross (P50)", _usd(o["total_front_end_gross"]["p50"]))
    rc = reconciled_counts(result)
    # Vehicle-based by default; the raw review-condition count lives in "View approval details".
    c[3].metric("Vehicles requiring review", rc["review_vehicles"])

    c2 = st.columns(3)
    hs = (d.get("expected_holding_cost_savings") or {}).get("p50")
    ds = (d.get("expected_depreciation_savings") or {}).get("p50")
    c2[0].metric("Expected holding-cost savings (P50)", _usd(hs) if hs is not None else "Not available")
    c2[1].metric("Expected depreciation savings (P50)", _usd(ds) if ds is not None else "Not available")
    c2[2].metric("Dealer-funded discount", _usd(plan["totals"]["total_dealer_funded"]))

    if result.state is WorkflowState.TARGET_NOT_ACHIEVABLE:
        st.caption("This is the most aggressive safe approach — it still does **not** reach the "
                   "target, and no plan guarantees sales. See the gap above.")
    if result.held_from_promotion:
        st.caption("🛡️ Held back from promotion by workflow protection: "
                   + ", ".join(result.held_from_promotion))


# --- 5. vehicles requiring action -----------------------------------------------------


def _vehicles_requiring_action(result) -> None:
    st.subheader("Analysed aging vehicles")
    evidence_by_id = {e.vehicle_id: e for e in result.vehicle_evidence}
    analysed_ids = set(evidence_by_id)
    # Show every vehicle that was analysed in depth, in the workflow's own attention order —
    # including the ones with no immediate action, which the old filter dropped. The excluded
    # and protected vehicles keep their own section below.
    shown = [a for a in result.consolidated_actions if a["vehicle_id"] in analysed_ids]
    if not shown:
        st.caption("No vehicle was analysed in depth in this run.")
        return

    rows = []
    for a in shown:
        ev = evidence_by_id.get(a["vehicle_id"])
        res = ev.result if ev else {}
        scenario = {}
        if res:
            strat = res.get("recommended_strategy", {}).get("strategy")
            scenario = next((s for s in res.get("pricing_scenarios", [])
                             if s["strategy"] == strat), {})
        be = res.get("break_even_analysis", {})
        days = (scenario.get("additional_days_to_sale") or {})
        immediate = a["recommended_action"] in _IMMEDIATE_ACTIONS
        # An analysed candidate with no immediate action was still worth surfacing: without a sale
        # event it holds its strategy, but it is eligible for one. This is a display label for the
        # existing NO_ACTION/PROTECT_PRICE classification, not a reclassification.
        action_text = (copy.action_label(a["recommended_action"]) if immediate
                       else "No immediate action — eligible for a sale event")
        rows.append({
            "Vehicle": ev.description if ev else a["vehicle_id"],
            "Attention": "Immediate action" if immediate else "No immediate action",
            "Recommended action": action_text,
            "Why it was analysed": ", ".join(copy.selection_label(c) for c in a["reason_codes"]),
            "Current asking price": a.get("current_price"),
            "Expected days to sale (P50)": days.get("p50"),
            "Conservative days to sale (P90)": days.get("p90"),
            "Break-even price": be.get("current_accounting_break_even"),
            "Approval needed": "Yes" if a["approvals_required"] else "No",
        })
    rc = reconciled_counts(result)
    st.caption(
        f"All {rc['analysed']} vehicles analysed in depth — "
        f"{rc['immediate_action']} need immediate action, "
        f"{rc['no_immediate_action']} have no immediate action. Each row is a separate "
        "single-vehicle analysis, shown side by side, never combined."
    )
    st.dataframe(
        pd.DataFrame(rows), hide_index=True,
        column_config={
            "Current asking price": st.column_config.NumberColumn(format="$%d"),
            "Break-even price": st.column_config.NumberColumn(format="$%d"),
            "Expected days to sale (P50)": st.column_config.NumberColumn(format="%.0f"),
            "Conservative days to sale (P90)": st.column_config.NumberColumn(format="%.0f"),
        },
    )
    with st.expander("Raw reason codes & per-vehicle simulation ids (audit)"):
        st.dataframe(
            pd.DataFrame([
                {"Vehicle": (evidence_by_id.get(a["vehicle_id"]).description
                             if a["vehicle_id"] in evidence_by_id else a["vehicle_id"]),
                 "Recommended action": a["recommended_action"],
                 "Reason codes": ", ".join(a["reason_codes"]),
                 "request_id": a["referenced_request_id"] or "—",
                 "simulation_id": a["referenced_simulation_id"] or "—"}
                for a in shown
            ]),
            hide_index=True,
        )
    st.caption("Each vehicle is a separate single-vehicle simulation, shown side by side — "
               "never combined.")


# --- 6. vehicles protected or excluded ------------------------------------------------


def _vehicles_excluded(result, facts: dict) -> None:
    st.subheader("Vehicles protected or excluded")
    if result.selection is None or not result.selection.exclusions:
        st.caption("No vehicles were excluded.")
        return
    st.caption("Vehicles held back from action, each with the reason and whether it is a "
               "safety rule, a business rule, or a data limitation.")
    rows = []
    for e in result.selection.exclusions:
        fact = facts.get(e.vehicle_id, {})
        rows.append({
            "Vehicle": e.description,
            "Why protected or excluded": "; ".join(copy.exclusion_label(c) for c in e.reason_codes),
            "Days on lot": fact.get("days_in_inventory"),
            "Status": (fact.get("status") or "ACTIVE").title(),
            "Rule type": copy.exclusion_category(e.reason_codes),
        })
    st.dataframe(
        pd.DataFrame(rows), hide_index=True,
        column_config={"Days on lot": st.column_config.NumberColumn(format="%.0f")},
    )
    with st.expander("View technical reason codes"):
        st.dataframe(
            pd.DataFrame([{"Vehicle": e.description, "Codes": ", ".join(e.reason_codes)}
                         for e in result.selection.exclusions]),
            hide_index=True,
        )


# --- 7. why these vehicles? -----------------------------------------------------------


def _why_these_vehicles(result) -> None:
    if result.selection is None or not result.selection.candidates:
        return
    st.subheader("Why these vehicles?")
    categories = copy.candidate_categories(result.selection.candidates)
    for name, description, count in categories:
        st.markdown(f"- **{name}** ({count}) — {description}")
    with st.expander("Detailed candidate ranking & raw reason codes"):
        st.dataframe(
            pd.DataFrame([
                {"Vehicle": c.description, "Days": c.days_in_inventory,
                 "Risk score": round(c.risk_score, 1),
                 "Reason codes": ", ".join(c.reason_codes)}
                for c in result.selection.candidates
            ]),
            hide_index=True,
            column_config={"Risk score": st.column_config.ProgressColumn(
                format="%.0f", min_value=0, max_value=100)},
        )


# --- 8. plan comparison ---------------------------------------------------------------


def _plan_comparison(result) -> None:
    promotion = result.promotion_result
    if promotion is None:
        return
    st.subheader("Compare sale-event approaches")
    st.caption("Each approach makes a different trade-off between protecting profit and freeing "
               "lot space. A plan improves the odds; it does not guarantee sales.")
    rec = promotion["recommended_plan"]["plan_type"]
    rows = []
    for plan in promotion["plans"]:
        o = plan["outcomes"]
        rows.append({
            "Sale-event approach": T.plan_name(plan["plan_type"]),
            "Vehicles in the event": plan["totals"]["vehicle_count"],
            "Dealer-funded discount": plan["totals"]["total_dealer_funded"],
            "Expected additional sales (P50)": o["incremental_units_sold"]["p50"],
            "Target likelihood": o["probability_target_achieved"],
            "Recommended": "★" if plan["plan_type"] == rec else "",
        })
    st.dataframe(
        pd.DataFrame(rows), hide_index=True,
        column_config={
            "Dealer-funded discount": st.column_config.NumberColumn(format="$%d"),
            "Target likelihood": st.column_config.NumberColumn(format="%.0f%%"),
        },
    )


# --- 9. warnings and approvals --------------------------------------------------------


def _warnings_and_approvals(result) -> None:
    st.subheader("What to review before pricing changes")
    if result.approvals_required:
        rc = reconciled_counts(result)
        manager_review = sum(1 for a in result.consolidated_actions
                             if a["recommended_action"] == "MANAGER_REVIEW")
        # Default copy is vehicle-based. The raw review-condition record count (and the raw
        # records themselves) move into "View approval details" below — showing it here by
        # default reads as if the dealer must complete that many separate approvals.
        st.caption(
            f"**{rc['review_vehicles']}** vehicle(s) require review before any pricing action — "
            f"**{manager_review}** assigned to manager review, the rest flagged for wholesale / "
            "loss-minimization review."
        )
        by_vehicle: dict[str, list[str]] = {}
        for a in result.approvals_required:
            kind = a.get("approval_type") or a.get("type") or "REVIEW"
            by_vehicle.setdefault(a.get("vehicle_id") or "—", []).append(kind)
        st.dataframe(
            pd.DataFrame([
                {"Vehicle": vid,
                 "Why a review is needed": "; ".join(dict.fromkeys(T.approval_why(k) for k in kinds))}
                for vid, kinds in by_vehicle.items()
            ]),
            hide_index=True,
        )
        with st.expander("View approval details"):
            st.markdown(
                f"- **Vehicles requiring review:** {rc['review_vehicles']}\n"
                f"- **Vehicles assigned to manager review:** {manager_review}\n"
                f"- **Review conditions triggered:** {rc['review_items']}"
            )
            st.caption(
                "“Review conditions triggered” counts the individual approval-condition records "
                "behind these vehicles — not separate dealer decisions. Each raw record, with the "
                "vehicle and simulation it came from, is listed below."
            )
            st.dataframe(
                pd.DataFrame([
                    {"Vehicle": a.get("vehicle_id") or "—",
                     "Review condition (raw code)": a.get("approval_type") or a.get("type") or "REVIEW",
                     "Source skill": a.get("source") or "—"}
                    for a in result.approvals_required
                ]),
                hide_index=True,
            )
    else:
        st.caption("No manager reviews required.")

    labels = sorted(
        {T.warning_label(w["code"]) for ev in result.vehicle_evidence for w in ev.warnings}
        | {T.warning_label(w["code"]) for w in (result.promotion_result or {}).get("warnings", [])}
        | {T.warning_label(w["code"]) for w in (result.portfolio_result or {}).get("warnings", [])}
    )
    if labels:
        st.markdown("**What to review:** " + " · ".join(labels))
    codes = sorted(
        {w["code"] for ev in result.vehicle_evidence for w in ev.warnings}
        | {w["code"] for w in (result.promotion_result or {}).get("warnings", [])}
        | {w["code"] for w in (result.portfolio_result or {}).get("warnings", [])}
    )
    if codes:
        with st.expander("View technical reason codes"):
            st.caption("Warning codes: " + ", ".join(f"`{c}`" for c in codes))


# --- 10. five-step workflow summary ---------------------------------------------------


def _five_step_summary(result) -> None:
    st.subheader("How the Agent got here")
    counts = result.skill_invocation_counts
    ran = set(result.execution_order)
    rows = []
    for i, (label, step_name, skill) in enumerate(BUSINESS_STEPS, start=1):
        did_run = step_name in ran
        rows.append({
            "Step": f"{i}. {label}",
            "Status": "Done" if did_run else "Skipped",
            "Skill": skill or "—",
            "Summary": _step_summary(step_name, result, counts),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True)


def _step_summary(step_name: str, result, counts: dict) -> str:
    d = result.portfolio_summary or {}
    if step_name == "PORTFOLIO_FORECAST":
        return (f"{d.get('current_inventory', '—')} units, "
                f"{_pct(d.get('current_utilization'))} utilized, "
                f"{_pct(d.get('aged_concentration_pct'))} over 90 days")
    if step_name == "CANDIDATE_SELECTION":
        sel = result.selection
        return (f"{len(sel.candidates)} selected, {len(sel.exclusions)} excluded"
                if sel else "—")
    if step_name == "SINGLE_VEHICLE_VALUATION":
        return f"{counts.get('single-vehicle-valuation', 0)} vehicle(s) analysed in depth"
    if step_name == "PROMOTION_PLAN":
        if result.promotion_result:
            return (f"{result.promotion_result['recommended_plan']['plan_type'].replace('_', ' ').title()} "
                    f"recommended · {_pct(d.get('probability_target_achieved'))} likely")
        return "Not run (no event supplied)"
    if step_name == "CONSOLIDATE":
        n = len(result.consolidated_actions)
        return f"{n} vehicle action(s) grouped"
    return "—"


# --- 11. full trace -------------------------------------------------------------------


def _full_trace(result) -> None:
    with st.expander("View full workflow execution trace"):
        st.caption(f"workflow_id `{result.workflow_id}` · type IMPROVE_AGING_INVENTORY")
        st.dataframe(
            pd.DataFrame([
                {"#": t.step_number, "Step": t.step_name, "Skill": t.skill_called or "—",
                 "request_id": t.request_id or "—", "simulation_id": t.simulation_id or "—",
                 "Start": t.start_timestamp, "End": t.end_timestamp,
                 "Status": t.status, "Warnings": len(t.warnings),
                 "Error": t.error or ""}
                for t in result.trace
            ]),
            hide_index=True,
        )
        if result.unavailable:
            st.caption("Unavailable in this run: " + ", ".join(result.unavailable))


# --- 12. disclosure -------------------------------------------------------------------


def _disclosure() -> None:
    st.divider()
    st.info(
        "**Synthetic data · prototype simulation · human review required.** The vehicles and "
        "market data are mocked; forecasts are a configured prototype simulation, not a trained "
        "prediction; every recommendation is a decision-support suggestion that a manager must "
        "review; and **no price is ever published from this application.**",
        icon="ℹ️",
    )
