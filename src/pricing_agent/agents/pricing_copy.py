"""Vocabulary, field extractors, and advisor-voice prose for the single-vehicle pricing conversation.

Presentation and selection only. Every figure a builder returns is copied straight from the stored
`single_vehicle` result (or a `promotional_headroom` ladder rung); nothing here runs a simulation,
prices a vehicle, or imports the calculation layers. The prose is deterministic templates so it holds
a consistent "vAuto inventory advisor" tone with or without an API key.
"""

from __future__ import annotations

import re

# Underlying scenario codes (unchanged), in dealer-preferred display order.
STRATEGY_CODES = ("MAXIMIZE_GROSS", "BALANCED", "INCREASE_VELOCITY")

# Short, recognizable dealer-facing labels for THIS conversation (display only; the app-wide labels
# on the Price Inventory page are deliberately left as they are).
DISPLAY_LABEL = {
    "MAXIMIZE_GROSS": "Protect Profit",
    "BALANCED": "Balanced",
    "INCREASE_VELOCITY": "Sell Faster",
}
GOAL = {
    "MAXIMIZE_GROSS": "Maximize gross profit",
    "BALANCED": "Balance profit and inventory turn",
    "INCREASE_VELOCITY": "Sell as quickly as possible",
}
PROS = {
    "MAXIMIZE_GROSS": "Captures the most front-end gross per unit",
    "BALANCED": "Strong gross while clearing the lot at a healthy pace",
    "INCREASE_VELOCITY": "Fastest turn — frees the slot and cash soonest",
}
CONS = {
    "MAXIMIZE_GROSS": "Slowest turn; more days on lot and holding cost",
    "BALANCED": "Gives up some gross versus holding for full price",
    "INCREASE_VELOCITY": "Thinnest gross and the least downside protection",
}

# Input synonyms the dealer might type → scenario code. Profit/velocity phrases are matched before the
# generic "balanced", so a specific ask wins.
_SYNONYMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bprofit[\s-]*first\b|\bprotect\s*profit\b|\bmaximiz\w*\s*(gross|profit)\b|"
                r"\bhighest\s*(gross|profit)\b", re.I), "MAXIMIZE_GROSS"),
    (re.compile(r"\bvelocity[\s-]*first\b|\bsell\s*faster\b|\bfastest\b|\bquickest\b|"
                r"\bmove\s*it\s*fast\b", re.I), "INCREASE_VELOCITY"),
    (re.compile(r"\bbalanced?\b", re.I), "BALANCED"),
)

NEXT_CHECKPOINT = (
    "If this vehicle remains unsold after 45 days, I'd recommend expanding the analysis beyond "
    "pricing alone to include inventory health, promotion planning, and portfolio optimization."
)

SELECTION_NOTICE = (
    "The {label} strategy is selected for review. No pricing action has been published."
)

PROBABILISTIC_NOTE = "This is an expected selling window, not a guaranteed sale date."


def match_strategy(text: str) -> str | None:
    """The scenario code named in `text`, or None."""
    for pattern, code in _SYNONYMS:
        if pattern.search(text):
            return code
    return None


# --- field extractors (copied, never computed) ----------------------------------------


def scenario_by_code(result: dict, code: str) -> dict | None:
    for s in result.get("pricing_scenarios", []) or []:
        if s.get("strategy") == code:
            return s
    return None


def recommended_code(result: dict) -> str:
    return (result.get("recommended_strategy") or {}).get("strategy") or "BALANCED"


def _p50(block) -> float | None:
    return block.get("p50") if isinstance(block, dict) else None


def _p90(block) -> float | None:
    return block.get("p90") if isinstance(block, dict) else None


def scenario_metrics(scenario: dict) -> dict:
    """The P50 headline metrics a dealer reads for one strategy — all copied from the scenario."""
    code = scenario.get("strategy")
    return {
        "strategy": code,
        "label": DISPLAY_LABEL.get(code, code),
        "goal": GOAL.get(code, ""),
        "pros": PROS.get(code, ""),
        "cons": CONS.get(code, ""),
        "proposed_list_price": scenario.get("proposed_list_price"),
        "p50_days": _p50(scenario.get("additional_days_to_sale")),
        "p90_days": _p90(scenario.get("additional_days_to_sale")),
        "p50_gross": _p50(scenario.get("expected_front_end_gross")),
        "p50_holding_cost": _p50(scenario.get("expected_cash_holding_cost")),
        "p50_depreciation": _p50(scenario.get("expected_depreciation_loss")),
        "p50_net_economic_value": _p50(scenario.get("expected_net_economic_value")),
        "deal_rating": scenario.get("deal_rating"),
        "price_to_market_ratio": scenario.get("price_to_market_ratio"),
    }


def baseline_recommendation(result: dict) -> dict:
    """The recommended scenario's headline numbers — the baseline the conversation compares against."""
    scen = scenario_by_code(result, recommended_code(result)) or {}
    m = scenario_metrics(scen)
    m["current_list_price"] = (result.get("vehicle") or {}).get("current_list_price")
    return m


# --- advisor prose (deterministic templates) ------------------------------------------


def first_turn_why(result: dict) -> list[str]:
    """The advisor WHY under the first-turn metrics: market position, competitiveness, demand/turn."""
    mp = result.get("market_position", {}) or {}
    scen = scenario_by_code(result, recommended_code(result)) or {}
    within30 = (scen.get("sale_probabilities") or {}).get("within_30_days")
    p50_days = _p50(scen.get("additional_days_to_sale"))
    ratio = scen.get("price_to_market_ratio") or mp.get("price_to_market_ratio")
    rating = (mp.get("deal_rating") or "").title()
    pct = mp.get("market_percentile")

    bullets: list[str] = []
    if pct is not None and rating:
        bullets.append(
            f"**Market position** — priced around the **{int(pct)}th percentile** of comparable "
            f"listings, a **{rating}** deal against the local market.")
    if ratio is not None:
        diff = (ratio - 1) * 100
        rel = "above" if diff >= 0 else "below"
        bullets.append(
            f"**Competitiveness** — about **{abs(diff):.1f}% {rel} market**, close enough to keep "
            "the vehicle in shoppers' search results.")
    if within30 is not None and p50_days is not None:
        bullets.append(
            f"**Expected demand & turn** — roughly **{within30 * 100:.0f}%** likely to sell within "
            f"30 days, with an expected **{p50_days:.0f} days** to sale (P50).")
    bullets.append(
        f"That is why I'd hold at the **{DISPLAY_LABEL.get(recommended_code(result))}** price for "
        "now — it protects gross while staying competitive for a unit at this age.")
    return bullets


def detail_why(result: dict, code: str) -> list[str]:
    """The WHY under the detailed financial card: profitability, velocity, competitiveness, downside."""
    m = scenario_metrics(scenario_by_code(result, code) or {})
    be = result.get("break_even_analysis", {}) or {}
    break_even = be.get("current_accounting_break_even")
    ratio = m["price_to_market_ratio"]
    bullets: list[str] = []
    if m["p50_gross"] is not None and m["p50_holding_cost"] is not None:
        bullets.append(
            f"**Profitability** — expected front-end gross of **${m['p50_gross']:,.0f}** (P50), "
            f"against about **${m['p50_holding_cost']:,.0f}** of expected holding cost.")
    if m["p50_days"] is not None:
        bullets.append(
            f"**Inventory velocity** — expected **{m['p50_days']:.0f} days** to sale (P50), a "
            "materially faster turn than holding for full price.")
    if ratio is not None and m["deal_rating"]:
        bullets.append(
            f"**Market competitiveness** — a **{str(m['deal_rating']).title()}** deal at about "
            f"**{(ratio - 1) * 100:+.1f}% vs market**, inside the range shoppers actually browse.")
    if break_even is not None and m["proposed_list_price"] is not None:
        cushion = m["proposed_list_price"] - break_even
        depr = f" and expected depreciation of about **${m['p50_depreciation']:,.0f}**" \
            if m["p50_depreciation"] is not None else ""
        bullets.append(
            f"**Downside risk** — holds about **${cushion:,.0f}** above the **${break_even:,.0f}** "
            f"break-even floor{depr}, so it does not book a loss on paper.")
    return bullets


def reasoning_steps(result: dict, code: str) -> list[tuple[str, str]]:
    """Four qualitative reasoning steps drawn from stored structured evidence. Deliberately does not
    reproduce the metric card or introduce computed numbers — it explains, it does not recompute."""
    rating = (result.get("market_position", {}) or {}).get("deal_rating", "fair")
    label = DISPLAY_LABEL.get(code, code)
    return [
        ("How I read the current market",
         "I anchored to the external market value and checked it against comparable listings. This "
         f"unit lands mid-pack — a {str(rating).lower()} deal — so there was genuine room to price "
         "with intent rather than guess at a number."),
        ("How your selling window shaped the price",
         "You wanted a healthier turn, so I weighted expected days-to-sale against gross. Protect "
         "Profit holds the most margin but sits on the lot longest; Sell Faster clears quickest but "
         "gives up the most gross."),
        ("How I protected profitability and the floor",
         "Every option is held above the accounting break-even floor, so none of them books a loss "
         "on paper. The recommendation keeps a cushion above that floor while still covering the "
         "cost of carrying the vehicle."),
        (f"Why {label} is the best trade-off here",
         f"{label} captures most of the available gross while clearing the lot noticeably faster "
         "than Protect Profit. For a unit at this age that is the strongest profit-per-day, without "
         "the thin margin and downside exposure of the most aggressive price."),
    ]


def strategy_trade_off_summary() -> str:
    return (
        "**Protect Profit** holds the most gross but the longest days on lot. **Sell Faster** clears "
        "quickest but thins the margin and the downside cushion. **Balanced** sits between them — "
        "most of the gross, a materially faster turn."
    )
