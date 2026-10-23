"""Improve Aging Inventory — orchestration engine. Phase 5.

**Not a fourth skill.** This coordinates the three existing skills in dependency order,
ranks and filters their results, and consolidates them into one dealer action plan. It adds
no valuation, forecasting, or promotion arithmetic of its own — every figure it presents was
produced by a skill and is carried through with the `request_id` and `simulation_id` that
produced it.

Sequence (recorded in an ordered execution trace):

    1. PORTFOLIO_FORECAST        run once
    2. CANDIDATE_SELECTION        rank/filter the portfolio result
    3. SINGLE_VEHICLE_VALUATION   only for selected candidates
    4. PROMOTION_PLAN             only when a real calendar event is resolved
    5. CONSOLIDATE                group actions, place figures side by side

The one rule that shapes everything: each skill runs its own simulation with its own
`simulation_id`, and percentiles from different simulations describe different probability
spaces. This engine therefore never sums or averages a percentile across skills. Portfolio-
level joint figures (ending inventory, target probability, joint gross/holding/depreciation
impact) come from a **single** source — the promotion planner's simulation — and are marked
unavailable when no event was run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from pricing_agent.mcp_clients import CapacityClient, MockTransport, MarketDataClient
from pricing_agent.skills import inventory_portfolio, promotion_planner, single_vehicle
from pricing_agent.workflows.candidate_selection import Selection, select_candidates

WORKFLOW_TYPE = "IMPROVE_AGING_INVENTORY"

# Bound the number of deep single-vehicle analyses so a large lot cannot blow up runtime.
# Candidates are risk-ranked, so the cap keeps the most valuable ones.
MAX_DEEP_ANALYSIS = 8


class WorkflowState(str, Enum):
    ROUTED_AND_EXECUTED = "ROUTED_AND_EXECUTED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    PARTIAL_RESULT = "PARTIAL_RESULT"
    TARGET_NOT_ACHIEVABLE = "TARGET_NOT_ACHIEVABLE"
    NO_SAFE_ACTIONS = "NO_SAFE_ACTIONS"
    EXECUTION_ERROR = "EXECUTION_ERROR"


# Consolidated per-vehicle action buckets.
ACTION_REPRICE_NOW = "REPRICE_NOW"
ACTION_EVENT_PROMOTION = "EVENT_PROMOTION"
ACTION_PROTECT_PRICE = "PROTECT_PRICE"
ACTION_MANAGER_REVIEW = "MANAGER_REVIEW"
ACTION_WHOLESALE = "WHOLESALE_OR_LOSS_MINIMIZATION_REVIEW"
ACTION_NO_ACTION = "NO_ACTION"


@dataclass(frozen=True)
class ImproveAgingRequest:
    """Structured inputs, already extracted by the assistant (no text parsing here)."""

    dealer_id: str = "DEALER-1001"
    target_utilization: float | None = None
    aging_threshold_days: int = 90
    event_requested: bool = False       # did the user reference an event at all?
    event_id: str | None = None         # resolved calendar event, if any
    event_name: str | None = None
    available_events: tuple[str, ...] = ()
    excluded_vehicle_ids: tuple[str, ...] = ()
    excluded_makes: tuple[str, ...] = ()
    optimization_priority: str = "BALANCED"
    minimum_gross_objective: float | None = None
    maximum_promotion_budget: float | None = None


@dataclass
class TraceEntry:
    step_number: int
    step_name: str
    skill_called: str | None
    request_id: str | None
    simulation_id: str | None
    start_timestamp: str
    end_timestamp: str
    status: str                         # OK | SKIPPED | ERROR
    warnings: tuple[str, ...] = ()
    error: str | None = None


@dataclass
class VehicleEvidence:
    vehicle_id: str
    description: str
    current_price: float | None
    recommended_action: str
    reason_codes: tuple[str, ...]
    request_id: str | None
    simulation_id: str | None
    warnings: list[dict]
    approvals_required: list[dict]
    result: dict                        # the full single-vehicle skill result, untouched


@dataclass
class ImproveAgingResult:
    state: WorkflowState
    workflow_id: str
    message: str
    request: ImproveAgingRequest
    execution_order: tuple[str, ...]
    trace: list[TraceEntry] = field(default_factory=list)
    portfolio_result: dict | None = None
    selection: Selection | None = None
    vehicle_evidence: list[VehicleEvidence] = field(default_factory=list)
    promotion_result: dict | None = None
    portfolio_summary: dict = field(default_factory=dict)
    consolidated_actions: list[dict] = field(default_factory=list)
    approvals_required: list[dict] = field(default_factory=list)
    unavailable: tuple[str, ...] = ()   # what a partial/no-event run could not produce
    # Vehicles the promotion skill's raw plan selected but the workflow holds back because
    # its candidate-selection protected them (recently acquired, high demand). The skill has
    # its own eligibility rules; the workflow's protection is authoritative for its plan.
    held_from_promotion: tuple[str, ...] = ()
    effective_promotion_ids: tuple[str, ...] = ()

    @property
    def skill_invocation_counts(self) -> dict[str, int]:
        counts = {"inventory-portfolio-forecast": 0, "single-vehicle-valuation": 0,
                  "dealer-event-promotion-planner": 0}
        for entry in self.trace:
            if entry.skill_called in counts and entry.status == "OK":
                counts[entry.skill_called] += 1
        return counts


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sim_id(result: dict | None) -> str | None:
    if not result:
        return None
    return (result.get("audit", {}).get("simulation") or {}).get("simulation_id")


def _request_id(result: dict | None) -> str | None:
    if not result:
        return None
    return result.get("audit", {}).get("request_id")


def _blocking(warnings: list[dict]) -> bool:
    return any(w.get("severity") == "BLOCKING" or w.get("blocks_publication") for w in warnings)


def run_improve_aging(
    transport: MockTransport, request: ImproveAgingRequest
) -> ImproveAgingResult:
    """Execute the orchestration for `request` against `transport`'s injected clock."""
    workflow_id = f"iaw_{int(datetime.now(timezone.utc).timestamp() * 1000) % 1_000_000_000:09d}"
    as_of = transport.as_of.date()
    order: list[str] = []
    trace: list[TraceEntry] = []

    result = ImproveAgingResult(
        state=WorkflowState.ROUTED_AND_EXECUTED,
        workflow_id=workflow_id,
        message="",
        request=request,
        execution_order=(),
    )
    result.trace = trace

    # --- Step 1: portfolio forecast (exactly once) ------------------------------------
    order.append("PORTFOLIO_FORECAST")
    start = _now()
    try:
        portfolio = inventory_portfolio.analyze(transport)
    except Exception as error:  # noqa: BLE001
        trace.append(TraceEntry(1, "PORTFOLIO_FORECAST", "inventory-portfolio-forecast",
                                None, None, start, _now(), "ERROR", error=str(error)))
        result.state = WorkflowState.EXECUTION_ERROR
        result.message = "The portfolio forecast did not complete, so no diagnosis is available."
        result.execution_order = tuple(order)
        return result

    result.portfolio_result = portfolio
    trace.append(TraceEntry(
        1, "PORTFOLIO_FORECAST", "inventory-portfolio-forecast",
        _request_id(portfolio), _sim_id(portfolio), start, _now(), "OK",
        warnings=tuple(w["code"] for w in portfolio.get("warnings", [])),
    ))

    # --- Step 2: candidate selection (no simulation) ----------------------------------
    order.append("CANDIDATE_SELECTION")
    start = _now()
    inventory = MarketDataClient(transport).get_dealer_inventory(request.dealer_id).data or []
    inbound = CapacityClient(transport).get_inbound_inventory(request.dealer_id).data or []
    selection = select_candidates(portfolio, inventory, inbound, as_of=as_of)
    # Honour caller exclusions.
    if request.excluded_vehicle_ids or request.excluded_makes:
        selection = _apply_caller_exclusions(selection, inventory, request)
    result.selection = selection
    trace.append(TraceEntry(2, "CANDIDATE_SELECTION", None, None, None, start, _now(), "OK"))

    result.portfolio_summary = _diagnose(portfolio, request)

    # --- event validation (Step 1 of the spec: do not substitute an unrelated event) --
    if request.event_requested and request.event_id is None:
        result.state = WorkflowState.NEEDS_CLARIFICATION
        events = ", ".join(request.available_events) or "none on the calendar"
        result.message = (
            "That event is not on the calendar, and I will not substitute a different one. "
            f"Available events: {events}. Name one to build the promotion step."
        )
        result.consolidated_actions = _portfolio_only_actions(selection)
        result.unavailable = ("promotion_plan", "target_achievement")
        result.execution_order = tuple(order)
        return result

    if not selection.candidates:
        result.state = WorkflowState.NO_SAFE_ACTIONS
        result.message = "No aging vehicles qualified for a repricing or promotion action."
        result.consolidated_actions = _portfolio_only_actions(selection)
        result.execution_order = tuple(order)
        return result

    # --- Step 3: single-vehicle valuation for selected candidates ---------------------
    order.append("SINGLE_VEHICLE_VALUATION")
    deep = selection.candidates[:MAX_DEEP_ANALYSIS]
    partial = False
    for candidate in deep:
        start = _now()
        try:
            sv = single_vehicle.analyze(candidate.vehicle_id, transport)
        except Exception as error:  # noqa: BLE001
            trace.append(TraceEntry(
                3, "SINGLE_VEHICLE_VALUATION", "single-vehicle-valuation",
                None, None, start, _now(), "ERROR", error=str(error),
            ))
            partial = True
            continue
        trace.append(TraceEntry(
            3, "SINGLE_VEHICLE_VALUATION", "single-vehicle-valuation",
            _request_id(sv), _sim_id(sv), start, _now(), "OK",
            warnings=tuple(w["code"] for w in sv.get("warnings", [])),
        ))
        result.vehicle_evidence.append(_evidence(candidate, sv))

    # --- Step 4: promotion plan (only when a real event is resolved) ------------------
    promotion = None
    if request.event_id is not None:
        order.append("PROMOTION_PLAN")
        start = _now()
        target = request.target_utilization if request.target_utilization is not None else 0.70
        try:
            promotion = promotion_planner.plan_event(transport, request.event_id, target)
            trace.append(TraceEntry(
                4, "PROMOTION_PLAN", "dealer-event-promotion-planner",
                _request_id(promotion), _sim_id(promotion), start, _now(), "OK",
                warnings=tuple(w["code"] for w in promotion.get("warnings", [])),
            ))
            result.promotion_result = promotion
        except Exception as error:  # noqa: BLE001
            trace.append(TraceEntry(
                4, "PROMOTION_PLAN", "dealer-event-promotion-planner",
                None, None, start, _now(), "ERROR", error=str(error),
            ))
            partial = True

    # --- Step 5: consolidate ----------------------------------------------------------
    order.append("CONSOLIDATE")
    start = _now()
    # The workflow's protection overrides the promotion skill's raw eligibility: a protected
    # vehicle the skill selected is held back and never promoted in the consolidated plan.
    protected_ids = {
        e.vehicle_id for e in selection.exclusions
        if any(r in Selection.PROTECTED_REASONS for r in e.reason_codes)
    }
    raw_promoted = _promoted_ids(promotion)
    result.held_from_promotion = tuple(sorted(raw_promoted & protected_ids))
    result.effective_promotion_ids = tuple(sorted(raw_promoted - protected_ids))
    result.consolidated_actions = _consolidate(
        result.vehicle_evidence, selection, result.effective_promotion_ids
    )
    result.approvals_required = _collect_approvals(result.vehicle_evidence, promotion)
    result.portfolio_summary.update(_summary_outcomes(promotion))
    trace.append(TraceEntry(5, "CONSOLIDATE", None, None, None, start, _now(), "OK"))

    result.execution_order = tuple(order)

    # --- terminal state ---------------------------------------------------------------
    unavailable: list[str] = []
    if promotion is None:
        unavailable += ["promotion_plan", "target_achievement", "joint_gross_impact"]
    if partial:
        result.state = WorkflowState.PARTIAL_RESULT
        result.message = ("Some analyses did not complete; the results below are the ones that "
                          "finished. Missing recommendations are marked unavailable.")
        unavailable.append("some_vehicle_analyses")
    elif promotion is not None and promotion["feasibility"]["status"] == "NOT_ACHIEVABLE":
        result.state = WorkflowState.TARGET_NOT_ACHIEVABLE
        result.message = (
            f"The {request.target_utilization:.0%} target is not reachable inside the "
            f"{request.event_name} window with safe actions. The diagnosis and the most "
            "aggressive safe plan are shown, along with what would close the gap."
            if request.target_utilization else
            "The utilization target is not reachable with safe actions in this window."
        )
    else:
        result.state = WorkflowState.ROUTED_AND_EXECUTED
        result.message = _executed_message(result, request)

    result.unavailable = tuple(dict.fromkeys(unavailable))
    return result


# --- helpers --------------------------------------------------------------------------


def _apply_caller_exclusions(
    selection: Selection, inventory: list[dict], request: ImproveAgingRequest
) -> Selection:
    from pricing_agent.workflows.candidate_selection import Candidate, Exclusion

    make_of = {v["vehicle_id"]: str(v.get("make", "")).casefold() for v in inventory}
    excluded_makes = {m.casefold() for m in request.excluded_makes}
    kept: list[Candidate] = []
    moved: list[Exclusion] = list(selection.exclusions)
    for c in selection.candidates:
        if c.vehicle_id in request.excluded_vehicle_ids:
            moved.append(Exclusion(c.vehicle_id, c.description, ("MANUAL_HOLD",)))
        elif make_of.get(c.vehicle_id) in excluded_makes:
            moved.append(Exclusion(c.vehicle_id, c.description, ("MANUAL_HOLD",)))
        else:
            kept.append(c)
    return Selection(tuple(kept), tuple(moved))


def _diagnose(portfolio: dict, request: ImproveAgingRequest) -> dict:
    """Portfolio-level diagnosis, entirely from the portfolio result (one simulation)."""
    capacity = portfolio["capacity_position"]
    aging = portfolio["aging_profile"]
    financial = portfolio["financial_risk"]
    return {
        "simulation_id": _sim_id(portfolio),
        "current_inventory": capacity["current_inventory"],
        "physical_open_slots": capacity["physical_open_slots"],
        "current_utilization": capacity["current_utilization"],
        "target_utilization": request.target_utilization,
        "aged_concentration_pct": aging["aged_concentration_pct"],
        "aging_buckets": aging["buckets"],
        "units_below_break_even": financial["units_below_break_even"],
        "holding_cost_exposure": financial.get("projected_cash_holding_exposure"),
        "depreciation_exposure": financial.get("projected_depreciation_exposure"),
        "one_month_units_p50": portfolio["one_month_forecast"]["unit_sales"]["p50"],
        "three_month_units_p50": portfolio["three_month_forecast"]["unit_sales"]["p50"],
    }


def _summary_outcomes(promotion: dict | None) -> dict:
    """Joint outcome figures — only from the promotion simulation, never combined."""
    if promotion is None:
        return {
            "required_unit_reduction": None,
            "expected_ending_inventory": None,
            "expected_ending_utilization": None,
            "probability_target_achieved": None,
            "expected_gross_impact": None,
            "expected_holding_cost_savings": None,
            "expected_depreciation_savings": None,
            "outcome_simulation_id": None,
        }
    target_block = promotion["inventory_target_calculation"]
    feasibility = promotion["feasibility"]
    financial = promotion["financial_impact"]
    # The joint outcome comes from the recommended plan's own simulation. The promotion
    # audit records the baseline simulation id; the outcome figures use the plan's, so the
    # plan's is the correct provenance to show against ending inventory and gross impact.
    recommended = promotion["recommended_plan"]["plan_type"]
    plan_sim = next(
        (p["simulation"]["simulation_id"] for p in promotion["plans"]
         if p["plan_type"] == recommended),
        _sim_id(promotion),
    )
    return {
        "required_unit_reduction": target_block["incremental_promotional_sales_required"],
        "expected_ending_inventory": promotion["projected_ending_inventory"],
        "probability_target_achieved": feasibility["probability_target_achieved"],
        "expected_gross_impact": financial["gross_impact"],
        "expected_holding_cost_savings": financial["cash_holding_cost_savings"],
        "expected_depreciation_savings": financial["depreciation_savings"],
        "outcome_simulation_id": plan_sim,
    }


def _evidence(candidate, sv: dict) -> VehicleEvidence:
    return VehicleEvidence(
        vehicle_id=candidate.vehicle_id,
        description=candidate.description,
        current_price=sv["vehicle"].get("current_list_price"),
        recommended_action="",  # filled during consolidation
        reason_codes=candidate.reason_codes,
        request_id=_request_id(sv),
        simulation_id=_sim_id(sv),
        warnings=sv.get("warnings", []),
        approvals_required=sv.get("approvals_required", []),
        result=sv,
    )


def _promoted_ids(promotion: dict | None) -> set[str]:
    if promotion is None:
        return set()
    plan = promotion["recommended_plan"]["plan_type"]
    for p in promotion["plans"]:
        if p["plan_type"] == plan:
            return {v["vehicle_id"] for v in p.get("vehicles_selected", [])}
    return set()


def _consolidate(
    evidence: list[VehicleEvidence], selection: Selection, promoted: tuple[str, ...]
) -> list[dict]:
    """Group each analysed vehicle into a single dealer action. No number is produced here;
    the decision reads the single-vehicle strategy, warnings, and approvals.

    `promoted` is the workflow's *effective* promotion set — the skill's plan minus any
    vehicle the workflow protected — so a protected vehicle is never marked for promotion."""
    promoted = set(promoted)
    from pricing_agent.workflows.candidate_selection import AGGRESSIVE_DISPOSITION

    actions: list[dict] = []
    candidate_by_id = {c.vehicle_id: c for c in selection.candidates}

    for ev in evidence:
        strategy = ev.result.get("recommended_strategy", {}).get("strategy")
        portfolio_action = candidate_by_id.get(ev.vehicle_id).portfolio_action \
            if ev.vehicle_id in candidate_by_id else "RETAIN_PRICE"

        if _blocking(ev.warnings) or ev.approvals_required:
            action = (ACTION_WHOLESALE if portfolio_action in AGGRESSIVE_DISPOSITION
                      else ACTION_MANAGER_REVIEW)
        elif portfolio_action in AGGRESSIVE_DISPOSITION:
            action = ACTION_WHOLESALE
        elif ev.vehicle_id in promoted:
            action = ACTION_EVENT_PROMOTION
        elif strategy in ("INCREASE_VELOCITY", "BALANCED"):
            action = ACTION_REPRICE_NOW
        else:
            action = ACTION_NO_ACTION

        object.__setattr__(ev, "recommended_action", action)
        actions.append({
            "vehicle_id": ev.vehicle_id,
            "description": ev.description,
            "current_price": ev.current_price,
            "recommended_action": action,
            "referenced_request_id": ev.request_id,
            "referenced_simulation_id": ev.simulation_id,
            "reason_codes": list(ev.reason_codes),
            "warnings": [w["code"] for w in ev.warnings],
            "approvals_required": [a.get("type") or a.get("approval_type") for a in ev.approvals_required],
        })

    # Protected / excluded vehicles surface as PROTECT_PRICE or NO_ACTION, never acted on.
    for exc in selection.exclusions:
        protect = any(r in Selection.PROTECTED_REASONS for r in exc.reason_codes)
        actions.append({
            "vehicle_id": exc.vehicle_id,
            "description": exc.description,
            "current_price": None,
            "recommended_action": ACTION_PROTECT_PRICE if protect else ACTION_NO_ACTION,
            "referenced_request_id": None,
            "referenced_simulation_id": None,
            "reason_codes": list(exc.reason_codes),
            "warnings": [],
            "approvals_required": [],
        })
    return actions


def _portfolio_only_actions(selection: Selection) -> list[dict]:
    """Vehicle-level recommendations available without single-vehicle analysis — used when
    the workflow stops early (unresolved event). Actions come from the candidate reasons."""
    out: list[dict] = []
    for c in selection.candidates:
        out.append({
            "vehicle_id": c.vehicle_id,
            "description": c.description,
            "current_price": c.current_list_price,
            "recommended_action": ACTION_REPRICE_NOW,
            "referenced_request_id": None,
            "referenced_simulation_id": None,
            "reason_codes": list(c.reason_codes),
            "warnings": [],
            "approvals_required": [],
        })
    for exc in selection.exclusions:
        protect = any(r in Selection.PROTECTED_REASONS for r in exc.reason_codes)
        out.append({
            "vehicle_id": exc.vehicle_id,
            "description": exc.description,
            "current_price": None,
            "recommended_action": ACTION_PROTECT_PRICE if protect else ACTION_NO_ACTION,
            "referenced_request_id": None,
            "referenced_simulation_id": None,
            "reason_codes": list(exc.reason_codes),
            "warnings": [],
            "approvals_required": [],
        })
    return out


def _collect_approvals(evidence: list[VehicleEvidence], promotion: dict | None) -> list[dict]:
    """Approvals preserved from each source, tagged with where they came from."""
    approvals: list[dict] = []
    for ev in evidence:
        for a in ev.approvals_required:
            approvals.append({"vehicle_id": ev.vehicle_id, "source": "single-vehicle-valuation", **a})
    if promotion:
        for a in promotion.get("approvals_required", []):
            approvals.append({"vehicle_id": None, "source": "dealer-event-promotion-planner", **a})
    return approvals


def _executed_message(result: ImproveAgingResult, request: ImproveAgingRequest) -> str:
    n = len(result.selection.candidates)
    deep = len(result.vehicle_evidence)
    scope = f"{n} aging candidate(s)" + (f" ({deep} analysed in depth)" if deep < n else "")
    if result.promotion_result is not None:
        status = result.promotion_result["feasibility"]["status"].replace("_", " ").lower()
        return (f"Diagnosed the lot, selected {scope}, and built the "
                f"{request.event_name} plan — target is {status}.")
    return (f"Diagnosed the lot and selected {scope}. Name a calendar event "
            "to add a promotion plan and quantify target achievement.")
