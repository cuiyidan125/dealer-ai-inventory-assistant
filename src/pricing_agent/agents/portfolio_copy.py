"""Vocabulary, field extractors, and advisor prose for the inventory-portfolio (Acquire) conversation.

Presentation and selection only. Every value returned here is copied from the stored
`inventory_portfolio` result (or a display-ready derivation the Acquire dashboard already performs,
such as over-90 count = aged_concentration_pct × current_inventory). Nothing here reruns a forecast,
recomputes a risk score, or imports the calculation layers. Deterministic templates keep a consistent
"inventory advisor" tone with or without an API key.
"""

from __future__ import annotations

# Recommended-action code → (severity dot, dealer label). Mirrors the Acquire dashboard's ACTION_LABEL.
ACTION = {
    "LOSS_MINIMIZATION_REVIEW": ("🔴", "Loss-minimization review"),
    "WHOLESALE_DISPOSITION": ("🔴", "Wholesale"),
    "MANAGER_REVIEW": ("🟠", "Manager review"),
    "VELOCITY_REPRICE": ("🟠", "Reprice for velocity"),
    "BALANCED_REPRICE": ("🟡", "Reprice to market"),
    "EVENT_PROMOTION": ("🟡", "Event promotion"),
    "INCREASE_PRICE": ("🟢", "Raise price"),
    "RETAIN_PRICE": ("🟢", "Hold price"),
}

# The default 30-day revenue target the outlook reports against (mirrors assistant.DEFAULT_REVENUE_TARGET
# and the Acquire dashboard sidebar default).
REVENUE_TARGET = 150_000


def action_label(code: str | None) -> tuple[str, str]:
    return ACTION.get(code or "", ("⚪", (code or "Review").replace("_", " ").title()))


def reason_icon(factor: str) -> str:
    """A per-reason icon for a stored `risk_factors` string (matched by keyword, never computed)."""
    f = (factor or "").lower()
    if "break-even" in f or "break even" in f:
        return "⛔"
    if "90 days" in f or "aging" in f or "age" in f:
        return "⏳"
    if "negative" in f:
        return "📉"
    if "depreciat" in f:
        return "💸"
    return "•"


def _p(block, key="p50"):
    return block.get(key) if isinstance(block, dict) else None


# --- field extractors (copied / display-ready derivations only) ------------------------


def outlook_kpis(result: dict) -> dict:
    om = result.get("one_month_forecast", {}) or {}
    return {
        "sold_p50": _p(om.get("unit_sales")),
        "sold_p10": _p(om.get("unit_sales"), "p10"),
        "sold_p90": _p(om.get("unit_sales"), "p90"),
        "revenue_p50": _p(om.get("sales_revenue")),
        "revenue_p10": _p(om.get("sales_revenue"), "p10"),
        "revenue_p90": _p(om.get("sales_revenue"), "p90"),
        "gross_p50": _p(om.get("front_end_gross")),
        "capacity_used_p50": _p(om.get("ending_utilization")),
        "below_target_prob": (om.get("risk_probabilities") or {}).get("revenue_below_target"),
        "revenue_target": REVENUE_TARGET,
    }


def lot_today_kpis(result: dict) -> dict:
    cap = result.get("capacity_position", {}) or {}
    aging = result.get("aging_profile", {}) or {}
    fin = result.get("financial_risk", {}) or {}
    val = result.get("portfolio_valuation", {}) or {}
    inv = result.get("inventory_summary", {}) or {}
    units = cap.get("current_inventory")
    aged_pct = aging.get("aged_concentration_pct")
    # Over-90 count is the same display derivation the Acquire dashboard renders.
    over90 = round(aged_pct * units) if (aged_pct is not None and units is not None) else None
    return {
        "units_on_lot": units,
        "open_slots": cap.get("physical_open_slots"),
        "utilization": cap.get("current_utilization"),
        "target_utilization": cap.get("target_utilization"),
        "over_90_units": over90,
        "aged_pct": aged_pct,
        "cash_tied_up": val.get("cash_tied_up"),
        "below_break_even": fin.get("units_below_break_even"),
        "below_break_even_exposure": fin.get("total_exposure_below_break_even"),
        "median_days": inv.get("median_days_in_inventory"),
    }


def select_top_risk(result: dict, n: int | None) -> list[dict]:
    """The n highest-risk vehicles (the stored `top_risk_vehicles` order is already the ranking),
    each joined to its recommended-action code. n=None returns the full ranked list."""
    actions = {a.get("vehicle_id"): a.get("action") for a in result.get("recommended_actions", []) or []}
    ranked = result.get("top_risk_vehicles", []) or []
    if n is not None:
        ranked = ranked[:n]
    out = []
    for v in ranked:
        out.append({
            "vehicle_id": v.get("vehicle_id"),
            "risk_score": v.get("risk_score"),
            "risk_factors": tuple(v.get("risk_factors", ()) or ()),
            "prob_age_over_90": v.get("prob_age_over_90"),
            "prob_negative_net_value": v.get("prob_negative_net_value"),
            "cost_basis": v.get("cost_basis"),
            "action_code": actions.get(v.get("vehicle_id")),
        })
    return out


def acquire_warnings(result: dict) -> list[dict]:
    """The warnings a dealer should clear before acquiring — the same severity filter the dashboard
    uses for its review panel."""
    return [w for w in (result.get("warnings", []) or [])
            if w.get("severity") in ("BLOCKING", "CRITICAL", "HIGH")]


# --- advisor prose (deterministic templates) ------------------------------------------


def outlook_summary(k: dict) -> str:
    sold = k.get("sold_p50")
    rev = k.get("revenue_p50")
    prob = k.get("below_target_prob")
    parts = []
    if sold is not None:
        rng = (f" ({k['sold_p10']:.0f}–{k['sold_p90']:.0f})"
               if k.get("sold_p10") is not None and k.get("sold_p90") is not None else "")
        parts.append(f"Over the next 30 days your lot is expected to sell about **{sold:.0f} vehicles**{rng}")
    if rev is not None:
        parts.append(f"bringing in roughly **${rev:,.0f}**")
    line = ", ".join(parts) + "." if parts else "Here's your 30-day outlook."
    if prob:
        line += (f" There's a **{prob:.0%}** chance revenue lands below your "
                 f"**${k.get('revenue_target', REVENUE_TARGET):,.0f}** target — this forecast assumes no "
                 "replacement purchases, so it's a cautious lower estimate.")
    return line


def lot_today_summary(k: dict) -> str:
    units = k.get("units_on_lot")
    util = k.get("utilization")
    tgt = k.get("target_utilization")
    cash = k.get("cash_tied_up")
    below = k.get("below_break_even")
    parts = []
    if units is not None and util is not None:
        over = (f", **{(util - tgt) * 100:+.0f} pts** vs your {tgt:.0%} target"
                if tgt is not None else "")
        parts.append(f"Right now you're carrying **{units} units at {util:.0%} utilization**{over}")
    if cash is not None:
        parts.append(f"with **${cash:,.0f}** in cash tied up")
    if below:
        parts.append(f"and **{below} units** advertised below break-even")
    return ", ".join(parts) + "." if parts else "Here's your lot today."


def top_risk_summary(rows: list[dict], n: int) -> str:
    shown = len(rows)
    return (f"These **{shown}** unit{'s' if shown != 1 else ''} carry the most economic risk of "
            "remaining unsold too long — every one is aged, underwater, or both. Clearing them frees "
            "the most cash and lot space first.")


def acquire_review_summary(warnings: list[dict]) -> str:
    n = len(warnings)
    return (f"Before adding units, there {'are' if n != 1 else 'is'} **{n} thing"
            f"{'s' if n != 1 else ''}** to clear first — otherwise these follow the new inventory onto "
            "an already-full lot.")


ACQUIRE_CTA = ("Free up space first — open **Improve Aging** to reprice and promote the aged cohort "
               "before you buy.")
