"""The assistant orchestrator: question in, executed workflow out. Phase 4, no model.

This is the layer that turns *"what should I price the 2020 Ford F-150 XLT?"* into a
resolved vehicle, a single skill invocation, and a concise result — deterministically, from
repository mock data, with no LLM anywhere in the path.

The flow is fixed:

    text → route → (resolve) → invoke one skill → concise summary → response

and the response is one of six honest states. What the orchestrator will **not** do is as
important as what it does:

* It runs at most one skill. Improve Aging, which would coordinate three, returns
  WORKFLOW_NOT_YET_AVAILABLE instead of running anything.
* It generates no numbers. Every figure in a summary is copied straight out of the
  schema-valid skill result; the orchestrator only selects and labels.
* It resolves against real inventory and the real event calendar. An unmatched vehicle is
  NO_MATCH, never a fabricated one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from pricing_agent.agents.resolver import MatchResult, MatchStatus, resolve_vehicle
from pricing_agent.agents.router import RouteResult, route
from pricing_agent.mcp_clients import EventClient, MockTransport, VautoClient
from pricing_agent.policy.warnings import sort_by_severity
from pricing_agent.skills import inventory_portfolio, promotion_planner, single_vehicle
from pricing_agent.workflows.context import WorkflowContext
from pricing_agent.workflows.improve_aging import (
    ImproveAgingRequest,
    WorkflowState,
    run_improve_aging,
)


class AssistantState(str, Enum):
    ROUTED_AND_EXECUTED = "ROUTED_AND_EXECUTED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    NO_MATCH = "NO_MATCH"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    WORKFLOW_NOT_YET_AVAILABLE = "WORKFLOW_NOT_YET_AVAILABLE"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    # Added in Phase 5 for the Improve Aging orchestration.
    PARTIAL_RESULT = "PARTIAL_RESULT"
    TARGET_NOT_ACHIEVABLE = "TARGET_NOT_ACHIEVABLE"
    NO_SAFE_ACTIONS = "NO_SAFE_ACTIONS"


# The workflow the dealer opens to see the full analysis behind a summary.
WORKFLOW_URL: dict[WorkflowContext, str] = {
    WorkflowContext.PRICE_INVENTORY: "price-inventory",
    WorkflowContext.ACQUIRE_INVENTORY: "acquire-inventory",
    WorkflowContext.MERCHANDISE_INVENTORY: "merchandise-inventory",
    WorkflowContext.IMPROVE_AGING_INVENTORY: "improve-aging-inventory",
}


@dataclass(frozen=True)
class AssistantResponse:
    state: AssistantState
    message: str
    route: RouteResult
    workflow: WorkflowContext | None = None
    skill: str | None = None
    resolved_vehicle_id: str | None = None
    match: MatchResult | None = None
    candidates: tuple[dict, ...] = ()
    summary: dict = field(default_factory=dict)
    warnings: tuple[dict, ...] = ()
    result: dict | None = None
    target_url: str | None = None
    # The full ImproveAgingResult when the aging orchestration ran (Phase 5). Kept as a
    # plain object so this module does not import the workflow types at annotation time.
    improve_aging: object | None = None
    # A single-vehicle AI action plan when the orchestrator ran ("what should I do about this
    # vehicle?"). A VehicleAdvice, kept as a plain object for the same reason.
    advice: object | None = None

    @property
    def executed(self) -> bool:
        return self.state is AssistantState.ROUTED_AND_EXECUTED


# --- event resolution for the promotion workflow --------------------------------------

_PERCENT = re.compile(r"(\d{1,3})\s*(?:%|percent)")

# Did the request reference an event at all? Used to tell "no event, just diagnose" from
# "named an event I could not resolve" (which must clarify, never substitute).
_EVENT_REFERENCE = re.compile(
    r"\b(event|promotion|promo|campaign|clearance|sale|july\s*4|fourth\s*of\s*july|"
    r"independence|labor\s*day|memorial\s*day|black\s*friday|president'?s?\s*day|holiday)\b",
    re.IGNORECASE,
)


def parse_explicit_target(text: str) -> float | None:
    """A stated utilization target, or None when none was given (no silent default)."""
    if match := _PERCENT.search(text):
        value = int(match.group(1))
        if 0 < value <= 100:
            return value / 100.0
    return None

# Holiday phrases that identify an event by its date, with a representative (month, day).
# A holiday matches an event only when that date falls inside the event's window — so
# "July 4th" does not silently become a late-July clearance sale that happens to share the
# month. Being wrong about which event the dealer meant is worse than asking.
_HOLIDAYS: tuple[tuple[re.Pattern[str], tuple[int, int]], ...] = (
    (re.compile(r"july\s*4|fourth\s*of\s*july|independence"), (7, 4)),
    (re.compile(r"memorial\s*day"), (5, 25)),
    (re.compile(r"labor\s*day"), (9, 7)),
    (re.compile(r"black\s*friday"), (11, 27)),
    (re.compile(r"president'?s?\s*day"), (2, 16)),
)


def parse_target_utilization(text: str, default: float = 0.70) -> float:
    """A percentage from the text, or the conventional 70% target when none is stated."""
    if match := _PERCENT.search(text):
        value = int(match.group(1))
        if 0 < value <= 100:
            return value / 100.0
    return default


def resolve_event(text: str, events: list[dict]) -> dict | None:
    """Match a named or dated event against the calendar. No fuzzy guessing.

    Name match first (the dealer said "Summer Clearance"), then a holiday whose month lands
    inside an event window. Returns the calendar record or `None` — and `None` is a real
    answer: it drives a clarification that lists what events exist, never a fabricated one.
    """
    lowered = text.lower()

    for event in events:
        name = event.get("event_name", "")
        # Every significant word of the event name present in the request.
        words = [w for w in re.split(r"\W+", name.lower()) if len(w) > 2]
        if words and all(w in lowered for w in words):
            return event

    for pattern, (month, day) in _HOLIDAYS:
        if pattern.search(lowered):
            for event in events:
                if _within_window(event, month, day):
                    return event
    return None


def _within_window(event: dict, month: int, day: int) -> bool:
    """Whether (month, day) falls inside the event's [start_date, end_date], year-agnostic."""
    try:
        start = date.fromisoformat(event["start_date"])
        end = date.fromisoformat(event["end_date"])
    except (KeyError, ValueError):
        return False
    holiday = (month, day)
    return (start.month, start.day) <= holiday <= (end.month, end.day)


# --- summaries (selection only; every value is copied from the skill result) -----------


def _pricing_summary(result: dict) -> dict:
    strategy = result["recommended_strategy"]["strategy"]
    scenario = next(s for s in result["pricing_scenarios"] if s["strategy"] == strategy)
    return {
        "vehicle": result["vehicle"]["description"]
        if "description" in result["vehicle"]
        else f"{result['vehicle']['year']} {result['vehicle']['make']} "
        f"{result['vehicle']['model']} {result['vehicle']['trim']}".strip(),
        "current_list_price": result["vehicle"]["current_list_price"],
        "recommended_price": scenario["proposed_list_price"],
        "p50_days_to_sale": scenario["additional_days_to_sale"]["p50"],
        "p90_days_to_sale": scenario["additional_days_to_sale"]["p90"],
        "break_even_price": result["break_even_analysis"]["current_accounting_break_even"],
        "promotional_headroom": result["promotional_headroom"]["max_safe_discount"],
        "strategy": strategy,
    }


def _portfolio_summary(result: dict) -> dict:
    capacity = result["capacity_position"]
    valuation = result["portfolio_valuation"]
    one_month = result["one_month_forecast"]
    return {
        "units_on_lot": capacity["current_inventory"],
        "open_slots": capacity["physical_open_slots"],
        "utilization": capacity["current_utilization"],
        "units_below_break_even": result["financial_risk"]["units_below_break_even"],
        "cash_tied_up": valuation["cash_tied_up"],
        "thirty_day_units_p50": one_month["unit_sales"]["p50"],
        "thirty_day_units_p10": one_month["unit_sales"]["p10"],
        "thirty_day_units_p90": one_month["unit_sales"]["p90"],
    }


def _promotion_summary(result: dict, event: dict) -> dict:
    feasibility = result["feasibility"]
    target_block = result["inventory_target_calculation"]
    return {
        "event_name": event["event_name"],
        "feasibility_status": feasibility["status"],
        "target_ending_inventory": target_block["target_ending_inventory"],
        "incremental_required": target_block["incremental_promotional_sales_required"],
        "probability_target_achieved": feasibility["probability_target_achieved"],
        "recommended_plan": result["recommended_plan"]["plan_type"],
    }


def _top_warnings(result: dict, limit: int = 3) -> tuple[dict, ...]:
    return tuple(sort_by_severity(list(result.get("warnings", [])))[:limit])


# --- orchestration --------------------------------------------------------------------


# Advisory phrasing about a single vehicle: the dealer wants a coordinated recommendation, not
# just one metric. This is the enterprise-orchestrator entry point.
_ADVISORY = re.compile(
    r"\b(what should i do|what do i do|what'?s my (?:best )?(?:move|option|play)|"
    r"help me (?:with|decide)|recommend(?:ed)? (?:an )?action|what actions?|advise me|"
    r"best course of action|action plan)\b", re.IGNORECASE)


def _maybe_advise(text: str, routed: RouteResult, *, as_of: datetime) -> AssistantResponse | None:
    """Run the single-vehicle AI orchestrator when the text is advisory and resolves to one
    vehicle. Returns None to fall through to the normal single-workflow routing.

    The orchestrator invokes all three skills and synthesizes one prioritized action plan; it
    computes nothing itself, and the plan ends at a human approval, never an automatic action."""
    if not _ADVISORY.search(text):
        return None
    parsed = routed.parsed_vehicle
    if parsed is None:
        return None
    try:
        transport = MockTransport(as_of=as_of)
        inventory = VautoClient(transport).get_dealer_inventory().data
        match = resolve_vehicle(parsed, inventory)
        if match.status is not MatchStatus.EXACT:
            return None
        from pricing_agent.agents.vehicle_advisor import advise_vehicle
        advice = advise_vehicle(match.vehicle_id, transport, as_of=as_of)
    except Exception:  # noqa: BLE001 — advisory is best-effort; fall back to normal routing
        return None
    return AssistantResponse(
        state=AssistantState.ROUTED_AND_EXECUTED,
        message=(f"Orchestrated an action plan for {advice.description} ({match.vehicle_id}) "
                 "across three business capabilities."),
        route=routed,
        workflow=WorkflowContext.IMPROVE_AGING_INVENTORY,
        skill=routed.required_skill,
        resolved_vehicle_id=match.vehicle_id,
        advice=advice,
        target_url=WORKFLOW_URL[WorkflowContext.IMPROVE_AGING_INVENTORY],
    )


def run_assistant(text: str, *, as_of: datetime) -> AssistantResponse:
    """Route, resolve, and execute a single supported workflow for `text`."""
    routed = route(text)
    workflow = routed.selected_workflow

    advised = _maybe_advise(text, routed, as_of=as_of)
    if advised is not None:
        return advised

    if workflow is None:
        return AssistantResponse(
            state=AssistantState.NEEDS_CLARIFICATION,
            message=(
                "I could not tell which decision this is about. Try naming a vehicle to "
                "price, asking what the lot will do over the next 30 days, or describing a "
                "sale event."
            ),
            route=routed,
        )

    try:
        if workflow is WorkflowContext.PRICE_INVENTORY:
            return _run_pricing(text, routed, as_of=as_of)
        if workflow is WorkflowContext.ACQUIRE_INVENTORY:
            return _run_portfolio(routed, as_of=as_of)
        if workflow is WorkflowContext.MERCHANDISE_INVENTORY:
            return _run_promotion(text, routed, as_of=as_of)
        if workflow is WorkflowContext.IMPROVE_AGING_INVENTORY:
            return _run_improve_aging(text, routed, as_of=as_of)
    except Exception as error:  # noqa: BLE001 — surfaced as a state, never swallowed
        return AssistantResponse(
            state=AssistantState.EXECUTION_ERROR,
            message=(
                "The workflow was identified, but the analysis did not complete. Open the "
                f"workflow directly to retry. ({type(error).__name__})"
            ),
            route=routed,
            workflow=workflow,
            skill=routed.required_skill,
            target_url=WORKFLOW_URL.get(workflow),
        )

    # Unreachable: every non-None workflow is handled above.
    return AssistantResponse(
        state=AssistantState.NEEDS_CLARIFICATION,
        message="This request is not supported yet.",
        route=routed,
        workflow=workflow,
    )


def _run_pricing(text: str, routed: RouteResult, *, as_of: datetime) -> AssistantResponse:
    parsed = routed.parsed_vehicle
    url = WORKFLOW_URL[WorkflowContext.PRICE_INVENTORY]

    if parsed is None or not routed.execution_allowed:
        return AssistantResponse(
            state=AssistantState.NEEDS_CLARIFICATION,
            message=(
                "Which vehicle do you want to analyze? Select a vehicle or provide year, "
                "make, model, and trim."
            ),
            route=routed,
            workflow=WorkflowContext.PRICE_INVENTORY,
            skill=routed.required_skill,
            target_url=url,
        )

    transport = MockTransport(as_of=as_of)
    inventory = VautoClient(transport).get_dealer_inventory().data
    match = resolve_vehicle(parsed, inventory)

    if match.status is MatchStatus.EXACT:
        result = single_vehicle.analyze(match.vehicle_id, transport, input_text=text)
        return AssistantResponse(
            state=AssistantState.ROUTED_AND_EXECUTED,
            message=f"Priced {match.vehicle_id} with the single-vehicle valuation skill.",
            route=routed,
            workflow=WorkflowContext.PRICE_INVENTORY,
            skill=routed.required_skill,
            resolved_vehicle_id=match.vehicle_id,
            match=match,
            summary=_pricing_summary(result),
            warnings=_top_warnings(result),
            result=result,
            target_url=url,
        )

    if match.status is MatchStatus.AMBIGUOUS:
        return AssistantResponse(
            state=AssistantState.AMBIGUOUS_MATCH,
            message=(
                "More than one vehicle matches that description. Which one did you mean?"
            ),
            route=routed,
            workflow=WorkflowContext.PRICE_INVENTORY,
            skill=routed.required_skill,
            match=match,
            candidates=match.candidates,
            target_url=url,
        )

    if match.status is MatchStatus.NONE:
        return AssistantResponse(
            state=AssistantState.NO_MATCH,
            message=(
                "No vehicle in the current inventory matches that description. This "
                "prototype analyzes vehicles already in dealer inventory, so it will not "
                "invent one. Check the details, or pick a vehicle from Price Inventory."
            ),
            route=routed,
            workflow=WorkflowContext.PRICE_INVENTORY,
            skill=routed.required_skill,
            match=match,
            target_url=url,
        )

    # INSUFFICIENT
    return AssistantResponse(
        state=AssistantState.NEEDS_CLARIFICATION,
        message=(
            "Which vehicle do you want to analyze? Select a vehicle or provide year, make, "
            "model, and trim."
        ),
        route=routed,
        workflow=WorkflowContext.PRICE_INVENTORY,
        skill=routed.required_skill,
        match=match,
        target_url=url,
    )


def _run_portfolio(routed: RouteResult, *, as_of: datetime) -> AssistantResponse:
    transport = MockTransport(as_of=as_of)
    result = inventory_portfolio.analyze(transport)
    return AssistantResponse(
        state=AssistantState.ROUTED_AND_EXECUTED,
        message="Ran the portfolio forecast for acquisition readiness and capacity.",
        route=routed,
        workflow=WorkflowContext.ACQUIRE_INVENTORY,
        skill=routed.required_skill,
        summary=_portfolio_summary(result),
        warnings=_top_warnings(result),
        result=result,
        target_url=WORKFLOW_URL[WorkflowContext.ACQUIRE_INVENTORY],
    )


def _run_promotion(text: str, routed: RouteResult, *, as_of: datetime) -> AssistantResponse:
    transport = MockTransport(as_of=as_of)
    url = WORKFLOW_URL[WorkflowContext.MERCHANDISE_INVENTORY]
    events = EventClient(transport).get_sales_event_calendar().data
    event = resolve_event(text, events)

    if event is None:
        names = ", ".join(e["event_name"] for e in events) or "none on the calendar"
        return AssistantResponse(
            state=AssistantState.NEEDS_CLARIFICATION,
            message=(
                "Which event should I plan for? I could not match one on the calendar. "
                f"Available events: {names}. Name one, and I'll build the plan."
            ),
            route=routed,
            workflow=WorkflowContext.MERCHANDISE_INVENTORY,
            skill=routed.required_skill,
            target_url=url,
        )

    target = parse_target_utilization(text)
    result = promotion_planner.plan_event(
        transport, event["event_id"], target, input_text=text
    )
    return AssistantResponse(
        state=AssistantState.ROUTED_AND_EXECUTED,
        message=f"Planned the {event['event_name']} event with the promotion planner.",
        route=routed,
        workflow=WorkflowContext.MERCHANDISE_INVENTORY,
        skill=routed.required_skill,
        summary=_promotion_summary(result, event),
        warnings=_top_warnings(result),
        result=result,
        target_url=url,
    )


# --- Improve Aging orchestration ------------------------------------------------------

_WORKFLOW_STATE_TO_ASSISTANT = {
    WorkflowState.ROUTED_AND_EXECUTED: AssistantState.ROUTED_AND_EXECUTED,
    WorkflowState.NEEDS_CLARIFICATION: AssistantState.NEEDS_CLARIFICATION,
    WorkflowState.PARTIAL_RESULT: AssistantState.PARTIAL_RESULT,
    WorkflowState.TARGET_NOT_ACHIEVABLE: AssistantState.TARGET_NOT_ACHIEVABLE,
    WorkflowState.NO_SAFE_ACTIONS: AssistantState.NO_SAFE_ACTIONS,
    WorkflowState.EXECUTION_ERROR: AssistantState.EXECUTION_ERROR,
}


def build_improve_aging_request(text: str, *, as_of: datetime) -> ImproveAgingRequest:
    """Extract the structured request the orchestration needs. All text parsing lives here,
    in the agent layer, so the workflow itself never touches free text."""
    transport = MockTransport(as_of=as_of)
    events = EventClient(transport).get_sales_event_calendar().data
    event_requested = bool(_EVENT_REFERENCE.search(text))
    event = resolve_event(text, events) if event_requested else None
    return ImproveAgingRequest(
        target_utilization=parse_explicit_target(text),
        event_requested=event_requested,
        event_id=event["event_id"] if event else None,
        event_name=event["event_name"] if event else None,
        available_events=tuple(e["event_name"] for e in events),
    )


def _run_improve_aging(text: str, routed: RouteResult, *, as_of: datetime) -> AssistantResponse:
    transport = MockTransport(as_of=as_of)
    request = build_improve_aging_request(text, as_of=as_of)
    result = run_improve_aging(transport, request)

    summary = _improve_aging_summary(result)
    warnings = _improve_aging_top_warnings(result)
    return AssistantResponse(
        state=_WORKFLOW_STATE_TO_ASSISTANT[result.state],
        message=result.message,
        route=routed,
        workflow=WorkflowContext.IMPROVE_AGING_INVENTORY,
        skill=None,
        summary=summary,
        warnings=warnings,
        improve_aging=result,
        target_url=WORKFLOW_URL[WorkflowContext.IMPROVE_AGING_INVENTORY],
    )


def wrap_improve_aging(result, routed, *, message: str | None = None) -> AssistantResponse:
    """Wrap an already-computed ImproveAgingResult in an AssistantResponse.

    Used by the follow-up layer to re-run the deterministic workflow with a modified request and
    present the outcome through the same Slice-1 summary/warnings/url machinery. It executes no
    skill and computes no number — it only re-packages a finished result."""
    state = _WORKFLOW_STATE_TO_ASSISTANT.get(result.state, AssistantState.EXECUTION_ERROR)
    return AssistantResponse(
        state=state,
        message=message or result.message,
        route=routed,
        workflow=WorkflowContext.IMPROVE_AGING_INVENTORY,
        skill=None,
        summary=_improve_aging_summary(result),
        warnings=_improve_aging_top_warnings(result),
        improve_aging=result,
        target_url=WORKFLOW_URL[WorkflowContext.IMPROVE_AGING_INVENTORY],
    )


def _improve_aging_summary(result) -> dict:
    diag = result.portfolio_summary or {}
    selection = result.selection
    promotion = result.promotion_result
    action_counts: dict[str, int] = {}
    for a in result.consolidated_actions:
        action_counts[a["recommended_action"]] = action_counts.get(a["recommended_action"], 0) + 1
    # Reconciled counts: distinguish analysed vehicles, those needing immediate action, and the
    # number of vehicles (not approval records) needing a manager review. A grouping of what the
    # workflow already decided — no reclassification, no recomputed number.
    _immediate = {"REPRICE_NOW", "EVENT_PROMOTION", "MANAGER_REVIEW",
                  "WHOLESALE_OR_LOSS_MINIMIZATION_REVIEW"}
    analysed_ids = {e.vehicle_id for e in result.vehicle_evidence}
    action_by_id = {a["vehicle_id"]: a["recommended_action"] for a in result.consolidated_actions}
    immediate_count = sum(1 for vid in analysed_ids if action_by_id.get(vid) in _immediate)
    review_vehicle_ids = {a.get("vehicle_id") for a in result.approvals_required if a.get("vehicle_id")}
    # Vehicles whose *final action* is specifically MANAGER_REVIEW — a distinct, smaller concept
    # than "vehicles with review conditions" (which also includes wholesale-review vehicles).
    manager_review_count = sum(
        1 for a in result.consolidated_actions if a["recommended_action"] == "MANAGER_REVIEW")
    return {
        "workflow": "Improve Aging Inventory",
        "current_inventory": diag.get("current_inventory"),
        "current_utilization": diag.get("current_utilization"),
        "target_utilization": diag.get("target_utilization"),
        "aged_concentration_pct": diag.get("aged_concentration_pct"),
        "units_below_break_even": diag.get("units_below_break_even"),
        "candidate_count": len(selection.candidates) if selection else 0,
        "deep_analysed_count": len(result.vehicle_evidence),
        "immediate_action_count": immediate_count,
        "no_immediate_action_count": len(analysed_ids) - immediate_count,
        "review_vehicle_count": len(review_vehicle_ids),
        "manager_review_count": manager_review_count,
        "review_item_count": len(result.approvals_required),
        "excluded_count": len(selection.exclusions) if selection else 0,
        "required_unit_reduction": diag.get("required_unit_reduction"),
        "recommended_plan": promotion["recommended_plan"]["plan_type"] if promotion else None,
        "target_status": (promotion["feasibility"]["status"] if promotion else "NO_EVENT"),
        "probability_target_achieved": diag.get("probability_target_achieved"),
        "approvals_required": len(result.approvals_required),
        "action_counts": action_counts,
        "execution_order": list(result.execution_order),
    }


def _improve_aging_top_warnings(result, limit: int = 4) -> tuple[dict, ...]:
    seen: dict[str, dict] = {}
    sources = list(result.vehicle_evidence)
    for ev in sources:
        for w in ev.warnings:
            seen.setdefault(w["code"], w)
    if result.promotion_result:
        for w in result.promotion_result.get("warnings", []):
            seen.setdefault(w["code"], w)
    if result.portfolio_result:
        for w in result.portfolio_result.get("warnings", []):
            seen.setdefault(w["code"], w)
    return tuple(sort_by_severity(list(seen.values()))[:limit])
