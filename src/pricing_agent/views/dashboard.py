"""Inventory dashboard — the demo's opening screen.

Written for a used-vehicle manager, so it leads with dollars and days rather than
architecture. Renders only; every number comes from the portfolio skill, which runs a
single simulation across the whole lot (D2) rather than twelve independent ones.

Extracted verbatim from `app.py` in Phase 2. The body is unchanged; only its enclosing
function and the `workflow_context` parameter are new.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from pricing_agent.config import load_config
from pricing_agent.mcp_clients import MockTransport, VautoClient
from pricing_agent.skills.inventory_portfolio import analyze
from pricing_agent.views import terminology as T
from pricing_agent.views.glossary import render_glossary
from pricing_agent.views.workflow_copy import render_workflow_header
from pricing_agent.workflows.context import WorkflowContext

import ui_components

AS_OF = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)


def md(text: str) -> str:
    """Streamlit reads `$...$` as LaTeX, so any string with two dollar amounts loses
    both signs. Every money-bearing markdown string goes through this."""
    return text.replace("$", r"\$")


def _pct(fraction: float | None) -> float | None:
    """Fraction to whole percent for NumberColumn, whose "%%" format does not scale."""
    return None if fraction is None else fraction * 100.0


# Cached at module level on purpose: a nested @st.cache_data would be redefined on every
# render call, and the cache would never hit.
@st.cache_data(show_spinner=False)
def portfolio(as_of: datetime, revenue_target: float | None) -> dict:
    return analyze(MockTransport(as_of=as_of), revenue_target_one_month=revenue_target)


@st.cache_data(show_spinner=False)
def inventory(as_of: datetime) -> list[dict]:
    return VautoClient(MockTransport(as_of=as_of)).get_dealer_inventory().data


ACTION_LABEL = {
    "LOSS_MINIMIZATION_REVIEW": "🔴 Loss-minimization review",
    "WHOLESALE_DISPOSITION": "🔴 Wholesale",
    "MANAGER_REVIEW": "🟠 Manager review",
    "VELOCITY_REPRICE": "🟠 Reprice for velocity",
    "BALANCED_REPRICE": "🟡 Reprice to market",
    "EVENT_PROMOTION": "🟡 Event promotion",
    "INCREASE_PRICE": "🟢 Raise price",
    "RETAIN_PRICE": "🟢 Hold price",
}

SEVERITY_KIND = {
    "BLOCKING": "error", "CRITICAL": "error", "HIGH": "warning",
    "MEDIUM": "warning", "LOW": "info", "INFO": "info",
}


def render_dashboard(workflow_context: WorkflowContext | None = None) -> None:
    """Render the inventory dashboard.

    `workflow_context` selects the page copy and nothing else — every figure below is
    computed the same way whichever workflow rendered it.
    """
    # --- header -----------------------------------------------------------------------

    config = load_config()
    copy = render_workflow_header(
        workflow_context, fallback_title="Used Vehicle Pricing Advisor"
    )

    target = st.sidebar.number_input(
        "30-day revenue target ($)", min_value=0, value=150_000, step=10_000,
        help="Drives the probability of missing target on the forecast tab.",
    )
    st.sidebar.caption(
        f"as of {AS_OF:%d %b %Y %H:%M} UTC\n\n"
        f"assumptions `{config.assumption_version}`\n\n"
        f"model `{config.model_version}`"
    )

    with st.spinner("Running the lot…"):
        result = portfolio(AS_OF, float(target))
        vehicles = inventory(AS_OF)

    capacity = result["capacity_position"]
    valuation = result["portfolio_valuation"]
    one_month = result["one_month_forecast"]
    three_month = result["three_month_forecast"]
    aging = result["aging_profile"]
    risk = {r["vehicle_id"]: r for r in result["top_risk_vehicles"]}
    actions = {a["vehicle_id"]: a for a in result["recommended_actions"]}

    st.caption(
        f"DEALER-1001 · {result['inventory_summary']['active_count']} active units · "
        f"median {result['inventory_summary']['median_days_in_inventory']:.0f} days in inventory"
    )

    if copy is not None:
        if copy.instruction is not None:
            st.caption(copy.instruction)
        if copy.scope_note is not None:
            st.info(copy.scope_note, icon="🎯")

    # --- KPI row ----------------------------------------------------------------------

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric(T.metric("units_on_lot"), capacity["current_inventory"],
              f"{capacity['physical_open_slots']} spaces open", delta_color="off")
    c2.metric(T.metric("lot_capacity_used"), f"{capacity['current_utilization']:.0%}",
              f"{capacity['current_utilization'] - capacity['target_utilization']:+.0%} "
              f"vs {capacity['target_utilization']:.0%} target",
              delta_color="inverse",
              help=(f"Target lot utilization is {capacity['target_utilization']:.0%} — a healthy "
                    "fullness that still leaves room to acquire. Above it means the lot is "
                    "carrying more inventory than planned."))
    c3.metric(T.metric("over_90"), f"{aging['aged_concentration_pct'] * capacity['current_inventory']:.0f}",
              f"{aging['aged_concentration_pct']:.0%} of lot", delta_color="off")
    c4.metric(
        T.metric("cash_tied_up"),
        f"${valuation['cash_tied_up']:,.0f}",
        f"${valuation['total_cost_basis']:,.0f} total cost basis",
        delta_color="off",
        help="Cost basis less floorplan financing outstanding.",
    )
    c5.metric(T.metric("below_break_even"), result["financial_risk"]["units_below_break_even"],
              f"${result['financial_risk']['total_exposure_below_break_even']:,.0f} exposure",
              delta_color="off")

    shown = [w for w in result["warnings"] if w["severity"] in ("BLOCKING", "CRITICAL", "HIGH")]
    for warning in shown:
        kind = SEVERITY_KIND[warning["severity"]]
        getattr(st, kind)(md(f"**{T.warning_label(warning['code'])}** — {warning['message']}  \n_{warning['remediation']}_"))
    if shown:
        with st.expander("View technical reason codes"):
            st.caption("Warning codes: " + ", ".join(f"`{w['code']}`" for w in shown))

    st.caption("Your lot today, and what it looks like over the next 30 and 90 days.")
    lot_tab, forecast_tab, risk_tab = st.tabs(
        ["Lot today", "Inventory outlook", "Vehicles needing attention"])

    # --- lot --------------------------------------------------------------------------

    with lot_tab:
        rows = []
        for vehicle in vehicles:
            vid = vehicle["vehicle_id"]
            action = actions.get(vid, {})
            rows.append(
                {
                    # Thumbnail rather than the full asset: a dozen full-size images inlined
                    # into a dataframe would push megabytes through every rerun.
                    "Photo": ui_components.thumbnail_uri(vehicle.get("image_url")),
                    "Suggested action": ACTION_LABEL.get(action.get("action", ""), action.get("action", "")),
                    "Stock": vid,
                    "Vehicle": f"{vehicle['year']} {vehicle['make']} {vehicle['model']}",
                    "Days on lot": vehicle["days_in_inventory"],
                    "Current asking price": vehicle.get("current_list_price"),
                    "Risk": risk.get(vid, {}).get("risk_score"),
                    # Scaled to whole percents: Streamlit's "%%" format takes the number
                    # literally, so a 1.0 fraction would render as "1%".
                    "Risk of over 90 days": _pct(risk.get(vid, {}).get("prob_age_over_90")),
                    "Chance of negative value": _pct(risk.get(vid, {}).get("prob_negative_net_value")),
                    "Why": action.get("matched_rule", ""),
                }
            )
        frame = pd.DataFrame(rows).sort_values("Risk", ascending=False)

        st.caption(
            "Ranked by risk of remaining unsold too long, which weights time on lot, expected "
            "value loss, chance of a negative outcome, and cost basis — so a $45,000 vehicle at "
            "moderate risk outranks a $9,000 vehicle at high risk. Open **Price Inventory** for "
            "any of these to see its recommended asking price and lowest safe price."
        )
        st.dataframe(
            frame, hide_index=True,
            column_config={
                "Photo": st.column_config.ImageColumn("Photo", width="small"),
                "Current asking price": st.column_config.NumberColumn(format="$%d"),
                "Risk": st.column_config.ProgressColumn(format="%.0f", min_value=0, max_value=100),
                "Risk of over 90 days": st.column_config.NumberColumn(format="%.0f%%"),
                "Chance of negative value": st.column_config.NumberColumn(format="%.0f%%"),
            },
        )

    # --- forecast ---------------------------------------------------------------------

    with forecast_tab:
        st.caption(
            "What the lot is expected to sell and how full it stays. This is a forecast for "
            "existing inventory only — it assumes no replacement purchases, so ending inventory "
            "and revenue are cautious lower estimates."
        )

        for label, block in (("Next 30 days", one_month), ("Next 90 days", three_month)):
            st.subheader(label)
            units, revenue = block["unit_sales"], block["sales_revenue"]
            a, b, c, d = st.columns(4)
            a.metric("Expected vehicles sold (P50)", f"{units['p50']:.0f}",
                     f"Range {units['p10']:.0f}–{units['p90']:.0f} (P10–P90)", delta_color="off")
            b.metric("Expected revenue (P50)", f"${revenue['p50']:,.0f}",
                     f"Downside (P10) ${revenue['p10']:,.0f}", delta_color="off")
            c.metric("Expected front-end gross (P50)", f"${block['front_end_gross']['p50']:,.0f}")
            d.metric("Expected lot capacity used (P50)", f"{block['ending_utilization']['p50']:.0%}")

            risk_probability = block["risk_probabilities"]["revenue_below_target"]
            if risk_probability is not None:
                st.progress(
                    min(1.0, risk_probability),
                    text=f"{risk_probability:.0%} chance revenue falls below the ${target:,.0f} target",
                )

            figure = go.Figure()
            figure.add_trace(
                go.Bar(
                    x=["Downside (P10)", "Expected (P50)", "Conservative (P90)"],
                    y=[revenue["p10"], revenue["p50"], revenue["p90"]],
                    text=[f"${v:,.0f}" for v in (revenue["p10"], revenue["p50"], revenue["p90"])],
                    textposition="outside",
                )
            )
            figure.update_layout(
                height=260, yaxis_title="Revenue, $", margin=dict(t=20, b=10), showlegend=False
            )
            st.plotly_chart(figure)

        st.subheader("Inventory age breakdown")
        st.caption("How the lot is distributed across time-on-lot ranges today, and how many "
                   "vehicles in each range are expected to still be here next month.")
        aging_frame = pd.DataFrame(
            [
                {
                    "Time on lot (days)": bucket["label"],
                    "Vehicles now": bucket["unit_count"],
                    "Expected still here in 30 days": bucket["projected_unit_count_at_horizon"],
                    "Cost basis": bucket["cost_basis"],
                }
                for bucket in aging["buckets"]
            ]
        )
        st.dataframe(
            aging_frame, hide_index=True,
            column_config={"Cost basis": st.column_config.NumberColumn(format="$%d")},
        )

    # --- risk -------------------------------------------------------------------------

    with risk_tab:
        st.subheader("Vehicles needing attention")
        st.caption("The vehicles with the most economic risk of remaining unsold too long, and "
                   "the suggested inventory action for each.")
        for entry in result["top_risk_vehicles"][:6]:
            vehicle = next(v for v in vehicles if v["vehicle_id"] == entry["vehicle_id"])
            with st.container(border=True):
                photo, left, right = st.columns([1, 2, 3], vertical_alignment="center")
                thumbnail = ui_components.thumbnail_uri(vehicle.get("image_url"), width=260)
                if thumbnail:
                    photo.image(thumbnail, width="stretch")
                else:
                    photo.caption("No photo")
                left.markdown(
                    f"**{vehicle['year']} {vehicle['make']} {vehicle['model']}**  \n"
                    f"{entry['vehicle_id']} · {vehicle['days_in_inventory']} days"
                )
                left.metric("Attention score", f"{entry['risk_score']:.0f}")
                right.markdown(md(
                    "\n".join(f"- {factor}" for factor in entry["risk_factors"])
                    + f"\n- Cost basis ${entry['cost_basis']:,.0f}"
                    + f"\n\n**Suggested inventory action:** "
                    + ACTION_LABEL.get(
                        actions[entry["vehicle_id"]]["action"],
                        actions[entry["vehicle_id"]]["action"],
                    )
                ))

        st.subheader("All alerts")
        for warning in result["warnings"]:
            st.markdown(md(f"**{T.warning_label(warning['code'])}** — {warning['message']}"))
        with st.expander("View technical reason codes"):
            st.caption("Warning codes: " + ", ".join(f"`{w['code']}`" for w in result["warnings"]))

        with st.expander("Data coverage and audit"):
            st.json(result["data_coverage"])
            st.write(
                f"**Simulation** — {result['audit']['simulation']['draw_count']:,} draws, "
                f"seed `{result['audit']['simulation']['seed']}`, "
                f"label `{result['audit']['simulation']['model_label']}`"
            )
            st.dataframe(
                pd.DataFrame(result["audit"]["mcp_tools_called"]),
                hide_index=True,
            )

    render_glossary()
    st.info(
        "Forecasts are a **configured prototype simulation**, not a trained prediction, and "
        "every recommendation should be reviewed before acting.",
        icon="ℹ️",
    )
