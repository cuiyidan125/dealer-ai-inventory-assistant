"""Single-vehicle AI orchestration: "I've had this vehicle a while — what should I do?"

This is the enterprise-orchestrator layer. It does **not** replace any business algorithm and it
computes no number of its own. Given one vehicle, it invokes the three existing deterministic
skills — Single Vehicle Valuation, Inventory Portfolio Forecast, and Event Promotion Planner —
and *synthesizes* their outputs into one prioritized, evidence-backed action plan with explicit
trade-offs. Every figure is copied from a skill result; the orchestrator only selects, orders,
and explains.

The LLM's job around this is intent understanding and narration; the deciding is done by the
skills, and the committing is done by a human (each plan ends at an approval gate, never an
automatic write-back).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from pricing_agent.skills import inventory_portfolio, promotion_planner, single_vehicle

# The dealer-facing names of the three capabilities the orchestrator coordinates.
CAP_VALUATION = "Single Vehicle Valuation"
CAP_PORTFOLIO = "Portfolio Forecast"
CAP_PROMOTION = "Event Promotion Planner"

# Aging target (days) a lot manages against — used only to phrase evidence, not to compute.
AGING_TARGET_DAYS = 60
REVIEW_IN_DAYS = 7


@dataclass(frozen=True)
class ActionItem:
    priority: int
    title: str
    detail: str
    evidence: tuple[str, ...]
    pros: tuple[str, ...]
    cons: tuple[str, ...]
    source_skills: tuple[str, ...]


@dataclass(frozen=True)
class VehicleAdvice:
    vehicle_id: str
    description: str
    intent: str                              # the workflow the intent mapped to
    capabilities_invoked: tuple[str, ...]    # the skills actually called, in order
    actions: tuple[ActionItem, ...]          # prioritized, synthesized plan
    review_in_days: int
    valuation: dict = field(default_factory=dict)   # raw skill results, for "Open Full Analysis"
    portfolio: dict = field(default_factory=dict)
    promotion: dict | None = None


def advise_vehicle(
    vehicle_id: str,
    transport,
    *,
    as_of: date,
    event_id: str | None = "EVT-SUMMER-2026",
    event_name: str | None = "Summer Clearance",
    target_utilization: float = 0.75,
) -> VehicleAdvice:
    """Orchestrate the three skills for one vehicle and synthesize an action plan."""
    valuation = single_vehicle.analyze(vehicle_id, transport)
    portfolio = inventory_portfolio.analyze(transport)
    promotion = None
    caps = [CAP_VALUATION, CAP_PORTFOLIO]
    if event_id:
        try:
            promotion = promotion_planner.plan_event(transport, event_id, target_utilization)
            caps.append(CAP_PROMOTION)
        except Exception:  # noqa: BLE001 — a promotion failure must not sink the advisory
            promotion = None

    v = valuation["vehicle"]
    desc = f"{v.get('year')} {v.get('make')} {v.get('model')} {v.get('trim') or ''}".strip()
    actions: list[ActionItem] = []

    price_action = _price_action(valuation)
    if price_action is not None:
        actions.append(price_action)
    if promotion is not None:
        promo_action = _promotion_action(vehicle_id, promotion, event_name)
        if promo_action is not None:
            actions.append(promo_action)
    actions.append(_merchandising_action(v))

    # Re-number by final order so priorities are 1..N regardless of which fired.
    actions = tuple(_reprioritize(actions))
    return VehicleAdvice(
        vehicle_id=vehicle_id, description=desc,
        intent="Improve Aging Inventory", capabilities_invoked=tuple(caps),
        actions=actions, review_in_days=REVIEW_IN_DAYS,
        valuation=valuation, portfolio=portfolio, promotion=promotion,
    )


def _reprioritize(actions):
    for i, a in enumerate(actions, start=1):
        yield ActionItem(i, a.title, a.detail, a.evidence, a.pros, a.cons, a.source_skills)


def _p50(block) -> float | None:
    return block.get("p50") if isinstance(block, dict) else None


def _price_action(valuation: dict) -> ActionItem | None:
    """P-x: a reprice, only when a faster strategy trades a modest gross for real days saved.

    All numbers copied from the valuation skill: the balanced scenario's proposed price, the
    days-to-sale gap versus holding gross, the break-even floor, and the market position."""
    scenarios = {s["strategy"]: s for s in valuation.get("pricing_scenarios", [])}
    hold = scenarios.get("MAXIMIZE_GROSS")
    faster = scenarios.get("BALANCED") or scenarios.get("INCREASE_VELOCITY")
    if not hold or not faster:
        return None
    current = float(valuation["vehicle"]["current_list_price"])
    reduction = current - float(faster["proposed_list_price"])
    if reduction <= 0:
        return None  # already priced to move — no reprice recommended

    days_saved = (_p50(hold["additional_days_to_sale"]) or 0) - (_p50(faster["additional_days_to_sale"]) or 0)
    gross_gap = (_p50(hold["expected_front_end_gross"]) or 0) - (_p50(faster["expected_front_end_gross"]) or 0)
    ptm = valuation.get("market_position", {}).get("price_to_market_ratio")
    days_in = int(valuation["vehicle"].get("days_in_inventory") or 0)
    min_safe = valuation.get("break_even_analysis", {}).get("minimum_safe_list_price")
    safe_room = (current - float(min_safe)) if min_safe else None

    evidence = []
    if ptm and ptm > 1.0:
        evidence.append(f"Priced ~{ptm - 1:.0%} above comparable listings (market position).")
    if days_in:
        evidence.append(f"{days_in} days on the lot — past the {AGING_TARGET_DAYS}-day aging target.")
    if safe_room and safe_room > 0:
        evidence.append(f"~${safe_room:,.0f} of safe discount room remains above break-even.")
    if days_saved > 0:
        evidence.append(f"Expected time-to-sale improves ~{days_saved:.0f} days at the balanced price.")
    return ActionItem(
        priority=1,
        title=f"Reduce asking price by ~${reduction:,.0f}",
        detail=f"Move from ${current:,.0f} toward the balanced price of ${float(faster['proposed_list_price']):,.0f}.",
        evidence=tuple(evidence),
        pros=("Faster inventory turn", "Lower holding and depreciation cost"),
        cons=(f"Front-end gross about ${abs(gross_gap):,.0f} lower per unit",),
        source_skills=(CAP_VALUATION, CAP_PORTFOLIO),
    )


def _promotion_action(vehicle_id: str, promotion: dict, event_name: str | None) -> ActionItem | None:
    """P-y: include in the campaign — only when the vehicle is event-eligible.

    Lift and margin figures are copied from the recommended promotion plan."""
    ranked = {c["vehicle_id"]: c for c in promotion.get("candidate_ranking", [])}
    entry = ranked.get(vehicle_id)
    if not entry or not entry.get("eligible"):
        return None
    rec_type = promotion.get("recommended_plan", {}).get("plan_type")
    plan = next((p for p in promotion.get("plans", []) if p["plan_type"] == rec_type), None)
    o = (plan or {}).get("outcomes", {})
    lift = _p50(o.get("incremental_units_sold")) if o.get("incremental_units_sold") else None
    lift_mean = (o.get("incremental_units_sold") or {}).get("mean")
    margin = _p50(o.get("gross_impact")) if o.get("gross_impact") else None
    event = event_name or "the promotional"

    evidence = [f"Event-eligible: aged and above market, with safe promotional headroom."]
    if lift_mean is not None:
        evidence.append(f"Recommended campaign expected lift: +{lift_mean:.1f} sales (P50 {lift:.0f}).")
    if margin is not None:
        evidence.append(f"Campaign gross impact at plan level: ${margin:,.0f}.")
    cons = ("Reduces average gross via the promotional discount",)
    if margin is not None and margin >= 0:
        cons = ("Commits promotional budget / discount spend",)
    return ActionItem(
        priority=2,
        title=f"Include in the {event} campaign",
        detail="Add to the recommended promotion plan so the event's demand lift applies to this unit.",
        evidence=tuple(evidence),
        pros=("Higher expected sell-through inside the event window",),
        cons=cons,
        source_skills=(CAP_PROMOTION,),
    )


def _merchandising_action(vehicle: dict) -> ActionItem:
    """P-z: a merchandising refresh — a best-practice nudge grounded in the vehicle's own attributes.

    Deliberately not a skill number: the prototype has no copy-generation service, so this is
    framed honestly as a recommended refresh, not a computed forecast."""
    days_in = int(vehicle.get("days_in_inventory") or 0)
    segment = str(vehicle.get("segment") or "").upper()
    evidence = []
    if days_in:
        evidence.append(f"Listing has been live {days_in} days — a refresh restarts shopper interest.")
    if segment == "TRUCK":
        evidence.append("Truck — highlight the towing / payload package and financing options.")
    else:
        evidence.append("Highlight the strongest features and financing options in the description.")
    return ActionItem(
        priority=3,
        title="Refresh the merchandising",
        detail="Update the description and photos, and lead with the features shoppers filter on.",
        evidence=tuple(evidence),
        pros=("More shopper engagement without giving up gross",),
        cons=("Requires merchandising staff time",),
        source_skills=(CAP_PROMOTION,),
    )
