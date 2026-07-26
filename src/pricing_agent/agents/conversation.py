"""Conversation state and deterministic reference resolution for multi-turn follow-ups.

This is the memory the Dealer AI Assistant keeps between turns. It is **not** an LLM's free-form
recollection: the active structured result is the source of truth, and every reference the dealer
makes — "the BMW", "those two vehicles", "the wholesale vehicles", "the recommended plan", "the
same event" — is resolved deterministically against that result. Nothing here computes a number;
it stores, indexes, and selects what the workflow already produced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from pricing_agent.agents.aging_answer import IMMEDIATE_ACTIONS
from pricing_agent.agents.router import parse_vehicle

# --- messages & state -----------------------------------------------------------------

# Where an assistant turn's content came from — shown to the dealer as provenance, and asserted
# by tests so the honest label can never drift from the behaviour.
SOURCE_USER = "user"
SOURCE_FIRST_TURN = "first_turn"
SOURCE_RERUN = "rerun"
SOURCE_EXPLANATION = "explanation"
SOURCE_FILTERED = "filtered_result"
SOURCE_EXISTING = "existing_result"
SOURCE_CLARIFICATION = "clarification"
SOURCE_UNSUPPORTED = "unsupported"
SOURCE_ERROR = "error"
SOURCE_SWITCH = "workflow_switch"
# Single-vehicle pricing conversation turns, each rendered by its own structured payload.
SOURCE_PRICING_TARGET = "pricing_target"          # "sell it within N days"
SOURCE_PRICING_STRATEGIES = "pricing_strategies"  # "show me a few strategies"
SOURCE_PRICING_DETAIL = "pricing_detail"          # "I like Balanced — show the detail"
SOURCE_PRICING_REASONING = "pricing_reasoning"    # "walk me through your reasoning"

PRICING_FOLLOWUP_SOURCES = frozenset({
    SOURCE_PRICING_TARGET, SOURCE_PRICING_STRATEGIES, SOURCE_PRICING_DETAIL,
    SOURCE_PRICING_REASONING,
})

# Inventory-portfolio (Acquire) conversation turns, each rendered by its own structured payload.
SOURCE_PORTFOLIO_LOT_TODAY = "portfolio_lot_today"          # "what does my lot look like today?"
SOURCE_PORTFOLIO_TOP_RISK = "portfolio_top_risk"           # "top N vehicles that need attention"
SOURCE_PORTFOLIO_ACQUIRE_REVIEW = "portfolio_acquire_review"  # "before I acquire, what to review?"

PORTFOLIO_FOLLOWUP_SOURCES = frozenset({
    SOURCE_PORTFOLIO_LOT_TODAY, SOURCE_PORTFOLIO_TOP_RISK, SOURCE_PORTFOLIO_ACQUIRE_REVIEW,
})

RICH_SOURCES = frozenset({SOURCE_FIRST_TURN, SOURCE_RERUN})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ConversationMessage:
    role: str
    source: str
    text: str = ""
    # Set for rich turns (first_turn / rerun) so history re-renders faithfully even after a
    # later rerun changes the active result. Plain objects to avoid annotation-time imports.
    result: object | None = None
    response: object | None = None
    referenced_vehicle_ids: tuple[str, ...] = ()
    referenced_workflow_id: str | None = None
    # Structured render data for pricing follow-up turns (a plain dict; the view renders it as
    # cards/tables). None for plain-text turns.
    payload: dict | None = None
    timestamp: str = field(default_factory=_now)


@dataclass(frozen=True)
class PriorWorkflow:
    """A previously active workflow, preserved when the conversation switches to a new one so a
    valid earlier result is never discarded."""
    workflow_type: str | None
    workflow_id: str | None
    request_id: str | None
    result: object | None
    response: object | None
    timestamp: str = field(default_factory=_now)


@dataclass
class ConversationState:
    conversation_id: str
    messages: list[ConversationMessage] = field(default_factory=list)
    active_workflow_type: str | None = None
    active_workflow_id: str | None = None
    active_request_id: str | None = None
    active_result: object | None = None
    active_response: object | None = None
    active_vehicle_ids: tuple[str, ...] = ()
    active_event: str | None = None
    active_target_utilization: float | None = None
    active_plan: str | None = None
    active_warnings: tuple[dict, ...] = ()
    active_approvals: tuple[dict, ...] = ()
    active_simulation_ids: tuple[str, ...] = ()
    last_user_request: str | None = None
    last_assistant_response: str | None = None
    pending_clarification: str | None = None
    previous_valid_result: object | None = None
    rerun_count: int = 0
    last_referenced_vehicle_ids: tuple[str, ...] = ()
    prior_workflows: list[PriorWorkflow] = field(default_factory=list)
    # --- single-vehicle pricing conversation memory -------------------------------------
    # Three deliberately distinct concepts (never conflated):
    #   baseline  — the first-turn valuation's recommended strategy; never overwritten by a
    #               day-target exploration.
    #   active    — the most recently *displayed* recommendation (a day target updates this).
    #   selected  — the scenario the dealer explicitly chose (set only on selection).
    baseline_pricing_recommendation: dict | None = None
    active_pricing_recommendation: dict | None = None
    selected_pricing_strategy: str | None = None
    last_pricing_followup_kind: str | None = None
    # --- inventory-portfolio (Acquire) conversation continuity ---------------------------
    # "outlook" on the first ACQUIRE turn, then "lot_today" / "top_risk" / "acquire_review".
    # Continuity/testing only — never a business-data source.
    last_portfolio_followup_kind: str | None = None

    # --- history -------------------------------------------------------------------

    def add_user(self, text: str, *, referenced: tuple[str, ...] = ()) -> None:
        self.last_user_request = text
        self.messages.append(ConversationMessage(
            role="user", source=SOURCE_USER, text=text, referenced_vehicle_ids=referenced))

    def add_assistant(self, text: str, source: str, *, result=None, response=None,
                      referenced: tuple[str, ...] = (), workflow_id: str | None = None,
                      payload: dict | None = None) -> None:
        self.last_assistant_response = text
        self.messages.append(ConversationMessage(
            role="assistant", source=source, text=text, result=result, response=response,
            referenced_vehicle_ids=referenced, referenced_workflow_id=workflow_id,
            payload=payload))

    # --- active result -------------------------------------------------------------

    def adopt(self, response) -> None:
        """Populate every active_* field from an AssistantResponse whose improve_aging is set.

        Copies ids, event, target, plan, warnings, and the raw approval records straight from the
        result — no recomputation. The previously active result is preserved by the caller before
        this is called on a successful rerun."""
        result = getattr(response, "improve_aging", None)
        if result is None:
            return
        summary = getattr(response, "summary", {}) or {}
        req = result.request
        self.active_response = response
        self.active_result = result
        self.active_workflow_type = "IMPROVE_AGING_INVENTORY"
        self.active_workflow_id = result.workflow_id
        self.active_request_id = _first_request_id(result)
        self.active_vehicle_ids = tuple(e.vehicle_id for e in result.vehicle_evidence)
        self.active_event = req.event_name
        self.active_target_utilization = req.target_utilization
        self.active_plan = summary.get("recommended_plan")
        self.active_warnings = tuple(getattr(response, "warnings", ()) or ())
        self.active_approvals = tuple(result.approvals_required)
        self.active_simulation_ids = _simulation_ids(result)

    def adopt_response(self, response) -> None:
        """Adopt any AssistantResponse — the aging orchestration via `adopt`, or a single-skill
        result (e.g. a valuation) via a generic read of its audit block. Copies ids and fields;
        computes nothing."""
        if getattr(response, "improve_aging", None) is not None:
            self.adopt(response)
            return
        result = getattr(response, "result", None) or {}
        audit = result.get("audit", {}) if isinstance(result, dict) else {}
        self.active_response = response
        self.active_result = result or None
        workflow = getattr(response, "workflow", None)
        self.active_workflow_type = workflow.name if workflow is not None else None
        self.active_workflow_id = audit.get("workflow_id") or audit.get("request_id")
        self.active_request_id = audit.get("request_id")
        self.active_vehicle_ids = (
            (response.resolved_vehicle_id,) if getattr(response, "resolved_vehicle_id", None) else ())
        self.active_event = None
        self.active_target_utilization = None
        self.active_plan = None
        self.active_warnings = tuple(getattr(response, "warnings", ()) or ())
        self.active_approvals = tuple(result.get("approvals_required", []) if isinstance(result, dict) else [])
        sim = (audit.get("simulation") or {}).get("simulation_id") if isinstance(audit, dict) else None
        self.active_simulation_ids = (sim,) if sim else ()

        # A fresh valuation resets the pricing conversation memory: its recommended strategy becomes
        # both the baseline and the active recommendation, and any prior selection is cleared. The
        # pricing follow-up handlers update `active`/`selected` later without ever touching baseline.
        if self.active_workflow_type == "PRICE_INVENTORY" and isinstance(result, dict):
            from pricing_agent.agents.pricing_copy import baseline_recommendation
            rec = baseline_recommendation(result)
            self.baseline_pricing_recommendation = rec
            self.active_pricing_recommendation = rec
            self.selected_pricing_strategy = None
            self.last_pricing_followup_kind = None
        else:
            self.baseline_pricing_recommendation = None
            self.active_pricing_recommendation = None
            self.selected_pricing_strategy = None
            self.last_pricing_followup_kind = None

        # A fresh portfolio forecast opens the Acquire conversation at the 30-day outlook.
        self.last_portfolio_followup_kind = (
            "outlook" if self.active_workflow_type == "ACQUIRE_INVENTORY" else None)

    def switch_to(self, response) -> None:
        """Switch the active workflow to `response`, preserving the current one in history first.

        The prior valid result is pushed to `prior_workflows` (never discarded); references that
        only applied to the prior workflow are cleared; then the new response is adopted."""
        if self.active_result is not None:
            self.prior_workflows.append(PriorWorkflow(
                workflow_type=self.active_workflow_type,
                workflow_id=self.active_workflow_id,
                request_id=self.active_request_id,
                result=self.active_result,
                response=self.active_response,
            ))
        self.last_referenced_vehicle_ids = ()
        self.pending_clarification = None
        self.adopt_response(response)

    @property
    def has_active_result(self) -> bool:
        return self.active_result is not None


def new_state() -> ConversationState:
    stamp = int(datetime.now(timezone.utc).timestamp() * 1000) % 1_000_000_000
    return ConversationState(conversation_id=f"conv_{stamp:09d}")


def _first_request_id(result) -> str | None:
    for e in result.vehicle_evidence:
        if e.request_id:
            return e.request_id
    for t in result.trace:
        if t.request_id:
            return t.request_id
    return None


def _simulation_ids(result) -> tuple[str, ...]:
    ids: list[str] = []
    for t in result.trace:
        if t.simulation_id and t.simulation_id not in ids:
            ids.append(t.simulation_id)
    return tuple(ids)


# --- vehicle index --------------------------------------------------------------------


@dataclass(frozen=True)
class VehicleRef:
    vehicle_id: str
    description: str
    action_code: str
    reason_codes: tuple[str, ...]
    warnings: tuple[str, ...]
    approvals: tuple[str, ...]
    current_price: float | None
    analysed: bool
    excluded: bool
    result: dict | None            # the full single-vehicle result, when analysed


def vehicle_index(result) -> dict[str, VehicleRef]:
    """id → VehicleRef for every vehicle in the active result (analysed + excluded). Read-only."""
    evidence = {e.vehicle_id: e for e in result.vehicle_evidence}
    excluded_ids = {e.vehicle_id for e in (result.selection.exclusions if result.selection else ())}
    index: dict[str, VehicleRef] = {}
    for a in result.consolidated_actions:
        vid = a["vehicle_id"]
        ev = evidence.get(vid)
        index[vid] = VehicleRef(
            vehicle_id=vid,
            description=a["description"],
            action_code=a["recommended_action"],
            reason_codes=tuple(a.get("reason_codes", ())),
            warnings=tuple(a.get("warnings", ())),
            approvals=tuple(a.get("approvals_required", ())),
            current_price=a.get("current_price"),
            analysed=ev is not None,
            excluded=vid in excluded_ids,
            result=ev.result if ev else None,
        )
    return index


# --- reference resolution -------------------------------------------------------------

import re  # noqa: E402  (kept near its only users)

_GROUP_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bwholesale\b"), "WHOLESALE_OR_LOSS_MINIMIZATION_REVIEW"),
    (re.compile(r"\bmanager[\s-]?review\b"), "MANAGER_REVIEW"),
)


@dataclass(frozen=True)
class ReferenceMatch:
    ids: tuple[str, ...]
    label: str
    ambiguous: bool = False


def resolve_reference(text: str, state: ConversationState) -> ReferenceMatch:
    """Resolve a dealer reference to vehicle ids against the active result. Deterministic.

    Returns an empty match when nothing is referenced, and sets `ambiguous` (with the candidate
    ids) when a description matches more than one vehicle — the caller then asks, never guesses.
    """
    if not state.has_active_result:
        return ReferenceMatch((), "", False)
    # The reference index only exists for an aging result (a cohort of vehicles). From a single-skill
    # result (e.g. a valuation dict), there is no cohort to resolve against — return empty so the
    # caller resolves the named vehicle from the text instead of crashing on a missing index.
    if state.active_workflow_type != "IMPROVE_AGING_INVENTORY":
        return ReferenceMatch((), "", False)
    lowered = text.lower()
    index = vehicle_index(state.active_result)

    # 1. Explicit id.
    ids = tuple(m.group(0).upper().replace("V", "V-").replace("V--", "V-")
                for m in re.finditer(r"\bV-?\d{4,6}\b", text, re.IGNORECASE))
    ids = tuple(vid for vid in (_norm_id(i) for i in ids) if vid in index)
    if ids:
        return ReferenceMatch(ids, ", ".join(ids))

    # 2. A demonstrative ("those two", "them", "these") refers back to the most recently
    # referenced set — checked before groups so it wins over the standing buckets.
    if re.search(r"\bthose\b|\bthem\b|\bthese\b", lowered) and state.last_referenced_vehicle_ids:
        return ReferenceMatch(state.last_referenced_vehicle_ids, "previously referenced")

    # 3. A specific vehicle by make / model / trim (and year, for disambiguation). Checked
    # before group words so "why is the BMW recommended for wholesale?" is about the BMW, not
    # every wholesale vehicle.
    parsed = parse_vehicle(text)
    tokens = [t for t in (parsed.make, parsed.model, parsed.trim) if t]
    if parsed.year:
        tokens.append(str(parsed.year))
    if tokens:
        matches = [r for r in index.values()
                   if all(tok.lower() in r.description.lower() for tok in tokens)]
        if len(matches) == 1:
            return ReferenceMatch((matches[0].vehicle_id,), matches[0].description)
        if len(matches) > 1:
            # Prefer the vehicle in the current analysis when a duplicate is only in the
            # excluded set (two identical RAV4s, one analysed, one excluded) — that is a
            # principled disambiguation, not an arbitrary pick. Still ambiguous if two
            # analysed vehicles match.
            analysed_hits = [m for m in matches if m.analysed]
            if len(analysed_hits) == 1:
                return ReferenceMatch((analysed_hits[0].vehicle_id,), analysed_hits[0].description)
            return ReferenceMatch(tuple(m.vehicle_id for m in matches),
                                  "; ".join(f"{m.description} ({m.vehicle_id})" for m in matches),
                                  ambiguous=True)

    # 4. Group references.
    analysed = [r for r in index.values() if r.analysed]
    immediate = [r for r in analysed if r.action_code in IMMEDIATE_ACTIONS]
    no_immediate = [r for r in analysed if r.action_code not in IMMEDIATE_ACTIONS]
    excluded = [r for r in index.values() if r.excluded]

    if re.search(r"\bprotected\b|\bexcluded\b", lowered):
        return ReferenceMatch(tuple(r.vehicle_id for r in excluded), "protected or excluded")
    if re.search(r"no[\s-]?immediate|do(?:es)?\s*n['o]?t?\s+need", lowered) and no_immediate:
        return ReferenceMatch(tuple(r.vehicle_id for r in no_immediate), "no-immediate-action")
    if re.search(r"immediate action|\bfive vehicles\b", lowered) and immediate:
        return ReferenceMatch(tuple(r.vehicle_id for r in immediate), "immediate-action")
    for pattern, action in _GROUP_PATTERNS:
        if pattern.search(lowered):
            group = tuple(r.vehicle_id for r in analysed if r.action_code == action)
            if group:
                return ReferenceMatch(group, action)

    return ReferenceMatch((), "", False)


def _norm_id(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    return f"V-{digits}"
