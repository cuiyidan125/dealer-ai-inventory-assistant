"""Deterministic follow-up handling for the Single Vehicle Pricing conversation.

After a valuation the dealer explores the *same* vehicle: re-target by days, compare strategies, pick
one, ask for the reasoning. This module classifies each ask with rules (never the LLM) and returns a
grounded `FollowupResult` whose structured `payload` the view renders as cards/tables.

Every figure is copied from the stored `single_vehicle` result or a `promotional_headroom` ladder
rung — this module runs no simulation, prices nothing, and imports none of the calculation layers
(`pricing_agent.domain`, `pricing_agent.simulation`, `numpy`). Selecting a strategy marks it "for
review"; nothing is ever published. Unmatched asks return None so the caller falls back to the
existing honest clarification.
"""

from __future__ import annotations

import re
from datetime import datetime

from pricing_agent.agents import pricing_copy as C
from pricing_agent.agents.conversation import (
    SOURCE_PRICING_DETAIL,
    SOURCE_PRICING_REASONING,
    SOURCE_PRICING_STRATEGIES,
    SOURCE_PRICING_TARGET,
    ConversationState,
)
from pricing_agent.agents.followup import FollowupResult

# --- classification -------------------------------------------------------------------

# "walk me through your reasoning", "why is Balanced better", "explain your thinking". Checked FIRST
# so a reasoning ask that names strategies is not mistaken for a selection.
_REASONING = re.compile(
    r"\bwalk me through\b|\breasoning\b|\brationale\b|\bthink(ing)?\b|"
    r"\bexplain (your|the|this|why)\b|\bhow (did|do) you\b|\bwhy (is|are|would|should|balanced|"
    r"protect|sell|velocity|profit)\b", re.I)

# "show me a few strategies / options", "compare the trade-offs". Requires a plural/compare cue, so a
# singular "the Balanced strategy" (a selection) does not match.
_STRATEGIES = re.compile(
    r"\bstrategies\b|\boptions?\b|\bcompare\b|\btrade[\s-]?offs?\b|"
    r"\b(three|3|several|some|a few)\s+(pricing\s+)?(strateg\w*|prices?|options?)\b|"
    r"\bshow me (three|3|several|some|a few)\b", re.I)

# A day target ("within ~20 days", "in 20 days") or a bare speed ask ("faster", "sooner"). A day
# number only counts as a *pricing* target when paired with a selling/price cue — otherwise
# "inventory look like in 30 days" would be mistaken for a re-price instead of a portfolio switch.
_SPEED = re.compile(r"\bfaster\b|\bsooner\b|\bquicker\b|\bspeed it up\b|\bmove it\b", re.I)
_DAYS = re.compile(
    r"(?:within|in|under|about|target|to)\s+(?:about\s+|approximately\s+|~\s*)?(\d{1,3})\b"
    r"|\b(\d{1,3})\s*[- ]?days?\b", re.I)
_SELL_CUE = re.compile(
    r"\bsell(ing)?\b|\bprice\b|\bpriced\b|\blist\b|\bmove (it|this)\b|\bturn (it|this)\b|"
    r"\boff the lot\b|\bgone\b", re.I)

_SELECT_INTENT = re.compile(
    r"\bi (like|prefer|want|'?ll take|choose|pick|select|lean)\b|\bgo with\b|\blet'?s (do|go|use)\b|"
    r"\bprefer the\b|\buse the\b|\bstick with\b", re.I)


def _parse_target_days(text: str) -> int | None:
    m = _DAYS.search(text)
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def _is_target_days(text: str) -> bool:
    if _SPEED.search(text):
        return True
    return _parse_target_days(text) is not None and bool(_SELL_CUE.search(text))


def _is_selection(text: str) -> bool:
    if C.match_strategy(text) is None:
        return False
    # An explicit intent verb, or a short message that is essentially just the strategy name.
    return bool(_SELECT_INTENT.search(text)) or len(text.split()) <= 4


def classify(text: str, state: ConversationState) -> str | None:
    """Which pricing beat `text` is, or None. Also used by the caller to decide that a coarse
    workflow-switch classification should yield to a clear pricing follow-up about the same car."""
    result = state.active_result
    if not isinstance(result, dict) or not result.get("pricing_scenarios"):
        return None
    if _REASONING.search(text):
        return "reasoning"
    if _STRATEGIES.search(text):
        return "strategies"
    if _is_target_days(text):
        return "target"
    if _is_selection(text):
        return "select"
    return None


def handle_pricing_followup(text: str, state: ConversationState, *, as_of: datetime) -> FollowupResult | None:
    """Classify and answer a pricing follow-up against the active valuation, or None to fall back."""
    kind = classify(text, state)
    if kind == "reasoning":
        return _reasoning(text, state)
    if kind == "strategies":
        return _strategies(text, state)
    if kind == "target":
        return _target_days(text, state)
    if kind == "select":
        return _select_strategy(text, state, C.match_strategy(text))
    return None


# --- beat 2: re-target by days --------------------------------------------------------


def _select_target_rung(result: dict, target_days: int):
    """(rung, reached). The minimum discount required to meet the target = the highest-price SAFE
    ladder rung whose P50 days-to-sale <= target. If no safe rung reaches it, the fastest safe rung
    (reached=False). Selection only — the numbers are the engine's."""
    ladder = (result.get("promotional_headroom") or {}).get("ladder") or []
    safe = [r for r in ladder if not r.get("exceeds_safe_discount")
            and r.get("p50_days_to_sale") is not None]
    qualifying = [r for r in safe if r["p50_days_to_sale"] <= target_days]
    if qualifying:
        # Least discount among qualifying rungs = highest price that still meets the target.
        return min(qualifying, key=lambda r: r["discount"]), True
    if safe:
        return min(safe, key=lambda r: r["p50_days_to_sale"]), False
    return None, False


def _target_days(text: str, state: ConversationState) -> FollowupResult:
    result = state.active_result
    target = _parse_target_days(text)
    if target is None:  # a bare "sell it faster" — use the velocity scenario's own P50 as the target
        velo = C.scenario_by_code(result, "INCREASE_VELOCITY") or {}
        target = int(C._p50(velo.get("additional_days_to_sale")) or 20)

    rung, reached = _select_target_rung(result, target)
    prev = state.active_pricing_recommendation or state.baseline_pricing_recommendation or {}
    be = (result.get("break_even_analysis") or {}).get("current_accounting_break_even")

    if rung is None:
        return FollowupResult(
            SOURCE_PRICING_TARGET,
            "I can't model a safe price for that target — the ladder has no rung inside the price "
            "floor. I've kept the current recommendation.",
            payload={"kind": "target", "target_days": target, "reached": False, "no_rung": True,
                     "previous": prev})

    target_price = rung["list_price"]
    target_p50 = rung["p50_days_to_sale"]
    target_nev = rung["p50_net_economic_value"]
    prev_price = prev.get("proposed_list_price")
    prev_days = prev.get("p50_days")
    prev_nev = prev.get("p50_net_economic_value")
    cushion = (target_price - be) if be is not None else None
    material = ((prev_nev is not None and target_nev is not None and target_nev < prev_nev * 0.5)
                or (cushion is not None and cushion < 1000))

    payload = {
        "kind": "target",
        "target_days": target,
        "reached": reached,
        "previous": {"label": prev.get("label"), "price": prev_price, "p50_days": prev_days,
                     "net_economic_value": prev_nev},
        "target": {"price": target_price, "p50_days": target_p50,
                   "net_economic_value": target_nev, "discount": rung["discount"]},
        "price_change": (target_price - prev_price) if prev_price is not None else None,
        "days_change": (target_p50 - prev_days) if prev_days is not None else None,
        "nev_change": (target_nev - prev_nev) if (prev_nev is not None and target_nev is not None) else None,
        "cushion_above_break_even": cushion,
        "break_even_price": be,
        "material_compression": bool(material),
        "note": C.PROBABILISTIC_NOTE,
    }

    # Explore-only: update the ACTIVE recommendation, never the baseline.
    state.active_pricing_recommendation = {
        "label": f"~{target}-day target", "strategy": None,
        "proposed_list_price": target_price, "p50_days": target_p50,
        "p50_net_economic_value": target_nev,
    }
    state.last_pricing_followup_kind = "target"

    if reached:
        text_out = (f"To target ~{target} days I'd move to **${target_price:,.0f}** "
                    f"(expected {target_p50:.0f} days, P50). {C.PROBABILISTIC_NOTE}")
    else:
        text_out = (f"Even the fastest safe price (**${target_price:,.0f}**, ~{target_p50:.0f} days) "
                    f"doesn't reach {target} days — the price floor binds. {C.PROBABILISTIC_NOTE}")
    return FollowupResult(SOURCE_PRICING_TARGET, text_out, payload=payload)


# --- beat 3: compare strategies -------------------------------------------------------


def _strategies(text: str, state: ConversationState) -> FollowupResult:
    result = state.active_result
    rows = [C.scenario_metrics(C.scenario_by_code(result, code) or {}) for code in C.STRATEGY_CODES]
    state.last_pricing_followup_kind = "strategies"
    payload = {"kind": "strategies", "rows": rows, "summary": C.strategy_trade_off_summary()}
    return FollowupResult(
        SOURCE_PRICING_STRATEGIES,
        "Here are three pricing strategies — Protect Profit, Balanced, and Sell Faster — with their "
        "expected days and gross so you can compare the trade-offs.",
        payload=payload)


# --- beat 4: select a strategy --------------------------------------------------------


def _detail_metrics(result: dict, code: str) -> dict:
    m = C.scenario_metrics(C.scenario_by_code(result, code) or {})
    be = result.get("break_even_analysis", {}) or {}
    mp = result.get("market_position", {}) or {}
    conf = (result.get("valuation", {}) or {}).get("confidence", {}) or {}
    return {
        "current_list_price": (result.get("vehicle") or {}).get("current_list_price"),
        "recommended_price": m["proposed_list_price"],
        "break_even_price": be.get("current_accounting_break_even"),
        "p50_gross": m["p50_gross"],
        "p50_holding_cost": m["p50_holding_cost"],
        "p50_depreciation": m["p50_depreciation"],
        "p50_days": m["p50_days"],
        "p90_days": m["p90_days"],
        "deal_rating": mp.get("deal_rating") or m["deal_rating"],
        "market_percentile": mp.get("market_percentile"),
        "price_to_market_ratio": m["price_to_market_ratio"],
        "confidence_level": conf.get("level"),
        "confidence_score": conf.get("score"),
    }


def _select_strategy(text: str, state: ConversationState, code: str) -> FollowupResult:
    result = state.active_result
    label = C.DISPLAY_LABEL.get(code, code)
    metrics = _detail_metrics(result, code)

    state.selected_pricing_strategy = code
    scen = C.scenario_by_code(result, code) or {}
    m = C.scenario_metrics(scen)
    state.active_pricing_recommendation = {
        "label": label, "strategy": code, "proposed_list_price": m["proposed_list_price"],
        "p50_days": m["p50_days"], "p50_net_economic_value": m["p50_net_economic_value"],
    }
    state.last_pricing_followup_kind = "detail"

    payload = {
        "kind": "detail",
        "code": code,
        "label": label,
        "notice": C.SELECTION_NOTICE.format(label=label),
        "metrics": metrics,
        "why": C.detail_why(result, code),
    }
    return FollowupResult(
        SOURCE_PRICING_DETAIL,
        C.SELECTION_NOTICE.format(label=label),
        payload=payload)


# --- beat 5: reasoning ----------------------------------------------------------------


def _reasoning(text: str, state: ConversationState) -> FollowupResult:
    result = state.active_result
    code = state.selected_pricing_strategy or C.recommended_code(result)
    state.last_pricing_followup_kind = "reasoning"
    payload = {
        "kind": "reasoning",
        "code": code,
        "label": C.DISPLAY_LABEL.get(code, code),
        "steps": C.reasoning_steps(result, code),
        "next_checkpoint": C.NEXT_CHECKPOINT,
    }
    return FollowupResult(
        SOURCE_PRICING_REASONING,
        f"Here's the reasoning behind {C.DISPLAY_LABEL.get(code, code)} — no new numbers, just how "
        "the decision was reached.",
        payload=payload)
