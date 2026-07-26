"""Event promotion planner — demo beat 4.

Leads with feasibility, because that is the question actually being asked. Reports mean
and P90 incremental units alongside P50: unit sales are integers, so a real effect
smaller than one car is invisible at the median.

Extracted verbatim from `pages/2_Promotion.py` in Phase 2. The body is unchanged;
only its enclosing function and the `workflow_context` parameter are new.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from pricing_agent.mcp_clients import EventClient, MockTransport, VautoClient
from pricing_agent.skills.promotion_planner import plan_event
from pricing_agent.views import terminology as T
from pricing_agent.views.glossary import render_glossary
from pricing_agent.views.review_panel import render_review_panel
from pricing_agent.views.style import style_fig
from pricing_agent.views.workflow_copy import render_workflow_header
from pricing_agent.workflows.context import WorkflowContext

import ui_components

AS_OF = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)



def md(text: str) -> str:
    return text.replace("$", r"\$")


# Cached at module level on purpose: a nested @st.cache_data would be redefined on
# every render call, and the cache would never hit.
@st.cache_data(show_spinner=False)
def events(as_of: datetime) -> list[dict]:
    return EventClient(MockTransport(as_of=as_of)).get_sales_event_calendar().data


@st.cache_data(show_spinner=False)
def plan(as_of: datetime, event_id: str, target: float) -> dict:
    return plan_event(MockTransport(as_of=as_of), event_id, target)


@st.cache_data(show_spinner=False)
def vehicles_by_id(as_of: datetime) -> dict[str, dict]:
    inventory = VautoClient(MockTransport(as_of=as_of)).get_dealer_inventory().data
    return {v["vehicle_id"]: v for v in inventory}


def describe(vehicle: dict | None) -> str:
    if not vehicle:
        return ""
    return f"{vehicle['year']} {vehicle['make']} {vehicle['model']}"


FEASIBILITY_STYLE = {
    "ACHIEVABLE": ("success", "✅"),
    "ACHIEVABLE_WITH_MARGIN_COST": ("warning", "⚠️"),
    "AT_RISK": ("warning", "⚠️"),
    "NOT_ACHIEVABLE": ("error", "🚫"),
}



def render_promotion_planner(workflow_context: WorkflowContext | None = None) -> None:
    """Render the event promotion planner.

    `workflow_context` selects the page copy and nothing else — the plan, the feasibility
    verdict and every figure below are computed identically without it.
    """
    catalog = events(AS_OF)
    labels = {f"{e['event_name']} ({e['start_date']} → {e['end_date']})": e["event_id"] for e in catalog}

    copy = render_workflow_header(workflow_context, fallback_title="Event promotion planner")

    choice = st.sidebar.selectbox("Event", list(labels))
    event_id = labels[choice]
    # Whole percent: Streamlit's "%%" format does not scale, so a 0.70 float slider would
    # render as "1%".
    target_pct = st.sidebar.slider("Target utilization", 40, 100, 75, 5, format="%d%%")
    target = target_pct / 100.0

    with st.spinner("Planning…"):
        result = plan(AS_OF, event_id, float(target))

    objective = result["promotion_objective"]
    target_block = result["inventory_target_calculation"]
    feasibility = result["feasibility"]
    plans = {p["plan_type"]: p for p in result["plans"]}
    recommended = result["recommended_plan"]

    st.caption(
        f"{objective['event']['event_name']} · {objective['event']['start_date']} to "
        f"{objective['event']['end_date']} · dates resolved from "
        f"`{objective['event']['date_source']}`"
    )

    if copy is not None and copy.instruction is not None:
        st.caption(copy.instruction)

    # --- feasibility first ----------------------------------------------------------------

    kind, icon = FEASIBILITY_STYLE.get(feasibility["status"], ("info", "•"))
    getattr(st, kind)(
        md(
            f"**Target likelihood: {T.feasibility_label(feasibility['status'])}** — "
            f"the target needs **{feasibility['required_incremental_units']} additional sale(s)** "
            f"inside a {feasibility['event_duration_days']}-day window. The most aggressive safe "
            f"plan delivers **{plans['CAPACITY_FIRST']['outcomes']['incremental_units_sold']['mean']:.1f} "
            f"on average**, and reaches the target in "
            f"**{feasibility['probability_target_achieved']:.0%}** of simulated outcomes. "
            "A sale-event plan improves the odds; it does not guarantee sales."
        ),
        icon=icon,
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Target ending inventory", target_block["target_ending_inventory"],
              f"{target:.0%} of {target_block['total_physical_slots']} spaces", delta_color="off")
    c2.metric("Expected without the event (P50)",
              f"{target_block['projected_inventory_without_promotion']['p50']:.0f}",
              "vehicles remaining", delta_color="off")
    c3.metric("Additional sales required", target_block["incremental_promotional_sales_required"])
    c4.metric("Eligible for the sale event", feasibility["max_safe_candidate_pool"],
              f"{len(result['excluded_vehicles'])} protected/excluded", delta_color="off")

    with st.expander("How the target was calculated"):
        st.markdown(md(
            f"""
    ```
    target ending inventory   = {target_block['total_physical_slots']} slots x {target:.0%}
                              = {target_block['target_ending_inventory']}

    projected without promo   = {target_block['current_inventory']} on lot
                              + {target_block['confirmed_inbound']} confirmed inbound
                              - {target_block['baseline_expected_sales']['p50']:.0f} baseline sales (P50)
                              - {target_block['other_expected_exits']} other exits
                              = {target_block['projected_inventory_without_promotion']['p50']:.0f}

    incremental required      = {target_block['incremental_promotional_sales_required']}
    ```
    Only `confirmed_inbound` enters the flow. `reserved_slots` is a superset of it
    ({target_block['reserved_not_inbound']} reserved beyond inbound), so counting both would
    inflate the requirement by the entire inbound volume.

    `other_expected_exits` has no data source and is assumed zero, which makes the
    requirement a conservative overestimate.
    """
        ))

    st.divider()

    # --- plans ----------------------------------------------------------------------------

    st.subheader("Compare sale-event approaches")
    st.caption(
        "Each approach makes a different trade-off between protecting profit and freeing space. "
        "The recommended one is marked. A plan improves the odds of reaching the target — it "
        "does not guarantee sales."
    )

    columns = st.columns(3)
    for column, plan_type in zip(columns, ("MARGIN_PROTECT", "BALANCED", "CAPACITY_FIRST")):
        item = plans[plan_type]
        outcomes = item["outcomes"]
        is_recommended = plan_type == recommended["plan_type"]

        with column.container(border=True):
            st.markdown(
                f"**{T.plan_name(plan_type)}**"
                + ("  ⭐ recommended" if is_recommended else "")
            )
            st.caption(T.plan_trade_off(plan_type))
            st.metric("Vehicles in the sale event", item["totals"]["vehicle_count"])
            st.metric("Dealer-funded discount", f"${item['totals']['total_dealer_funded']:,.0f}")
            st.metric(
                "Expected additional sales",
                f"{outcomes['incremental_units_sold']['mean']:.2f} avg",
                f"Conservative (P90) {outcomes['incremental_units_sold']['p90']:.0f}",
                delta_color="off",
            )
            st.metric(
                "Expected total front-end gross (P50)",
                f"${outcomes['total_front_end_gross']['p50']:,.0f}",
                delta_color="off",
            )
            st.metric("Likelihood of reaching the target", f"{outcomes['probability_target_achieved']:.0%}")
            st.caption(
                f"Expected lot capacity used (P50) {outcomes['ending_utilization']['p50']:.0%} · "
                f"{'within budget' if item['totals']['within_budget'] else 'over budget'}"
            )

    if plans["MARGIN_PROTECT"]["totals"]["vehicle_count"] == 0:
        st.info(
            "**Prioritize profit protection** discounts nothing here. On this lot, discounting "
            "would cost more gross than it saves in carrying cost — so the profit-protecting "
            "answer is *do not discount*. The other two approaches show what the space target costs.",
            icon="💡",
        )

    # Three metrics on three independent scales — a shared y-axis would crush the sub-one-car
    # sales effect and the small gross figures against the ~100% likelihood. Small multiples let
    # each metric compare the three plans on its own terms, with the value printed on every bar.
    order = ("MARGIN_PROTECT", "BALANCED", "CAPACITY_FIRST")
    x_labels = ["Profit<br>protection", "Balanced ★", "Free<br>space"]
    bar_colors = ["#7A8CA6", "#EE5A2A", "#E8A13C"]  # recommended (Balanced) in vAuto orange
    add_sales = [plans[p]["outcomes"]["incremental_units_sold"]["mean"] for p in order]
    gross = [plans[p]["outcomes"]["total_front_end_gross"]["p50"] for p in order]
    likely = [plans[p]["outcomes"]["probability_target_achieved"] * 100.0 for p in order]
    panels = [
        ("Additional sales (avg units)", add_sales, [f"{v:.2f}" for v in add_sales]),
        ("Total front-end gross (P50)", gross, [f"${v:,.0f}" for v in gross]),
        ("Target likelihood", likely, [f"{v:.0f}%" for v in likely]),
    ]
    figure = make_subplots(rows=1, cols=3, subplot_titles=[p[0] for p in panels],
                           horizontal_spacing=0.09)
    for ci, (_title, values, texts) in enumerate(panels, start=1):
        figure.add_trace(
            go.Bar(x=x_labels, y=values, marker_color=bar_colors, text=texts,
                   textposition="outside", cliponaxis=False, showlegend=False,
                   hovertemplate="%{text}<extra></extra>"),
            row=1, col=ci,
        )
        figure.update_yaxes(showticklabels=False, showgrid=True, gridcolor="rgba(0,0,0,0.06)",
                            zeroline=True, zerolinecolor="rgba(0,0,0,0.28)", row=1, col=ci)
        figure.update_xaxes(tickfont=dict(size=11), row=1, col=ci)
    figure.update_layout(height=300, margin=dict(t=52, b=28, l=6, r=6),
                         uniformtext=dict(minsize=10, mode="show"))
    for annotation in figure.layout.annotations:  # subplot titles
        annotation.font.size = 13
    st.plotly_chart(style_fig(figure), use_container_width=True)
    st.caption("Each metric has its own scale, so the bars compare the three approaches within "
               "that metric. Balanced ★ is the recommended plan.")

    # --- alternatives ---------------------------------------------------------------------

    if feasibility["alternatives"]:
        st.subheader("What would improve the odds")
        st.caption("Concrete options, because a likelihood on its own leaves you where you started.")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Option": a["option"].replace("_", " ").title(),
                        "Change required": a["quantified_change"],
                        "Unit": a["unit"].title(),
                        "New target likelihood": a["resulting_probability_target_achieved"] * 100.0,
                    }
                    for a in feasibility["alternatives"]
                ]
            ),
            hide_index=True,
            column_config={
                "New target likelihood": st.column_config.NumberColumn(format="%.0f%%"),
            },
        )

    # --- promote / protect / exclude ------------------------------------------------------

    st.subheader("Per-vehicle actions")
    promote_tab, protect_tab, exclude_tab = st.tabs(
        ["Include in sale event", "Protect price", "Protected or excluded"])
    actions = result["per_vehicle_actions"]
    chosen_plan = plans[recommended["plan_type"]]
    prices = {v["vehicle_id"]: v for v in chosen_plan["vehicles_selected"]}

    fleet = vehicles_by_id(AS_OF)
    PHOTO_COLUMN = st.column_config.ImageColumn("Photo", width="small")


    def _photo(vehicle_id: str) -> str | None:
        return ui_components.thumbnail_uri((fleet.get(vehicle_id) or {}).get("image_url"))


    with promote_tab:
        rows = [
            {
                "Photo": _photo(a["vehicle_id"]),
                "Stock": a["vehicle_id"],
                "Vehicle": describe(fleet.get(a["vehicle_id"])),
                "Current asking price": prices[a["vehicle_id"]]["current_list_price"],
                "Sale-event price": a["promotion_price"],
                "Discount": prices[a["vehicle_id"]]["discount"],
                "Lowest safe asking price": prices[a["vehicle_id"]]["minimum_safe_list_price"],
            }
            for a in actions
            if a["action"] == "PROMOTE"
        ]
        if rows:
            st.dataframe(
                pd.DataFrame(rows), hide_index=True,
                column_config={
                    "Photo": PHOTO_COLUMN,
                    **{
                        c: st.column_config.NumberColumn(format="$%d")
                        for c in ("Current asking price", "Sale-event price", "Discount",
                                  "Lowest safe asking price")
                    },
                },
            )
            st.caption("No sale-event price falls below its lowest safe asking price.")
        else:
            st.write("This plan promotes no vehicles.")

    with protect_tab:
        rows = [
            {
                "Photo": _photo(a["vehicle_id"]),
                "Stock": a["vehicle_id"],
                "Vehicle": describe(fleet.get(a["vehicle_id"])),
                "Why": a["reason"],
            }
            for a in actions
            if a["action"] == "PROTECT_PRICE"
        ]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True,
                         column_config={"Photo": PHOTO_COLUMN})
        else:
            st.write("No vehicles are being held back from this plan.")

    with exclude_tab:
        rows = [
            {
                "Photo": _photo(a["vehicle_id"]),
                "Stock": a["vehicle_id"],
                "Vehicle": describe(fleet.get(a["vehicle_id"])),
                "Reason": a["reason"],
            }
            for a in actions
            if a["action"] == "EXCLUDE"
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True,
                     column_config={"Photo": PHOTO_COLUMN})
        st.caption(
            "Every exclusion carries a reason — a plan is not reviewable if you cannot see "
            "what was left out. \"No safe room for a discount\" means the vehicle is already at "
            "or below its safe price, which is common for aged units: the vehicles you most "
            "want to move are often the ones you cannot safely discount."
        )

    # --- warnings -------------------------------------------------------------------------

    if result["warnings"]:
        render_review_panel(result["warnings"], heading="What to review")

    with st.expander("Audit"):
        st.write(
            f"Simulation seed `{result['audit']['simulation']['seed']}`, "
            f"{result['audit']['simulation']['draw_count']:,} draws, "
            f"label `{result['audit']['simulation']['model_label']}`"
        )
        st.caption(
            f"Event lift: {feasibility['lift_source']}"
            + (
                f" ({feasibility['historical_event_lift']}x from prior events)"
                if feasibility["historical_event_lift"]
                else " — no validated history, configured default used"
            )
        )

    render_glossary()
