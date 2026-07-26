"""Ask the Dealer AI Assistant — the product's primary entry point.

Phase 4 connects this page to deterministic routing. A dealer's question is classified,
the named vehicle is resolved against real inventory, and — for a supported single-workflow
request — one skill runs and its result is summarised here, with a button to open the full
analytical workspace.

Three things this page still does not do, by design:

* **No LLM.** Routing, parsing, and resolution are rules over strings
  (`pricing_agent.agents`). The only numbers shown come straight out of the skill result.
* **No orchestration.** Improve Aging, which would coordinate three skills, returns a
  transparent "not yet available" rather than running anything.
* **No publishing.** Nothing here writes a price.

The workflow cards are passed in by the caller, not imported, so the view stays free of an
import cycle with `workflows.registry`.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable, NamedTuple

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

import ui_components
from pricing_agent.agents import build_aging_answer, handle_followup, new_state, run_assistant
from pricing_agent.agents.assistant import AssistantResponse, AssistantState
from pricing_agent.agents.conversation import (
    PORTFOLIO_FOLLOWUP_SOURCES,
    PRICING_FOLLOWUP_SOURCES,
    RICH_SOURCES,
    SOURCE_CLARIFICATION,
    SOURCE_ERROR,
    SOURCE_EXPLANATION,
    SOURCE_FILTERED,
    SOURCE_FIRST_TURN,
    SOURCE_PORTFOLIO_ACQUIRE_REVIEW,
    SOURCE_PORTFOLIO_LOT_TODAY,
    SOURCE_PORTFOLIO_OUTLOOK,
    SOURCE_PORTFOLIO_TOP_RISK,
    SOURCE_PRICING_DETAIL,
    SOURCE_PRICING_REASONING,
    SOURCE_PRICING_STRATEGIES,
    SOURCE_PRICING_TARGET,
    SOURCE_RERUN,
    SOURCE_SWITCH,
    SOURCE_UNSUPPORTED,
)
from pricing_agent.mcp_clients import MockTransport, VautoClient
from pricing_agent.views import terminology as T
from pricing_agent.views.review_panel import render_review_panel, severity_dot
from pricing_agent.views.style import style_fig
from pricing_agent.workflows.context import WorkflowContext
from pricing_agent.workflows.pages import page_for

AS_OF = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)

# Stable, non-widget session keys. Streamlit garbage-collects the state of any widget that
# is not rendered on a run, so the request and its result are held under plain keys that
# survive navigating to a workflow and back.
QUESTION_KEY = "assistant_question"
RESPONSE_KEY = "assistant_response"
SELECTED_VEHICLE_KEY = "assistant_selected_vehicle_id"
CONVERSATION_KEY = "assistant_conversation"
PENDING_FOLLOWUP_KEY = "assistant_pending_followup"

# Provenance shown above each non-rich assistant turn, so the dealer always knows whether an
# answer came from the existing result, a filter, a clarification, an unavailable capability,
# or a preserved-previous error.
_PROVENANCE = {
    SOURCE_EXPLANATION: "🔎 From your current analysis",
    SOURCE_FILTERED: "🔎 Filtered from your current analysis",
    SOURCE_CLARIFICATION: "❓ Needs a bit more information",
    SOURCE_UNSUPPORTED: "🚫 Not available in this prototype",
    SOURCE_ERROR: "⚠️ Kept your previous analysis",
    SOURCE_RERUN: "🔄 Re-ran the analysis",
    SOURCE_SWITCH: "🔀 Switched workflow",
    SOURCE_PRICING_TARGET: "🎯 Re-targeted for a faster sale",
    SOURCE_PRICING_STRATEGIES: "📊 Pricing strategies compared",
    SOURCE_PRICING_DETAIL: "🧾 Selected strategy — detail",
    SOURCE_PRICING_REASONING: "🧭 Reasoning",
    SOURCE_PORTFOLIO_OUTLOOK: "🔮 Inventory outlook — next 30 days",
    SOURCE_PORTFOLIO_LOT_TODAY: "📊 Your lot today",
    SOURCE_PORTFOLIO_TOP_RISK: "🚨 Vehicles needing attention",
    SOURCE_PORTFOLIO_ACQUIRE_REVIEW: "🧭 Before you acquire",
}

# Read by the Price Inventory view to preselect the routed vehicle.
SESSION_KEY = QUESTION_KEY  # kept for backward compatibility with earlier phases

SUGGESTED_PROMPTS = (
    "What should I price 2020 Ford F-150 XLT?",
    "What will my inventory look like in the next 30 days?",
    "Plan the Summer Clearance event to reach 70% utilization.",
    "Which aging vehicles should I promote?",
)


class WorkflowCard(NamedTuple):
    """The subset of registry metadata this view renders. Passed in, never imported."""

    display_name: str
    description: str
    icon: str
    availability: str
    skills: tuple[str, ...]


def md(text: str) -> str:
    """Escape dollar signs so Streamlit does not read `$…$` as LaTeX."""
    return str(text).replace("$", r"\$")


def _money(value: object) -> str:
    try:
        return f"\\${float(value):,.0f}"
    except (TypeError, ValueError):
        return "—"


def _open_workflow_link(url_path: str, label: str) -> None:
    """A client-side link to a workflow page.

    `st.page_link` with the live `st.Page` navigates inside the Streamlit session, so the
    routed vehicle held in session state survives the jump. A raw HTML anchor would do a
    full page reload and wipe it, which is exactly the bug this avoids. If the page is not
    registered (e.g. imported outside the running app), fall back to guidance.
    """
    page = page_for(url_path)
    if page is not None:
        st.page_link(page, label=label, icon=":material/arrow_forward:")
    else:
        st.caption(f"Open the workflow from the sidebar (`{url_path}`).")


def render_assistant_home(
    workflow_context: WorkflowContext | None = None,
    workflows: Iterable[WorkflowCard] = (),
) -> None:
    """Render the assistant entry point."""
    st.title("Ask the Dealer AI Assistant")
    st.caption(
        "Describe a pricing or inventory decision in your own words. The assistant reads "
        "the request with deterministic rules, chooses the right dealer workflow, resolves "
        "the vehicle against real inventory, and hands the numbers to the engine — it never "
        "produces a price itself, and never calls a model."
    )

    st.markdown(
        "**What it can do now**\n\n"
        "- Price a vehicle already in inventory, end to end\n"
        "- Forecast what the lot will sell over the next 30 and 90 days\n"
        "- Plan a sale event when you name one on the calendar\n"
        "- Improve aging inventory — diagnose, select, price, and plan across all three skills"
    )

    request = st.text_area(
        "What are you trying to decide?",
        key="assistant_input",
        placeholder="e.g. What should I price 2020 Ford F-150 XLT?",
        height=110,
    )

    conversation = st.session_state.get(CONVERSATION_KEY)
    in_conversation = conversation is not None and conversation.has_active_result

    left, right = st.columns([1, 4])
    submitted = left.button("Get recommendation", type="primary")
    right.caption("Ask a new question here; follow up in the chat box below.")

    # The starter prompts are only useful before a conversation exists; once one does, the
    # relevant follow-ups are shown beneath the thread instead.
    if not in_conversation:
        for prompt in SUGGESTED_PROMPTS:
            st.markdown(f"- _{prompt}_")

    if submitted:
        if request.strip():
            _start_conversation(request)
        else:
            st.warning("Type a question first, or pick a workflow below.", icon="✍️")

    # A clicked follow-up suggestion queued on the previous run.
    pending = st.session_state.pop(PENDING_FOLLOWUP_KEY, None)
    conversation = st.session_state.get(CONVERSATION_KEY)
    if pending and conversation is not None and conversation.has_active_result:
        handle_followup(pending, conversation, as_of=AS_OF)
        _sync_workspace(conversation)

    conversation = st.session_state.get(CONVERSATION_KEY)
    response: AssistantResponse | None = st.session_state.get(RESPONSE_KEY)

    if conversation is not None and conversation.has_active_result:
        st.divider()
        _render_conversation(conversation)
        _render_followup_suggestions(conversation)
        follow = st.chat_input("Ask a follow-up, or start a new request…")
        if follow and follow.strip():
            handle_followup(follow, conversation, as_of=AS_OF)
            _sync_workspace(conversation)
            st.rerun()
    elif response is not None:
        st.divider()
        _render_response(response)

    st.divider()
    _render_cards(workflows)

    st.info(
        "Prototype on synthetic data. Forecasts are a **configured simulation**, not a "
        "trained prediction, and no price can be published from this application.",
        icon="ℹ️",
    )


# --- conversation ---------------------------------------------------------------------


def _start_conversation(request: str) -> None:
    """Run the first turn and open a fresh conversation around any workflow result.

    Every question that produces a structured result — an aging analysis, a valuation, a
    portfolio forecast, or an event plan — enters the multi-turn thread, so follow-ups and
    workflow switching are available from any starting point. A clarification or error with no
    structured result stays on the single-turn path."""
    st.session_state[QUESTION_KEY] = request
    response = run_assistant(request, as_of=AS_OF)
    st.session_state[RESPONSE_KEY] = response
    if response.resolved_vehicle_id:
        st.session_state[SELECTED_VEHICLE_KEY] = response.resolved_vehicle_id

    first_result = response.improve_aging if response.improve_aging is not None else response.result
    if first_result is not None:
        conversation = new_state()
        conversation.add_user(request)
        conversation.add_assistant(response.message, SOURCE_FIRST_TURN,
                                   result=first_result, response=response)
        conversation.adopt_response(response)
        st.session_state[CONVERSATION_KEY] = conversation
        _sync_workspace(conversation)
    else:
        # No structured result to explore (clarification / no-match / error) — single-turn path.
        st.session_state.pop(CONVERSATION_KEY, None)
        st.session_state.pop("improve_aging_result", None)


def _sync_workspace(conversation) -> None:
    """Keep the workspace(s) pointed at the conversation's active result, so opening a workflow
    page (and returning) preserves context. The aging result seeds the Improve Aging workspace;
    a valuation switch seeds the Price Inventory preselect."""
    if conversation.active_workflow_type == "IMPROVE_AGING_INVENTORY" \
            and conversation.active_result is not None:
        st.session_state["improve_aging_result"] = conversation.active_result
    elif conversation.active_workflow_type == "PRICE_INVENTORY" and conversation.active_vehicle_ids:
        st.session_state[SELECTED_VEHICLE_KEY] = conversation.active_vehicle_ids[0]


def _render_conversation(conversation) -> None:
    """The full turn-by-turn thread. Prior turns are never erased; the first turn renders the
    Slice-1 rich answer, a workflow switch renders the new workflow's result, and other turns
    render their grounded text with a provenance chip."""
    last_user_text = ""
    for message in conversation.messages:
        with st.chat_message(message.role):
            if message.role == "user":
                last_user_text = message.text
                st.markdown(md(message.text))
            elif message.source == SOURCE_FIRST_TURN and message.response is not None:
                _render_workflow_result(message.response, last_user_text)
            elif message.source == SOURCE_SWITCH and message.response is not None:
                st.caption(_PROVENANCE[SOURCE_SWITCH])
                st.markdown(md(message.text))
                _render_workflow_result(message.response, last_user_text)
            elif message.source in PRICING_FOLLOWUP_SOURCES and message.payload is not None:
                st.caption(_PROVENANCE.get(message.source, ""))
                _render_pricing_followup(message.payload)
            elif message.source in PORTFOLIO_FOLLOWUP_SOURCES and message.payload is not None:
                st.caption(_PROVENANCE.get(message.source, ""))
                _render_portfolio_followup(message.payload)
            else:
                caption = _PROVENANCE.get(message.source)
                if caption:
                    st.caption(caption)
                st.markdown(md(message.text))
                if message.source == SOURCE_RERUN and message.response is not None \
                        and message.response.target_url:
                    _open_workflow_link(message.response.target_url,
                                        "Open the updated workspace →")


def _render_workflow_result(response: AssistantResponse, entry_text: str = "") -> None:
    """Render a workflow's normal result inside the thread — used for the first turn and for a
    later workflow switch. Dispatches on the response's workflow context. `entry_text` is the
    question that opened the turn, so the portfolio result can open on today vs the outlook."""
    if response.workflow is WorkflowContext.PRICE_INVENTORY:
        _render_pricing_result(response)
    elif response.workflow is WorkflowContext.ACQUIRE_INVENTORY:
        _render_portfolio_result(response, entry_text)
    elif response.workflow is WorkflowContext.MERCHANDISE_INVENTORY:
        _render_promotion_result(response)
    elif response.workflow is WorkflowContext.IMPROVE_AGING_INVENTORY:
        _render_improve_aging_result(response, show_followups=False)


def _render_followup_suggestions(conversation) -> None:
    """Clickable, result-relevant follow-ups. Clicking queues the question as a follow-up on the
    next run; the workspace suggestion is handled by the page link, not a rerun.

    The clickable suggestions come from the aging answer model, so they render only for an active
    aging result. Other workflows still support typed follow-ups via the chat box; they just have
    no curated suggestion chips yet (Tier-2 work)."""
    if conversation.active_workflow_type != "IMPROVE_AGING_INVENTORY":
        return
    answer = build_aging_answer(conversation.active_result,
                                workspace_url=conversation.active_response.target_url
                                if conversation.active_response else None)
    if answer is None or not answer.suggested_followups:
        return
    st.caption("Try a follow-up:")
    columns = st.columns(3)
    slot = 0
    for index, question in enumerate(answer.suggested_followups):
        if question.lower().startswith("open"):
            continue  # the workspace link is rendered inside the turn, not as a follow-up
        if columns[slot % 3].button(question, key=f"followup_{index}"):
            st.session_state[PENDING_FOLLOWUP_KEY] = question
            st.rerun()
        slot += 1


# --- the six states -------------------------------------------------------------------


def _render_response(response: AssistantResponse) -> None:
    question = st.session_state.get(QUESTION_KEY, "")
    if question:
        st.caption(f"You asked: _{question[:160]}_")

    dispatch = {
        AssistantState.ROUTED_AND_EXECUTED: _render_executed,
        AssistantState.NEEDS_CLARIFICATION: _render_clarification,
        AssistantState.NO_MATCH: _render_no_match,
        AssistantState.AMBIGUOUS_MATCH: _render_ambiguous,
        AssistantState.WORKFLOW_NOT_YET_AVAILABLE: _render_not_available,
        AssistantState.EXECUTION_ERROR: _render_error,
        # Phase 5 — Improve Aging orchestration states.
        AssistantState.PARTIAL_RESULT: _render_executed,
        AssistantState.TARGET_NOT_ACHIEVABLE: _render_executed,
        AssistantState.NO_SAFE_ACTIONS: _render_clarification,
    }
    dispatch[response.state](response)

    _render_route_detail(response)


def _workflow_label(response: AssistantResponse) -> str:
    return response.workflow.label if response.workflow else "—"


def _render_executed(response: AssistantResponse) -> None:
    if getattr(response, "advice", None) is not None:
        _render_advice(response)
        return
    if response.workflow is WorkflowContext.PRICE_INVENTORY:
        _render_pricing_result(response)
    elif response.workflow is WorkflowContext.ACQUIRE_INVENTORY:
        _render_portfolio_result(response)
    elif response.workflow is WorkflowContext.MERCHANDISE_INVENTORY:
        _render_promotion_result(response)
    elif response.workflow is WorkflowContext.IMPROVE_AGING_INVENTORY:
        _render_improve_aging_result(response)


def _render_advice(response: AssistantResponse) -> None:
    """The single-vehicle AI orchestrator result: intent + capabilities invoked, a prioritized
    action plan with evidence and trade-offs, and a human approval gate. The orchestrator computed
    nothing — every figure is copied from one of the three skills."""
    adv = response.advice
    st.success(f"**AI Workflow** — one question, three business capabilities coordinated.", icon="🧭")
    c1, c2 = st.columns([1, 2])
    c1.caption("Intent detected")
    c1.markdown(f"**{adv.intent}**")
    c2.caption("Business capabilities invoked")
    c2.markdown("　".join(f"✅ {cap}" for cap in adv.capabilities_invoked))

    st.markdown(f"### Recommended action plan — {adv.description}")
    st.caption("Synthesized from the three skills; the AI orders and explains, it does not compute.")
    for a in adv.actions:
        with st.container(border=True):
            st.markdown(f"**Priority {a.priority} · {a.title}**")
            if a.detail:
                st.caption(a.detail)
            for e in a.evidence:
                st.markdown(f"- {e}")
            tcols = st.columns(2)
            tcols[0].markdown("**Pros**  \n" + "  \n".join(f"• {p}" for p in a.pros))
            tcols[1].markdown("**Cons**  \n" + "  \n".join(f"• {c}" for c in a.cons))
            st.caption("From: " + " · ".join(a.source_skills))
    st.info(f"Review again in {adv.review_in_days} days.", icon="🗓️")

    st.markdown("**Recommended next action**")
    b1, b2 = st.columns([1, 2])
    if b1.button("Approve recommendation", type="primary", key="advice_approve"):
        st.success("Approved — routed to the manager's queue. (Prototype: nothing is written back "
                   "to inventory; the human remains the decision-maker.)")
    if response.target_url:
        with b2:
            _open_workflow_link(response.target_url, "Open the full analysis →")


def _render_improve_aging_result(response: AssistantResponse, *, show_followups: bool = True) -> None:
    """The direct, grounded answer — the actual vehicles and their recommended actions, in the
    conversation. The full evidence, plan comparison, and audit live on the workspace, linked
    at the end. Every value is read from the structured result; nothing is computed here.

    `show_followups` is False inside a conversation thread, where one clickable suggestion block
    is rendered once beneath the whole thread instead of after every turn."""
    s = response.summary
    icon = "✅" if response.state is AssistantState.ROUTED_AND_EXECUTED else "🚫"
    st.markdown(f"{icon} **Improve Aging Inventory** — {md(response.message)}")

    answer = build_aging_answer(response.improve_aging, workspace_url=response.target_url)
    if answer is None:
        # No deep analysis available (e.g. an unresolved event stopped the workflow early).
        if response.target_url:
            _open_workflow_link(response.target_url, "Open the full Improve Aging workspace →")
        return

    # A compact orientation row, then the actual answer.
    target = s.get("target_status", "NO_EVENT")
    util, tgt = s.get("current_utilization"), s.get("target_utilization")
    c1, c2, c3 = st.columns(3)
    c1.metric(
        "Lot capacity used → target",
        f"{util:.0%}" if isinstance(util, (int, float)) else "—",
        (f"target {tgt:.0%}" if isinstance(tgt, (int, float)) else "no target"),
        delta_color="off",
    )
    c2.metric("Target likelihood",
              "No event" if target == "NO_EVENT" else T.feasibility_label(target))
    c3.metric("Aging vehicles analysed", answer.analysed_count,
              f"{answer.immediate_count} need action now", delta_color="off")

    st.markdown(f"**{md(answer.understood)}**")

    if answer.immediate:
        st.markdown(f"**{answer.immediate_count} need immediate action:**")
        for v in answer.immediate:
            st.markdown(
                f"- **{md(v.description)}** — {md(v.action_label)}  \n"
                f"  _Reason: {md(v.reason)}_"
            )

    if answer.no_immediate:
        st.markdown(
            f"**{answer.no_immediate_count} do not need immediate action "
            "but may be sale-event candidates:**"
        )
        for v in answer.no_immediate:
            st.markdown(f"- {md(v.description)}")

    if answer.event_selected and answer.event_block:
        _render_event_block(answer.event_block)
    else:
        st.info(md(answer.promotion_note), icon="🗓️")

    # Default review copy is vehicle-based — the raw record count lives in the audit expander.
    if answer.review_vehicle_count:
        st.caption(f"🔍 {md(answer.key_review_note)}")
    if s.get("recommended_plan"):
        st.caption(f"Recommended approach **{T.plan_name(s['recommended_plan'])}**.")

    # Top one or two warnings only — the workspace shows the full set.
    for warning in response.warnings[:2]:
        st.caption(f"⚠️ {md(T.warning_label(warning.get('code', '')))} — "
                   f"{md(str(warning.get('message', '')))[:110]}")

    _render_approval_details(answer)

    if show_followups and answer.suggested_followups:
        st.markdown("**You could ask next:**")
        for q in answer.suggested_followups:
            st.markdown(f"- _{md(q)}_")

    if response.target_url:
        _open_workflow_link(response.target_url, "Open the full Improve Aging workspace →")
        st.caption("The workspace shows the diagnosis, per-vehicle evidence, plan comparison, "
                   "and the execution trace for this request.")


def _render_event_block(block) -> None:
    """The extra distinctions a selected event makes — promoted vs analysed-not-selected vs
    protected/excluded, plus target likelihood and the recommended approach."""
    if block.promoted:
        st.markdown(f"**{len(block.promoted)} recommended for the "
                    f"{md(block.event_name or 'sale')} event:**")
        for v in block.promoted:
            st.markdown(f"- {md(v.description)}")
    if block.probability_target_achieved is not None:
        st.caption(
            f"Target likelihood **{T.feasibility_label(block.target_status or '')}** — "
            f"reaches the target **{block.probability_target_achieved:.0%}** of the time. "
            "A plan improves the odds; it does not guarantee sales."
        )


def _render_approval_details(answer) -> None:
    """Progressive disclosure: the default surfaces show only the unique vehicle count. The raw
    review-condition record count and the per-vehicle breakdown live here, in the audit view."""
    if not answer.review_vehicle_count:
        return
    with st.expander("View approval details"):
        st.markdown(
            f"- **Vehicles requiring review:** {answer.review_vehicle_count}\n"
            f"- **Vehicles assigned to manager review:** {answer.manager_review_count}\n"
            f"- **Review conditions triggered:** {answer.review_item_count}"
        )
        st.caption(
            "“Review conditions triggered” counts the individual approval-condition records "
            "behind these vehicles — not separate dealer decisions. The workspace lists each "
            "condition with its raw code and the vehicle it belongs to."
        )


def _render_pricing_result(response: AssistantResponse) -> None:
    s = response.summary
    st.success(
        f"**{s.get('vehicle', response.resolved_vehicle_id)}** — priced against the local "
        f"market (`{response.resolved_vehicle_id}`).",
        icon="✅",
    )
    if s.get("vin"):
        st.caption(f"VIN {s['vin']}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Current asking price", _money(s.get("current_list_price")))
    c2.metric("Recommended asking price", _money(s.get("recommended_price")))
    c3.metric(
        "Expected days to sale (P50)", f"{s.get('p50_days_to_sale', 0):.0f}",
        f"Conservative (P90) {s.get('p90_days_to_sale', 0):.0f}", delta_color="off",
    )
    c4.metric("Break-even price", _money(s.get("break_even_price")))

    st.caption(
        f"Safe room for an additional discount {_money(s.get('promotional_headroom'))} · "
        f"recommended pricing approach **{T.strategy_name(str(s.get('strategy', '')))}**. "
        "Every figure is read from the analysis — the assistant computes nothing."
    )

    # Advisor WHY — reads like an inventory advisor, not a calculator. Built from stored fields.
    if isinstance(response.result, dict):
        from pricing_agent.agents import pricing_copy as PC
        why = PC.first_turn_why(response.result)
        if why:
            st.markdown("**Why this price**")
            for bullet in why:
                st.markdown(md(f"- {bullet}"))

    _render_warnings(response)

    if response.target_url:
        _open_workflow_link(response.target_url, "Open the full Price Inventory analysis →")
        st.caption("Opens the workspace with this vehicle already selected.")


# --- pricing follow-up rendering (structured payloads from pricing_followup) -----------


def _render_pricing_followup(payload: dict) -> None:
    kind = payload.get("kind")
    if kind == "target":
        _render_pricing_target(payload)
    elif kind == "strategies":
        _render_pricing_strategies(payload)
    elif kind == "detail":
        _render_pricing_detail(payload)
    elif kind == "reasoning":
        _render_pricing_reasoning(payload)


def _render_pricing_target(p: dict) -> None:
    if p.get("no_rung"):
        st.warning("I can't model a safe price for that target — the price floor binds, so I've "
                   "kept the current recommendation.", icon="⚠️")
        return
    prev, tgt = p["previous"], p["target"]
    if p["reached"]:
        st.markdown(md(
            f"To give the best chance of selling within **~{p['target_days']} days**, I'd move to "
            f"**{_money(tgt['price'])}**. This is the *minimum discount* — the least aggressive safe "
            "price change currently modeled that reaches your target."))
    else:
        st.markdown(md(
            f"The requested **~{p['target_days']} days** can't be reached within the safe price "
            f"floor. The fastest safe price is **{_money(tgt['price'])}** at about "
            f"**{tgt['p50_days']:.0f} days** — the pricing floor prevents going faster."))

    c1, c2, c3 = st.columns(3)
    c1.metric("Previous price", _money(prev.get("price")),
              f"{prev.get('p50_days', 0):.0f} days (P50)", delta_color="off")
    c2.metric("Target price", _money(tgt["price"]),
              f"{tgt['p50_days']:.0f} days (P50)", delta_color="off")
    dprice = p.get("price_change")
    dgd = p.get("days_change")
    c3.metric("Change", _money(dprice) if dprice is not None else "—",
              (f"{dgd:+.0f} days" if dgd is not None else ""), delta_color="off")

    c4, c5 = st.columns(2)
    c4.metric("Expected net economic value", _money(tgt.get("net_economic_value")),
              "after holding, depreciation & slot cost", delta_color="off")
    c5.metric("Downside protection", _money(p.get("cushion_above_break_even")),
              f"above {_money(p.get('break_even_price'))} break-even", delta_color="off")

    if p.get("material_compression"):
        st.warning("The faster target materially reduces expected net value and leaves less "
                   "downside protection above break-even.", icon="⚠️")
    st.caption(md(f"_{p.get('note', '')}_  The baseline recommendation is unchanged; this is an "
                 "exploration."))


def _render_pricing_strategies(p: dict) -> None:
    st.markdown("**Three pricing strategies**")
    rows = []
    for m in p["rows"]:
        rows.append({
            "Strategy": m["label"],
            "Recommended price": _money(m["proposed_list_price"]),
            "Expected days (P50)": f"{(m['p50_days'] or 0):.0f}",
            "Expected gross (P50)": _money(m["p50_gross"]),
            "Pros": m["pros"],
            "Cons": m["cons"],
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True)
    st.caption(md(p.get("summary", "")))


def _render_pricing_detail(p: dict) -> None:
    st.success(p["notice"], icon="🧾")
    m = p["metrics"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Current asking price", _money(m.get("current_list_price")))
    c2.metric("Recommended price", _money(m.get("recommended_price")))
    c3.metric("Break-even price", _money(m.get("break_even_price")))
    c4, c5, c6 = st.columns(3)
    c4.metric("Expected front-end gross (P50)", _money(m.get("p50_gross")))
    c5.metric("Expected holding cost (P50)", _money(m.get("p50_holding_cost")))
    c6.metric("Expected depreciation (P50)", _money(m.get("p50_depreciation")))
    c7, c8, c9 = st.columns(3)
    c7.metric("Expected days to sale (P50)", f"{(m.get('p50_days') or 0):.0f}",
              f"Conservative (P90) {(m.get('p90_days') or 0):.0f}", delta_color="off")
    rating = str(m.get("deal_rating") or "").title()
    pct = m.get("market_percentile")
    c8.metric("Market position", rating or "—",
              (f"{int(pct)}th percentile" if pct is not None else ""), delta_color="off")
    conf = m.get("confidence_level")
    score = m.get("confidence_score")
    c9.metric("Confidence", str(conf).title() if conf else "—",
              (f"score {score:.0f}" if isinstance(score, (int, float)) else ""), delta_color="off")

    for bullet in p.get("why", []):
        st.markdown(md(f"- {bullet}"))


def _render_pricing_reasoning(p: dict) -> None:
    st.markdown(f"**Why {p.get('label', 'this strategy')} — my reasoning**")
    for i, (title, body) in enumerate(p.get("steps", []), start=1):
        st.markdown(md(f"**Step {i} · {title}**  \n{body}"))
    with st.container(border=True):
        st.markdown(f"**Next checkpoint**  \n{md(p.get('next_checkpoint', ''))}")


@st.cache_data(show_spinner=False)
def _inventory_by_id(as_of: datetime) -> dict:
    """vehicle_id → static inventory metadata (description, days, price, photo) for presentation
    enrichment only — never a re-run of any analysis."""
    data = VautoClient(MockTransport(as_of=as_of)).get_dealer_inventory().data or []
    return {v["vehicle_id"]: v for v in data}


# A forward-looking opener ("next 30 days", "forecast") opens on the outlook; anything else opens on
# the lot today — present before future, the natural dealer journey.
_ENTRY_OUTLOOK = re.compile(
    r"next\s+\d+\s+(day|week|month)|\b(30|90|thirty|ninety)\s*[- ]?day|forecast|outlook|"
    r"look\s+like\s+in|expected\s+to\s+sell|going\s+to\s+(sell|do)|\bwhat\s+will\b", re.I)


def _render_portfolio_result(response: AssistantResponse, entry_text: str = "") -> None:
    """First ACQUIRE turn. Adapts to how the dealer opened the conversation: a forward-looking
    question shows the 30-day outlook; anything else opens on the lot today."""
    from pricing_agent.agents import portfolio_copy as PC
    result = response.result if isinstance(response.result, dict) else {}
    if _ENTRY_OUTLOOK.search(entry_text or ""):
        st.success("Here's what your lot is expected to do over the next 30 days — a forecast for "
                   "existing inventory only, so it's a cautious lower estimate.", icon="🔮")
        k = PC.outlook_kpis(result)
        st.markdown(md(PC.outlook_summary(k)))
        _portfolio_outlook_body(k)
    else:
        st.success("Here's where your lot stands today.", icon="📊")
        k = PC.lot_today_kpis(result)
        st.markdown(md(PC.lot_today_summary(k)))
        _portfolio_lot_today_body(k, PC.select_top_risk(result, None))
    if response.target_url:
        _open_workflow_link(response.target_url, "Open the full Acquire Inventory analysis →")


def _portfolio_outlook_body(k: dict) -> None:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🔮 Expected vehicles sold (P50)", f"{(k['sold_p50'] or 0):.0f}",
              (f"range {k['sold_p10']:.0f}–{k['sold_p90']:.0f}" if k.get("sold_p10") is not None else ""),
              delta_color="off")
    c2.metric("💵 Expected revenue (P50)", _money(k["revenue_p50"]),
              (f"downside {_money(k['revenue_p10'])}" if k.get("revenue_p10") is not None else ""),
              delta_color="off")
    c3.metric("💰 Expected front-end gross (P50)", _money(k["gross_p50"]))
    c4.metric("📊 Expected lot capacity used (P50)", f"{(k['capacity_used_p50'] or 0):.0%}")
    if k.get("below_target_prob"):
        # _money already escapes '$' for markdown — don't wrap it in md() again (double-escape).
        st.markdown(f"**{k['below_target_prob']:.0%}** chance revenue falls below the "
                    f"**{_money(k['revenue_target'])}** target.")
    if k.get("revenue_p50") is not None and k.get("revenue_p10") is not None:
        ys = [k["revenue_p10"], k["revenue_p50"], k["revenue_p90"]]
        fig = go.Figure(go.Bar(
            x=["Downside (P10)", "Expected (P50)", "Conservative (P90)"], y=ys,
            text=[f"${v:,.0f}" for v in ys], textposition="outside", cliponaxis=False))
        fig.update_layout(height=260, yaxis_title="Revenue, $", margin=dict(t=24, b=10),
                          showlegend=False)
        st.plotly_chart(style_fig(fig))


def _portfolio_lot_today_body(k: dict, vehicles: list) -> None:
    from pricing_agent.agents import portfolio_copy as PC
    c = st.columns(5)
    c[0].metric("🚗 Units on lot", k.get("units_on_lot", "—"),
                f"{k.get('open_slots', 0)} open", delta_color="off")
    util, tgt = k.get("utilization"), k.get("target_utilization")
    c[1].metric("📊 Utilization", f"{(util or 0):.0%}",
                (f"{(util - tgt) * 100:+.0f} pts vs {tgt:.0%}" if util is not None and tgt is not None else ""),
                delta_color="off")
    c[2].metric("⏳ Over 90 days", k.get("over_90_units", "—"),
                f"{(k.get('aged_pct') or 0):.0%} of lot", delta_color="off")
    c[3].metric("💰 Cash tied up", _money(k.get("cash_tied_up")))
    c[4].metric("🔻 Below break-even", k.get("below_break_even", "—"),
                _money(k.get("below_break_even_exposure")), delta_color="off")

    inv = _inventory_by_id(AS_OF)
    rows = []
    for v in vehicles:
        veh = inv.get(v["vehicle_id"], {})
        dot, label = PC.action_label(v.get("action_code"))
        rows.append({
            "Photo": ui_components.thumbnail_uri(veh.get("image_url")),
            "Suggested action": f"{dot} {label}",
            "Stock": v["vehicle_id"],
            "Vehicle": f"{veh.get('year', '')} {veh.get('make', '')} {veh.get('model', '')}".strip(),
            "Days on lot": veh.get("days_in_inventory"),
            "Asking price": veh.get("current_list_price"),
            "Risk": v.get("risk_score"),
            "Risk of over 90 days": _pct_or_none(v.get("prob_age_over_90")),
            "Chance of negative value": _pct_or_none(v.get("prob_negative_net_value")),
        })
    st.dataframe(
        pd.DataFrame(rows), hide_index=True,
        column_config={
            "Photo": st.column_config.ImageColumn("Photo", width="small"),
            "Asking price": st.column_config.NumberColumn(format="$%d"),
            "Risk": st.column_config.ProgressColumn(format="%.0f", min_value=0, max_value=100),
            "Risk of over 90 days": st.column_config.NumberColumn(format="%.0f%%"),
            "Chance of negative value": st.column_config.NumberColumn(format="%.0f%%"),
        },
    )
    st.caption("Ranked by risk of remaining unsold too long — time on lot, expected value loss, and "
               "chance of a negative outcome.")


# --- portfolio follow-up rendering (structured payloads from portfolio_followup) ------


def _render_portfolio_followup(payload: dict) -> None:
    kind = payload.get("kind")
    if kind == "outlook":
        st.markdown(md(payload["summary"]))
        _portfolio_outlook_body(payload["kpis"])
    elif kind == "lot_today":
        st.markdown(md(payload["summary"]))
        _portfolio_lot_today_body(payload["kpis"], payload["vehicles"])
    elif kind == "top_risk":
        _render_portfolio_top_risk(payload)
    elif kind == "acquire_review":
        _render_portfolio_acquire_review(payload)


def _pct_or_none(fraction) -> float | None:
    return None if fraction is None else fraction * 100.0


def _render_portfolio_top_risk(p: dict) -> None:
    from pricing_agent.agents import portfolio_copy as PC
    st.markdown(md(p["summary"]))
    inv = _inventory_by_id(AS_OF)
    for v in p["vehicles"]:
        veh = inv.get(v["vehicle_id"], {})
        with st.container(border=True):
            photo_col, body_col = st.columns([1, 4])
            path = ui_components.resolve_image(veh.get("image_url"))
            if path is not None:
                with photo_col:
                    try:
                        components.html(ui_components.vehicle_photo_html(path, height=96), height=104)
                    except OSError:
                        pass
            desc = f"{veh.get('year', '')} {veh.get('make', '')} {veh.get('model', '')}".strip() \
                or v["vehicle_id"]
            days = veh.get("days_in_inventory")
            meta = f"{v['vehicle_id']}" + (f" · {days} days on lot" if days is not None else "")
            dot, label = PC.action_label(v.get("action_code"))
            reasons = "  \n".join(f"{PC.reason_icon(f)} {f}" for f in v.get("risk_factors", ()))
            body_col.markdown(md(
                f"**{desc}**  ·  {meta}  ·  🎯 **attention score {(v.get('risk_score') or 0):.0f}**  \n"
                f"{reasons}  \n**Suggested inventory action:** {dot} {label}"))


def _render_portfolio_acquire_review(p: dict) -> None:
    st.markdown(md(p["summary"]))
    warnings = p.get("warnings", [])
    if warnings:
        n = len(warnings)
        render_review_panel(
            warnings,
            heading=f"{n} thing{'s' if n != 1 else ''} to clear before you acquire")
    if p.get("cta"):
        st.markdown(md(p["cta"]))
        _open_workflow_link("improve-aging-inventory", "Open Improve Aging →")


def _render_promotion_result(response: AssistantResponse) -> None:
    s = response.summary
    st.success(f"Built a sale-event plan for the **{s.get('event_name')}** event.", icon="✅")

    c1, c2, c3 = st.columns(3)
    c1.metric("Target likelihood", T.feasibility_label(str(s.get("feasibility_status", ""))))
    c2.metric("Additional sales needed", s.get("incremental_required", "—"))
    c3.metric("Reaches the target", f"{s.get('probability_target_achieved', 0):.0%}")

    st.caption(
        f"Target ending inventory {s.get('target_ending_inventory')} · recommended approach "
        f"**{T.plan_name(str(s.get('recommended_plan', '')))}**. A plan improves the odds; it "
        "does not guarantee sales."
    )

    _render_warnings(response)

    if response.target_url:
        _open_workflow_link(response.target_url, "Open the full Merchandise Inventory plan →")


def _render_clarification(response: AssistantResponse) -> None:
    st.warning(response.message, icon="❓")
    if response.target_url:
        _open_workflow_link(response.target_url, "Open the workflow →")


def _render_no_match(response: AssistantResponse) -> None:
    st.error(response.message, icon="🚫")
    if response.target_url:
        _open_workflow_link(response.target_url, "Pick a vehicle in Price Inventory →")


def _render_ambiguous(response: AssistantResponse) -> None:
    st.warning(response.message, icon="🔀")
    for candidate in response.candidates:
        label = (
            f"{candidate.get('year')} {candidate.get('make')} {candidate.get('model')} "
            f"{candidate.get('trim')}".strip()
        )
        cols = st.columns([3, 2, 2])
        cols[0].markdown(f"**{label}** · `{candidate.get('vehicle_id')}`")
        cols[1].caption(f"{candidate.get('mileage'):,} mi" if candidate.get("mileage") else "—")
        if cols[2].button("Analyze this one", key=f"pick_{candidate.get('vehicle_id')}"):
            # Open a conversation around the picked vehicle, same as typing it directly — so the
            # dealer lands in the multi-turn thread with a follow-up box, not a single-turn view.
            _start_conversation(str(candidate.get("vehicle_id")))
            st.rerun()


def _render_not_available(response: AssistantResponse) -> None:
    st.info(response.message, icon="🚧")
    if response.target_url:
        _open_workflow_link(response.target_url, "See the Improve Aging sequence →")


def _render_error(response: AssistantResponse) -> None:
    st.error(response.message, icon="⚠️")


def _render_warnings(response: AssistantResponse) -> None:
    if not response.warnings:
        return
    with st.expander(f"Top warnings ({len(response.warnings)})"):
        for warning in response.warnings:
            dot = severity_dot(warning.get("severity"))
            st.markdown(md(
                f"{dot} **{T.warning_label(warning.get('code', ''))}** — "
                f"{warning.get('message', '')}"
            ))


def _render_route_detail(response: AssistantResponse) -> None:
    route = response.route
    with st.expander("How this was routed"):
        st.markdown(
            f"- Detected workflow: **{_workflow_label(response)}**\n"
            f"- Skill: `{response.skill or '—'}`\n"
            f"- Confidence: **{route.confidence.value}**\n"
            f"- Reason codes: {', '.join(f'`{code}`' for code in route.reason_codes) or '—'}\n"
            f"- Extracted entities: `{route.extracted_entities or '—'}`\n"
            f"- Missing fields: {', '.join(route.missing_fields) or '—'}\n"
            f"- Ambiguous fields: {', '.join(route.ambiguous_fields) or '—'}"
        )
        st.caption(
            "Deterministic rules only. No model was called, and no figure above the "
            "workflow line was generated by the router."
        )


def _render_cards(workflows: Iterable[WorkflowCard]) -> None:
    cards = list(workflows)
    if not cards:
        return
    st.subheader("Dealer workflows")
    st.caption("Open any of these directly from the sidebar.")
    columns = st.columns(2)
    for index, card in enumerate(cards):
        with columns[index % 2].container(border=True):
            st.markdown(f"**{card.icon} {card.display_name}**")
            st.caption(card.description)
            if card.skills:
                st.caption("Uses: " + ", ".join(card.skills))
            if card.availability != "AVAILABLE":
                st.caption(f"Status: {card.availability.replace('_', ' ').title()}")
