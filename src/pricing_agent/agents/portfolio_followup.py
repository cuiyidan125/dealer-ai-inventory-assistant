"""Deterministic follow-up handling for the inventory-portfolio (Acquire) conversation.

After the 30-day outlook, the dealer drills down without leaving the thread: current lot health,
the units needing attention, and what to review before acquiring. Each ask is classified with rules
(never the LLM) and answered from the stored `inventory_portfolio` result — this module selects,
sorts, parses a display count, and maps codes to labels/icons, but computes nothing and imports none
of the calculation layers (`domain`, `simulation`, `numpy`, solvers). Unmatched asks return None so
the caller falls back to the honest clarification (or a genuine cross-workflow switch).
"""

from __future__ import annotations

import re
from datetime import datetime

from pricing_agent.agents import portfolio_copy as C
from pricing_agent.agents.conversation import (
    SOURCE_PORTFOLIO_ACQUIRE_REVIEW,
    SOURCE_PORTFOLIO_LOT_TODAY,
    SOURCE_PORTFOLIO_TOP_RISK,
    ConversationState,
)
from pricing_agent.agents.followup import FollowupResult

# Freeing lot space is an Improve-Aging job — never a portfolio follow-up, even when phrased as a
# precursor to acquiring. Checked first so the switch (routed by the router to IMPROVE_AGING) wins.
_FREE_SPACE = re.compile(
    r"\bfree up (space|room)\b|\bmake room\b|\bclear (out )?(the )?(aged|old)\b|"
    r"\b(create|make) a plan to free\b|\bplan to free up\b", re.I)

_TOP_RISK = re.compile(
    r"\btop\s+\d+\b|\btop\s+(one|two|three|four|five|six|seven|eight|nine|ten)\b|"
    r"\bneed(s|ing)?\s+attention\b|\b(most|at)\s+risk\b|\briskiest\b|\bworst\b|"
    r"\bwhich\s+(units|vehicles|cars)\b.*\b(attention|risk|problem|drag)\b|"
    r"\bdragging\s+(me\s+)?(down)?\b|\bproblem\s+(units|cars|vehicles)\b", re.I)

# Requires BOTH an acquire/buy context AND a review cue (lookaheads), so "free up space before I
# acquire" — which has no review cue — does not match and is left to switch.
_ACQUIRE_REVIEW = re.compile(
    r"(?=.*\b(acquir\w+|buy(ing)?\s+(more|another|again)|add(ing)?\s+(more\s+)?(inventory|units|cars)|"
    r"purchas\w+|stock\s+up)\b)"
    r"(?=.*\b(review|check|look at|what\s+should\s+i|what\s+(to|do\s+i)\s+(review|check|fix|watch)|"
    r"ready\s+to\s+(buy|acquire)|due\s+diligence|fix\s+first|watch\s+out|first)\b)", re.I | re.S)

_LOT_TODAY = re.compile(
    r"\blot\b.*\b(today|now|right now)\b|\b(current|today'?s)\s+(lot|inventory|state|health)\b|"
    r"\bwhere\s+(do\s+i|am\s+i)\s+stand\b|\bstate\s+of\s+(my\s+|the\s+)?lot\b|"
    r"\bhow\s+(full|healthy)\s+is\s+(my\s+|the\s+)?lot\b|\blot\s+health\b|\blot\s+look(s|ing)?\s+like\b",
    re.I)

_COUNT_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10}


def _parse_count(text: str, default: int = 5) -> int:
    m = re.search(r"\btop\s+(\d+)\b|\b(\d+)\s+(?:vehicles|units|cars)\b", text, re.I)
    if m:
        return int(m.group(1) or m.group(2))
    m = re.search(r"\btop\s+(" + "|".join(_COUNT_WORDS) + r")\b|\b(" + "|".join(_COUNT_WORDS)
                  + r")\s+(?:vehicles|units|cars)\b", text, re.I)
    if m:
        return _COUNT_WORDS[(m.group(1) or m.group(2)).lower()]
    return default


def classify(text: str, state: ConversationState) -> str | None:
    """Which portfolio beat `text` is, or None. Also used by the caller to let a clear portfolio
    follow-up yield a coarse workflow-switch classification about the same lot."""
    result = state.active_result
    if not isinstance(result, dict) or "capacity_position" not in result:
        return None
    if _FREE_SPACE.search(text):      # Improve-Aging intent — let it switch
        return None
    if _TOP_RISK.search(text):
        return "top_risk"
    if _ACQUIRE_REVIEW.search(text):
        return "acquire_review"
    if _LOT_TODAY.search(text):
        return "lot_today"
    return None


def handle_portfolio_followup(text: str, state: ConversationState, *, as_of: datetime) -> FollowupResult | None:
    kind = classify(text, state)
    if kind == "top_risk":
        return _top_risk(text, state)
    if kind == "acquire_review":
        return _acquire_review(text, state)
    if kind == "lot_today":
        return _lot_today(text, state)
    return None


def _lot_today(text: str, state: ConversationState) -> FollowupResult:
    result = state.active_result
    kpis = C.lot_today_kpis(result)
    payload = {
        "kind": "lot_today",
        "summary": C.lot_today_summary(kpis),
        "kpis": kpis,
        "vehicles": C.select_top_risk(result, None),   # full ranked table
    }
    state.last_portfolio_followup_kind = "lot_today"
    return FollowupResult(SOURCE_PORTFOLIO_LOT_TODAY, C.lot_today_summary(kpis), payload=payload)


def _top_risk(text: str, state: ConversationState) -> FollowupResult:
    result = state.active_result
    n = _parse_count(text, default=5)
    rows = C.select_top_risk(result, n)
    payload = {
        "kind": "top_risk",
        "n": n,
        "summary": C.top_risk_summary(rows, n),
        "vehicles": rows,
    }
    state.last_portfolio_followup_kind = "top_risk"
    return FollowupResult(SOURCE_PORTFOLIO_TOP_RISK, C.top_risk_summary(rows, n), payload=payload)


def _acquire_review(text: str, state: ConversationState) -> FollowupResult:
    result = state.active_result
    warnings = C.acquire_warnings(result)
    payload = {
        "kind": "acquire_review",
        "summary": C.acquire_review_summary(warnings),
        "warnings": warnings,
        "cta": C.ACQUIRE_CTA,
    }
    state.last_portfolio_followup_kind = "acquire_review"
    return FollowupResult(SOURCE_PORTFOLIO_ACQUIRE_REVIEW, C.acquire_review_summary(warnings),
                          payload=payload)
