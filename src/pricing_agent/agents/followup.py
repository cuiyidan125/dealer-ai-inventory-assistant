"""Deterministic follow-up classification and handling for the Dealer AI Assistant.

After the first grounded answer, the dealer asks follow-ups against the *same* structured result.
This module decides — with rules, never the LLM — which of five things a follow-up is, and
produces a grounded reply:

    E  unsupported / unavailable   → say so; invent nothing; never publish
    D  workflow rerun              → validated new input; re-run the deterministic workflow
    C  clarification               → one concise question for the missing input
    B  filter existing result      → select existing rows; no rerun
    A  explain existing result     → explain from existing fields; no rerun, no new number

Order matters: unsupported and rerun are tested before answering, so "use Summer Clearance" or
"publish the price" can never be mistaken for a question about the current result. Every value in
a reply is copied from the active result; nothing here computes a price, a percentile, or a
probability, and a rerun keeps the previous valid result until the new one succeeds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime

from pricing_agent.agents.aging_answer import IMMEDIATE_ACTIONS, build_aging_answer
from pricing_agent.agents.assistant import (
    AssistantState,
    parse_explicit_target,
    resolve_event,
    run_assistant,
    wrap_improve_aging,
)
from pricing_agent.agents.conversation import (
    SOURCE_CLARIFICATION,
    SOURCE_ERROR,
    SOURCE_EXPLANATION,
    SOURCE_FILTERED,
    SOURCE_RERUN,
    SOURCE_SWITCH,
    SOURCE_UNSUPPORTED,
    ConversationState,
    VehicleRef,
    resolve_reference,
    vehicle_index,
)
from pricing_agent.agents.router import route
from pricing_agent.mcp_clients import EventClient, MockTransport
from pricing_agent.workflows.context import WorkflowContext
from pricing_agent.workflows.improve_aging import WorkflowState, run_improve_aging

# Explicit aging-intent words: when present, a message stays a follow-up to the active aging
# result even if it brushes a pricing word — the action asked for is about the aging analysis.
_AGING_INTENT = re.compile(
    r"\b(why|selected|aging|wholesale|manager[\s-]?review|break[\s-]?even|days on lot|"
    r"included in|promotion candidate|review condition|no[\s-]?immediate|not need immediate)\b",
    re.IGNORECASE)

_THIS_VEHICLE = re.compile(r"\bthis vehicle\b|\bthis one\b|\bit\b", re.IGNORECASE)

# --- signals --------------------------------------------------------------------------

# Data the prototype does not have, or actions it must never take. Checked first so none of these
# can be read as a question about the current result.
_UNSUPPORTED = re.compile(
    r"\b(vdp|vdp views?|page views?|shopper|shoppers|leads?|lead conversion|conversion rate|"
    r"click[\s-]?through|live market|market supply|days[\s-]?supply|inventory turn rate|"
    r"web traffic|test drives?|publish|push the price|go live)\b", re.IGNORECASE)

_EVENT_REFERENCE = re.compile(
    r"\b(event|promotion|promo|campaign|clearance|sale|labor\s*day|memorial\s*day|"
    r"black\s*friday|holiday|summer)\b", re.IGNORECASE)

_EXCLUDE = re.compile(
    r"\b(exclude|remove|drop|protect|hold|keep|don'?t\s+(?:promote|discount|touch)|"
    r"leave\s+(?:out|alone))\b", re.IGNORECASE)

_WHY = re.compile(r"\bwhy\b", re.IGNORECASE)

# Filter vocabulary → (dealer label, predicate over a VehicleRef). Analysed vehicles only.
_FILTERS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"over\s*90|past\s*90|already.*90|90\s*days?\b|120\s*days?\b", re.I),
     "already over 90 days on lot",
     "over_90"),
    (re.compile(r"wholesale", re.I), "flagged for wholesale / loss-minimization review", "wholesale"),
    (re.compile(r"manager[\s-]?review", re.I), "assigned to manager review", "manager_review"),
    (re.compile(r"safe\s+promotional\s+room|promotional\s+room|discount\s+room|safe\s+headroom", re.I),
     "have safe promotional room", "safe_room"),
    (re.compile(r"depreciation", re.I), "carry high depreciation risk", "depreciation"),
    (re.compile(r"inbound|replacement\s+pressure", re.I),
     "face inbound replacement pressure", "inbound"),
    (re.compile(r"below\s+break[\s-]?even|underwater|under\s+water", re.I),
     "are priced below break-even", "below_break_even"),
    (re.compile(r"require\s+review|need\s+review|needs?\s+a?\s*(?:manager\s+)?review|"
                r"require\s+a?\s*manager", re.I),
     "require review before any pricing action", "requires_review"),
    (re.compile(r"no[\s-]?immediate|don'?t\s+need\s+(?:immediate\s+)?action|"
                r"not\s+need\s+immediate", re.I),
     "do not need immediate action", "no_immediate"),
)


def _predicate(key: str):
    over90 = {"CURRENTLY_OVER_90_DAYS", "CURRENTLY_OVER_120_DAYS"}
    return {
        "over_90": lambda r: bool(set(r.reason_codes) & over90),
        "wholesale": lambda r: r.action_code == "WHOLESALE_OR_LOSS_MINIMIZATION_REVIEW",
        "manager_review": lambda r: r.action_code == "MANAGER_REVIEW",
        "safe_room": lambda r: "HIGH_SAFE_PROMOTIONAL_HEADROOM" in r.reason_codes,
        "depreciation": lambda r: "HIGH_DEPRECIATION_RISK" in r.reason_codes,
        "inbound": lambda r: "INBOUND_REPLACEMENT_PRESSURE" in r.reason_codes,
        "below_break_even": lambda r: "BELOW_PROJECTED_BREAK_EVEN" in r.approvals,
        "requires_review": lambda r: bool(r.approvals),
        "no_immediate": lambda r: r.analysed and r.action_code not in IMMEDIATE_ACTIONS,
    }[key]


# --- result -----------------------------------------------------------------------


@dataclass(frozen=True)
class FollowupResult:
    kind: str
    text: str
    referenced_ids: tuple[str, ...] = ()
    reran: bool = False
    success: bool = True
    response: object | None = None      # a new AssistantResponse when a rerun succeeded
    payload: dict | None = None         # structured render data for pricing follow-up turns


# --- entry point ------------------------------------------------------------------


def handle_followup(text: str, state: ConversationState, *, as_of: datetime) -> FollowupResult:
    """Classify and answer a follow-up, mutating `state` (history, and active result on a rerun).

    On a successful rerun the previous result is preserved in `previous_valid_result` and the new
    one is adopted; on any failure the previous active result is left untouched.
    """
    state.add_user(text)

    if not state.has_active_result:
        result = FollowupResult(
            SOURCE_ERROR,
            "I don't have an analysis to work from yet. Ask an aging question first — for "
            "example, \"Which aging vehicles should I promote?\"", success=False)
        return _record(state, result)

    # Unavailable data and price-publishing are refused first — they are neither a workflow switch
    # nor a follow-up, and "publish the price" must never be read as a valuation intent.
    unsupported = _unsupported(text, state, as_of)
    if unsupported is not None:
        return _record(state, unsupported)

    # New-workflow intent is checked *before* follow-up classification, so a pricing request is
    # never forced into the active aging result just because the vehicle appears there.
    routed = detect_new_workflow(text, state)
    if routed is not None:
        # A genuine switch takes precedence — *unless* we're in a pricing conversation and the
        # message is a clear pricing follow-up about the current car. That yields the switch, so a
        # re-target like "sell it within 20 days" isn't mis-routed by a coarse "…30 days…" cue.
        followup_here = False
        if state.active_workflow_type == "PRICE_INVENTORY":
            from pricing_agent.agents.pricing_followup import classify as _pricing_classify
            followup_here = _pricing_classify(text, state) is not None
        elif state.active_workflow_type == "ACQUIRE_INVENTORY":
            from pricing_agent.agents.portfolio_followup import classify as _portfolio_classify
            followup_here = _portfolio_classify(text, state) is not None
        if not followup_here:
            return _record(state, _switch_workflow(text, state, routed, as_of))

    if state.active_workflow_type == "IMPROVE_AGING_INVENTORY":
        handlers = (_rerun, _clarification, _filter, _explain)
        for handler in handlers:
            result = handler(text, state, as_of)
            if result is not None:
                return _record(state, result)
        return _record(state, _fallback(state))

    if state.active_workflow_type == "PRICE_INVENTORY":
        # Lazy import breaks the followup <-> pricing_followup cycle (pricing_followup imports
        # FollowupResult from this module).
        from pricing_agent.agents.pricing_followup import handle_pricing_followup
        priced = handle_pricing_followup(text, state, as_of=as_of)
        if priced is not None:
            return _record(state, priced)

    if state.active_workflow_type == "ACQUIRE_INVENTORY":
        from pricing_agent.agents.portfolio_followup import handle_portfolio_followup
        portfolio = handle_portfolio_followup(text, state, as_of=as_of)
        if portfolio is not None:
            return _record(state, portfolio)

    # Active workflow is not the multi-turn aging result and no follow-up handler matched.
    return _record(state, _non_aging_followup(state))


def _record(state: ConversationState, result: FollowupResult) -> FollowupResult:
    """Append the assistant turn and update reference memory / active result."""
    if result.kind == SOURCE_SWITCH and result.success and result.response is not None:
        # Preserve the current workflow in history, then adopt the new one — active is replaced
        # only here, after the new workflow has succeeded.
        state.switch_to(result.response)
        if result.referenced_ids:
            state.last_referenced_vehicle_ids = result.referenced_ids
        state.add_assistant(result.text, SOURCE_SWITCH, result=state.active_result,
                            response=result.response, referenced=result.referenced_ids,
                            workflow_id=state.active_workflow_id)
        return result
    if result.referenced_ids:
        state.last_referenced_vehicle_ids = result.referenced_ids
    if result.reran and result.success and result.response is not None:
        state.previous_valid_result = state.active_result
        state.adopt(result.response)
        state.rerun_count += 1
        state.pending_clarification = None
        state.add_assistant(result.text, SOURCE_RERUN, result=state.active_result,
                            response=result.response, referenced=result.referenced_ids,
                            workflow_id=state.active_workflow_id)
        return result
    if result.kind == SOURCE_CLARIFICATION:
        state.pending_clarification = result.text
    else:
        state.pending_clarification = None
    state.add_assistant(result.text, result.kind, referenced=result.referenced_ids,
                        workflow_id=state.active_workflow_id, payload=result.payload)
    return result


# --- new-workflow switch (runs before A–E) ----------------------------------------


def detect_new_workflow(text: str, state: ConversationState):
    """Return the RouteResult when `text` is a strong new-workflow intent that should switch away
    from the active conversation, else None. Deterministic — this reads the existing router.

    The rule depends on which workflow is active:

    * A Single Vehicle Valuation (`PRICE_INVENTORY`) is always a switch target — "price the Honda",
      "value another vehicle" — whichever workflow is active.
    * From the multi-turn **aging** result, non-valuation routes stay follow-ups: event/promotion
      phrases add an event to the *same* aging analysis (a rerun), and aging routes are follow-ups.
    * From a **single-skill** result (a valuation, a portfolio forecast, a promotion plan), a route
      to a *different* workflow is a genuine switch — e.g. a portfolio question asked after a
      valuation, which otherwise dead-ended in "ask me to value another vehicle".

    An explicit aging-intent word keeps the message a follow-up regardless."""
    if _AGING_INTENT.search(text):
        return None
    routed = route(text)
    target = routed.selected_workflow
    if target is None:
        return None
    if target is WorkflowContext.PRICE_INVENTORY:
        return routed
    # Non-valuation target: only switch away from a single-skill result, and only to a *different*
    # workflow. Aging (and no active result) keeps these as follow-ups/reruns.
    active = state.active_workflow_type
    if active in (None, "IMPROVE_AGING_INVENTORY"):
        return None
    if target.name != active:
        return routed
    return None


def _switch_workflow(text, state, routed, as_of: datetime) -> FollowupResult:
    """Switch the conversation to the routed workflow. A valuation resolves a target vehicle and
    runs the single-vehicle skill; any other workflow (portfolio forecast, event promotion) runs
    through the existing deterministic assistant. Either way the caller performs the state switch
    only on success; on failure the active result is left untouched."""
    if routed.selected_workflow is WorkflowContext.PRICE_INVENTORY:
        return _switch_to_valuation(text, state, routed, as_of)
    return _switch_to_other_workflow(text, state, routed, as_of)


def _switch_to_other_workflow(text, state, routed, as_of: datetime) -> FollowupResult:
    """Switch to a non-valuation workflow (portfolio forecast or event promotion) by running the
    existing deterministic assistant. On success returns a SWITCH; when the workflow needs an input
    it could not resolve (e.g. a promotion with no named event) it asks; on failure it keeps the
    previous result untouched."""
    try:
        response = run_assistant(text, as_of=as_of)
    except Exception as error:  # noqa: BLE001 — a failed switch must never overwrite the prior result
        return FollowupResult(
            SOURCE_ERROR,
            f"I couldn't complete that ({type(error).__name__}); I've kept the previous analysis.",
            success=False)

    if response.state is AssistantState.ROUTED_AND_EXECUTED \
            and response.workflow is routed.selected_workflow:
        prior = _workflow_label(state.active_workflow_type)
        target_label = _workflow_label(response.workflow.name)
        transition = f"Switching from {prior} to {target_label}."
        ids = (response.resolved_vehicle_id,) if response.resolved_vehicle_id else ()
        return FollowupResult(SOURCE_SWITCH, transition, referenced_ids=ids,
                              success=True, response=response)

    if response.state is AssistantState.NEEDS_CLARIFICATION:
        # e.g. a promotion request that named no event on the calendar — ask, don't switch.
        return FollowupResult(SOURCE_CLARIFICATION,
                              response.message or "I need a bit more detail to run that.")
    return FollowupResult(
        SOURCE_ERROR,
        "That workflow could not be completed, so I've kept the previous analysis.",
        success=False)


def _switch_to_valuation(text, state, routed, as_of: datetime) -> FollowupResult:
    """Resolve the target vehicle and run the existing deterministic valuation. On success returns
    a SWITCH result (the caller performs the state switch); on failure or ambiguity it returns an
    error/clarification and the active result is left untouched."""
    target, ambiguous = _price_target(text, state)
    if ambiguous:
        options = _describe_ids(state, ambiguous)
        return FollowupResult(
            SOURCE_CLARIFICATION,
            f"More than one vehicle matches that — {options}. Which should I value?",
            referenced_ids=tuple(ambiguous))
    if target is None and not routed.execution_allowed:
        return FollowupResult(
            SOURCE_CLARIFICATION,
            "Which vehicle should I value? Name it (year, make, model) or pick one from the "
            "current list.")

    query = f"price {target}" if target else text
    try:
        response = run_assistant(query, as_of=as_of)
    except Exception as error:  # noqa: BLE001 — a failed switch must never overwrite the prior result
        return FollowupResult(
            SOURCE_ERROR,
            f"I couldn't complete the valuation ({type(error).__name__}); I've kept the previous "
            "analysis.", success=False)

    if response.state is AssistantState.ROUTED_AND_EXECUTED \
            and response.workflow is WorkflowContext.PRICE_INVENTORY:
        desc = response.summary.get("vehicle") or response.resolved_vehicle_id
        prior = _workflow_label(state.active_workflow_type)
        transition = f"Switching from {prior} to Single Vehicle Valuation for {desc}."
        ids = (response.resolved_vehicle_id,) if response.resolved_vehicle_id else ()
        return FollowupResult(SOURCE_SWITCH, transition, referenced_ids=ids,
                              success=True, response=response)

    if response.state is AssistantState.AMBIGUOUS_MATCH:
        return FollowupResult(
            SOURCE_CLARIFICATION,
            "More than one vehicle matches that description. Which one did you mean?")
    if response.state is AssistantState.EXECUTION_ERROR:
        return FollowupResult(
            SOURCE_ERROR,
            "The valuation could not be completed, so I've kept the previous analysis.",
            success=False)
    # NO_MATCH / NEEDS_CLARIFICATION — ask, do not switch, keep the prior result.
    return FollowupResult(SOURCE_CLARIFICATION,
                          response.message or "Which vehicle should I value?")


def _price_target(text: str, state: ConversationState):
    """(vehicle_id, None) for a single resolved target; (None, ids) when ambiguous; (None, None)
    when nothing resolves. Uses the active-result reference resolver (Slice 2), including the
    documented analysed-over-excluded preference."""
    ref = resolve_reference(text, state)
    if ref.ambiguous:
        return None, ref.ids
    if len(ref.ids) == 1:
        return ref.ids[0], None
    if len(ref.ids) > 1:
        return None, ref.ids
    if _THIS_VEHICLE.search(text) and len(state.last_referenced_vehicle_ids) == 1:
        return state.last_referenced_vehicle_ids[0], None
    return None, None


def _describe_ids(state: ConversationState, ids) -> str:
    index = vehicle_index(state.active_result) if state.active_workflow_type == \
        "IMPROVE_AGING_INVENTORY" else {}
    return "; ".join(f"{index[i].description} ({i})" if i in index else i for i in ids)


def _non_aging_followup(state: ConversationState) -> FollowupResult:
    """After switching to a single-skill result (e.g. a valuation), keep the conversation honest:
    offer to value another vehicle or return to the earlier analysis, rather than pretend a rich
    follow-up engine exists for it."""
    prior = state.prior_workflows[-1].workflow_type if state.prior_workflows else None
    hint = " or ask to go back to the aging analysis" if prior == "IMPROVE_AGING_INVENTORY" else ""
    return FollowupResult(
        SOURCE_CLARIFICATION,
        f"You're viewing the {_workflow_label(state.active_workflow_type)} result. Ask me to value "
        f"another vehicle{hint}. The full evidence is in the workspace.")


def _workflow_label(workflow_type: str | None) -> str:
    return {
        "IMPROVE_AGING_INVENTORY": "Improve Aging Inventory",
        "PRICE_INVENTORY": "Single Vehicle Valuation",
        "ACQUIRE_INVENTORY": "Portfolio Forecast",
        "MERCHANDISE_INVENTORY": "Event Promotion",
    }.get(workflow_type or "", workflow_type or "the previous workflow")


# --- E. unsupported / unavailable -------------------------------------------------


def _unsupported(text: str, state: ConversationState, as_of: datetime) -> FollowupResult | None:
    if not _UNSUPPORTED.search(text):
        return None
    if re.search(r"\bpublish|push the price|go live\b", text, re.IGNORECASE):
        return FollowupResult(
            SOURCE_UNSUPPORTED,
            "This prototype never publishes a price — every recommendation is decision support "
            "for a manager to act on. I can't push a price live.")
    return FollowupResult(
        SOURCE_UNSUPPORTED,
        "That relies on data this prototype doesn't have — shopper views, lead conversion, and "
        "live market supply aren't part of the analysis. I won't invent it. I can explain the "
        "vehicles, their prices, days on lot, break-even, and review conditions from the "
        "current result.")


# --- D. workflow rerun ------------------------------------------------------------


def _rerun(text: str, state: ConversationState, as_of: datetime) -> FollowupResult | None:
    event = _resolved_event(text, as_of)
    if event is not None and event["event_name"] == state.active_event:
        event = None    # already the active event — not a rerun
    target = parse_explicit_target(text)
    exclude_ids: tuple[str, ...] = ()
    if _EXCLUDE.search(text):
        ref = resolve_reference(text, state)
        if ref.ambiguous:
            return _ambiguous(ref)
        exclude_ids = ref.ids

    if event is None and target is None and not exclude_ids:
        return None

    base = state.active_result.request
    kwargs: dict = {}
    changed: list[str] = []
    if event is not None:
        kwargs.update(event_requested=True, event_id=event["event_id"],
                      event_name=event["event_name"])
        changed.append(f"added the {event['event_name']} event")
    if target is not None:
        kwargs.update(target_utilization=target)
        changed.append(f"set the target to {target:.0%}")
    if exclude_ids:
        merged = tuple(dict.fromkeys(base.excluded_vehicle_ids + exclude_ids))
        kwargs.update(excluded_vehicle_ids=merged)
        index = vehicle_index(state.active_result)
        names = ", ".join(index[i].description for i in exclude_ids if i in index)
        changed.append(f"protected {names}")

    new_request = replace(base, **kwargs)
    status = "Re-running the Improve Aging analysis — " + "; ".join(changed) + "."

    try:
        new_result = run_improve_aging(MockTransport(as_of=as_of), new_request)
    except Exception as error:  # noqa: BLE001 — a rerun must degrade, never overwrite
        return FollowupResult(
            SOURCE_ERROR,
            f"{status}\n\nThe re-run did not complete ({type(error).__name__}), so I've kept the "
            "previous analysis. Nothing changed.", success=False)

    if new_result.state is WorkflowState.EXECUTION_ERROR:
        return FollowupResult(
            SOURCE_ERROR,
            f"{status}\n\nThe re-run could not complete, so I've kept the previous analysis.",
            success=False)

    routed = getattr(state.active_response, "route", None)
    new_response = wrap_improve_aging(new_result, routed, message=new_result.message)
    text_out = _rerun_summary(status, state, new_result, new_response)
    return FollowupResult(SOURCE_RERUN, text_out, reran=True, success=True, response=new_response)


def _resolved_event(text: str, as_of: datetime) -> dict | None:
    if not _EVENT_REFERENCE.search(text):
        return None
    events = EventClient(MockTransport(as_of=as_of)).get_sales_event_calendar().data or []
    return resolve_event(text, events)


def _rerun_summary(status: str, state: ConversationState, new_result, new_response) -> str:
    """A what-changed summary built only from existing fields of the old and new results."""
    answer = build_aging_answer(new_result, workspace_url=new_response.target_url)
    lines = [status, "", answer.understood]
    block = answer.event_block
    if block and block.promoted:
        promoted = ", ".join(v.description for v in block.promoted)
        lines.append(f"Recommended for the {block.event_name} event: {promoted}.")
        if block.probability_target_achieved is not None:
            plan_note = ""
            if block.recommended_plan:
                plan_note = f" ({block.recommended_plan.replace('_', ' ').title()} plan)"
            lines.append(
                f"Target likelihood: reaches the target "
                f"{block.probability_target_achieved:.0%} of the time{plan_note}.")
    else:
        lines.append(f"{answer.immediate_count} need immediate action; "
                     f"{answer.no_immediate_count} have no immediate action.")
    if answer.review_vehicle_count:
        lines.append(f"{answer.review_vehicle_count} vehicles require review before pricing "
                     "changes.")
    lines.append("_The previous analysis is preserved; details are in the full workspace._")
    return "\n\n".join(lines)


# --- C. clarification -------------------------------------------------------------


def _clarification(text: str, state: ConversationState, as_of: datetime) -> FollowupResult | None:
    lowered = text.lower()
    # A rerun-shaped ask with no resolvable input reaches here (rerun already consumed the
    # resolvable cases).
    if re.search(r"\bpromote(\s+them| all)?\b|in the event|use the event|the sale event", lowered):
        events = EventClient(MockTransport(as_of=as_of)).get_sales_event_calendar().data or []
        names = ", ".join(e["event_name"] for e in events) or "none on the calendar"
        return FollowupResult(
            SOURCE_CLARIFICATION,
            f"Which sale event should I plan for? I won't assume one. Available: {names}.")
    if re.search(r"\btarget\b|\butilization\b|\blower\b|\braise\b", lowered) \
            and parse_explicit_target(text) is None:
        return FollowupResult(
            SOURCE_CLARIFICATION,
            "What utilization target should I use? Name a percentage, e.g. \"set the target to "
            "70%\".")
    if re.search(r"\bthat vehicle\b|\bthis one\b|\bit\b", lowered) \
            and not resolve_reference(text, state).ids:
        return FollowupResult(
            SOURCE_CLARIFICATION,
            "Which vehicle do you mean? Name it — for example \"the BMW\" or a vehicle id.")
    return None


# --- B. filter --------------------------------------------------------------------


def _filter(text: str, state: ConversationState, as_of: datetime) -> FollowupResult | None:
    # A question about one specific vehicle ("is the Accord below break-even?") is an explanation
    # of that vehicle, not a group filter — defer to _explain. A group reference (the wholesale
    # vehicles, the two no-immediate) or a pure filter phrase still filters.
    ref = resolve_reference(text, state)
    if ref.ambiguous or (len(ref.ids) == 1) or (_WHY.search(text) and ref.ids):
        return None
    match = next((f for f in _FILTERS if f[0].search(text)), None)
    if match is None:
        return None
    _pat, label, key = match
    index = vehicle_index(state.active_result)
    predicate = _predicate(key)
    rows = [r for r in index.values() if r.analysed and predicate(r)]
    if not rows:
        return FollowupResult(SOURCE_FILTERED,
                              f"No analysed vehicle in this result {label}.")
    listing = "\n".join(f"- {r.description}" for r in rows)
    header = (f"{_count_word(len(rows)).capitalize()} analysed "
              f"vehicle{'s' if len(rows) != 1 else ''} {label}:")
    return FollowupResult(SOURCE_FILTERED, f"{header}\n\n{listing}",
                          referenced_ids=tuple(r.vehicle_id for r in rows))


# --- A. explain -------------------------------------------------------------------


def _explain(text: str, state: ConversationState, as_of: datetime) -> FollowupResult | None:
    ref = resolve_reference(text, state)
    if ref.ambiguous:
        return _ambiguous(ref)
    if not ref.ids:
        if _WHY.search(text):
            return FollowupResult(
                SOURCE_CLARIFICATION,
                "Which vehicle do you mean? Name it — for example \"the BMW\" or a vehicle id.")
        return None
    index = vehicle_index(state.active_result)
    paragraphs = [_explain_vehicle(index[i]) for i in ref.ids if i in index]
    return FollowupResult(SOURCE_EXPLANATION, "\n\n".join(paragraphs),
                          referenced_ids=ref.ids)


def _explain_vehicle(ref: VehicleRef) -> str:
    from pricing_agent.views import improve_aging_copy as copy
    from pricing_agent.views import terminology as T

    immediate = ref.action_code in IMMEDIATE_ACTIONS
    action = copy.action_label(ref.action_code) if immediate else "no immediate action"
    reasons = "; ".join(copy.selection_label(c) for c in ref.reason_codes)
    lines = [f"**{ref.description}** — {action}."]
    if reasons:
        lines.append(f"Selected because: {reasons}.")

    res = ref.result or {}
    veh = res.get("vehicle", {})
    be = res.get("break_even_analysis", {})
    strat = res.get("recommended_strategy", {}).get("strategy")
    scen = next((s for s in res.get("pricing_scenarios", []) if s["strategy"] == strat), {})
    days_add = scen.get("additional_days_to_sale", {})
    facts: list[str] = []
    if veh.get("days_in_inventory") is not None:
        facts.append(f"{int(veh['days_in_inventory'])} days on the lot")
    if ref.current_price is not None:
        facts.append(f"asking ${ref.current_price:,.0f}")
    if be.get("current_accounting_break_even") is not None:
        facts.append(f"break-even ${be['current_accounting_break_even']:,.0f}")
    if days_add.get("p50") is not None and days_add.get("p90") is not None:
        facts.append(f"expected {days_add['p50']:.0f} days to sell (P50), "
                     f"{days_add['p90']:.0f} in the conservative case (P90)")
    if facts:
        lines.append(_capitalize(" · ".join(facts)) + ".")

    proposed = scen.get("proposed_list_price")
    min_safe = be.get("minimum_safe_list_price")
    if isinstance(proposed, (int, float)) and isinstance(min_safe, (int, float)) \
            and proposed < min_safe:
        lines.append(f"The proposed price ${proposed:,.0f} is below the lowest safe asking price "
                     f"${min_safe:,.0f} — that price floor is why it needs a manager review.")

    if ref.approvals:
        whys = list(dict.fromkeys(T.approval_why(a) for a in ref.approvals))
        lines.append("Review conditions before repricing: " + "; ".join(whys) + ".")
    return " ".join(lines)


# --- helpers ----------------------------------------------------------------------


def _ambiguous(ref) -> FollowupResult:
    options = ref.label
    return FollowupResult(
        SOURCE_CLARIFICATION,
        f"That matches more than one vehicle — {options}. Which one did you mean?",
        referenced_ids=ref.ids)


def _fallback(state: ConversationState) -> FollowupResult:
    return FollowupResult(
        SOURCE_CLARIFICATION,
        "I can explain a vehicle (\"why is the BMW recommended for wholesale?\"), filter the "
        "list (\"show only vehicles over 90 days\"), or re-run with a change (\"use Summer "
        "Clearance\", \"set the target to 70%\"). What would you like?")


_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def _count_word(n: int) -> str:
    return _WORDS[n] if 0 <= n < len(_WORDS) else str(n)


def _capitalize(s: str) -> str:
    return s[0].upper() + s[1:] if s else s
