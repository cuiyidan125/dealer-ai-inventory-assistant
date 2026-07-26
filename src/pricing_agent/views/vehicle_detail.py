"""Vehicle detail — demo beats 2 and 3.

The screen where the recommendation has to be trusted, so the price, the reason, the
floor, and the refusal all appear together rather than the price alone.

Extracted verbatim from `pages/1_Vehicle_Detail.py` in Phase 2. The body is unchanged;
only its enclosing function and the `workflow_context` parameter are new.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from pricing_agent.agents import extract, intent_of
from pricing_agent.llm import credentials_present, explain
from pricing_agent.mcp_clients import MockTransport, VautoClient
from pricing_agent.policy.price_floor import can_publish
from pricing_agent.skills.single_vehicle import analyze
from pricing_agent.views import terminology as T
from pricing_agent.views.glossary import render_glossary
from pricing_agent.views.review_panel import render_review_panel
from pricing_agent.views.style import style_fig
from pricing_agent.views.workflow_copy import render_workflow_header
from pricing_agent.workflows.context import WorkflowContext

import ui_components

AS_OF = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)


def md(text: str) -> str:
    """Escape dollar signs for Streamlit markdown.

    Streamlit reads `$...$` as LaTeX math, so any string quoting two dollar amounts —
    which is most warning messages, since every one reports an observed value against a
    threshold — silently loses both signs and italicises the text between them. Every
    money-bearing string rendered as markdown must go through this.
    """
    return text.replace("$", r"\$")


# Cached at module level on purpose: a nested @st.cache_data would be redefined on every
# render call, and the cache would never hit.
@st.cache_data(show_spinner=False)
def load_inventory(as_of: datetime) -> list[dict]:
    return VautoClient(MockTransport(as_of=as_of)).get_dealer_inventory().data


@st.cache_data(show_spinner=False)
def analyze_vehicle(vehicle_id: str, as_of: datetime) -> dict:
    return analyze(vehicle_id, MockTransport(as_of=as_of))


def render_vehicle_detail(workflow_context: WorkflowContext | None = None) -> None:
    """Render the vehicle detail screen.

    `workflow_context` selects the page copy and nothing else — the recommendation, the
    floor and every figure below are computed identically without it.
    """
    copy = render_workflow_header(workflow_context)
    if copy is not None and copy.instruction is not None:
        st.caption(copy.instruction)

    inventory = load_inventory(AS_OF)
    labels = {
        f"{v['vehicle_id']} · {v['year']} {v['make']} {v['model']} · {v['days_in_inventory']}d": v[
            "vehicle_id"
        ]
        for v in inventory
    }

    # If the assistant routed a vehicle here, preselect it once by seeding the widget's own
    # state before the widget is created — the supported way to set a default. It is popped
    # so a later manual change in the selectbox wins and is not overridden on the next run.
    routed_id = st.session_state.pop("assistant_selected_vehicle_id", None)
    if routed_id is not None:
        for label, vid in labels.items():
            if vid == routed_id:
                st.session_state["vehicle_detail_choice"] = label
                break

    choice = st.sidebar.selectbox("Vehicle", list(labels), key="vehicle_detail_choice")
    vehicle_id = labels[choice]

    with st.spinner("Analyzing…"):
        result = analyze_vehicle(vehicle_id, AS_OF)

    vehicle = result["vehicle"]
    valuation = result["valuation"]
    break_even = result["break_even_analysis"]
    sales = result["sales_outcome_distribution"]
    headroom = result["promotional_headroom"]
    recommended_strategy = result["recommended_strategy"]["strategy"]
    scenario = next(s for s in result["pricing_scenarios"] if s["strategy"] == recommended_strategy)

    image_column, title_column = st.columns([1, 3], vertical_alignment="center")

    with image_column:
        # A photo when one resolves on disk; the generated silhouette only as a fallback.
        # components.html rather than st.html throughout: Streamlit's sanitizer strips <svg>
        # and leaves the wrapper behind, which renders as an empty bar rather than failing
        # visibly.
        photo_markup = None
        photo_path = ui_components.resolve_image(vehicle.get("image_url"))
        if photo_path is not None:
            try:
                photo_markup = ui_components.vehicle_photo_html(photo_path)
            except OSError:
                # Present in the fixture but unreadable — fall back rather than break.
                photo_markup = None

        if photo_markup is not None:
            components.html(photo_markup, height=ui_components.CARD_HEIGHT + 18)
            st.caption("Merchandising photo")
        else:
            components.html(
                ui_components.vehicle_silhouette_svg(
                    vehicle.get("segment"), vehicle.get("model")
                ),
                height=ui_components.CARD_HEIGHT + 6,
            )
            st.caption(
                "No photo on file · "
                f"{ui_components.body_style(vehicle.get('segment'), vehicle.get('model')).title()}"
            )

    with title_column:
        # The page heading belongs to the workflow when one is bound, so the vehicle
        # becomes the subject beneath it rather than a competing <h1>.
        vehicle_heading = st.header if copy is not None else st.title
        vehicle_heading(
            f"{vehicle['year']} {vehicle['make']} {vehicle['model']} {vehicle['trim'] or ''}".strip()
        )
        st.caption(
            f"{vehicle['vehicle_id']} · {vehicle['mileage']:,} miles · "
            f"{vehicle['days_in_inventory']} days in inventory · {vehicle['condition'].title()}"
        )

    # --- natural-language intake (§4.2) -----------------------------------------------

    with st.expander("Ask in plain English", expanded=False):
        st.caption(
            "The model reads the request into validated JSON and hands it to the engine. It "
            "never produces a number — the extraction schema has no property for one."
        )
        question = st.text_area(
            "Request",
            value=(
                "Analyze this 2022 Toyota RAV4 XLE with 42,000 miles. We paid $23,500 and "
                "spent $1,200 on reconditioning. It has been in inventory for 37 days. Tell "
                "me what it is worth, how much discount room we have, and the expected P50 "
                "and P90 sales time."
            ),
            height=110,
            label_visibility="collapsed",
        )
        if st.button("Extract"):
            request, llm_result, errors = extract(question, as_of=AS_OF)
            badge = "🟢 live model" if llm_result.live else "🟡 recorded fallback"
            st.caption(f"{badge} · routed to `{intent_of(llm_result)}`"
                       + (f" · {llm_result.note}" if llm_result.note else ""))

            if errors:
                st.error(
                    "Extraction did not validate, so it was not passed to any tool (§4.2):\n\n"
                    + "\n".join(f"- {e}" for e in errors)
                )
            else:
                st.success("Schema-valid — safe to pass to the pricing engine.", icon="✅")

            left, right = st.columns(2)
            left.markdown("**Extracted request**")
            left.json({k: v for k, v in request.items() if k not in ("extraction_provenance",)})
            right.markdown("**Field provenance**")
            right.dataframe(
                pd.DataFrame(request["extraction_provenance"]),
                hide_index=True,
            )
            st.caption(
                "Note what is absent: no price, no valuation, no days-to-sale. Those fields "
                "do not exist in the extraction schema, which is the structural half of §4.1."
            )

    # --- the refusal, before anything else --------------------------------------------

    publishable, reasons = can_publish(result["warnings"], [])
    if not publishable:
        st.error(
            md(
                "**This price cannot be published.**\n\n"
                + "\n".join(f"- {reason}" for reason in reasons)
            ),
            icon="🚫",
        )

    # --- headline ---------------------------------------------------------------------

    c1, c2, c3, c4 = st.columns(4)
    current = vehicle["current_list_price"]
    recommended = scenario["proposed_list_price"]

    c1.metric(
        T.metric("recommended_price"),
        f"${recommended:,.0f}",
        f"${recommended - current:+,.0f} vs current" if current else None,
    )
    c2.metric(T.metric("market_value"), f"${valuation['market_value']:,.0f}", valuation["anchor"].title())
    c3.metric(
        T.metric("expected_gross_p50"),
        f"${scenario['expected_front_end_gross']['p50']:,.0f}",
        f"Downside (P10) ${scenario['expected_front_end_gross']['p10']:,.0f}",
        delta_color="off",
    )
    c4.metric(
        T.metric("expected_days_p50"),
        f"{scenario['additional_days_to_sale']['p50']:.0f}",
        f"Conservative (P90) {scenario['additional_days_to_sale']['p90']:.0f} days",
        delta_color="off",
    )

    st.caption(
        f"Recommended pricing approach: **{T.strategy_name(recommended_strategy)}** — "
        f"{T.STRATEGY.get(recommended_strategy, {}).get('trade_off', '')}"
    )
    with st.expander("View technical reason codes"):
        st.caption("Rationale codes: "
                   + " · ".join(f"`{c}`" for c in result["recommended_strategy"]["rationale_codes"]))

    # --- narration, constrained to the allow-list -------------------------------------

    narrative = explain(result["explanation_inputs"], result["warnings"])
    with st.container(border=True):
        st.markdown(md(narrative.text))
        caption = f"Explanation source: **{narrative.source_label}**"
        if not credentials_present():
            caption += " — no API key set, so the deterministic template is used."
        st.caption(caption)

        if narrative.rejected:
            # The model cited something the engine never published, so its prose was
            # discarded. Showing that this happened is more valuable than hiding it.
            st.warning(
                md(
                    "The generated explanation was **rejected** and replaced with the "
                    "template. It cited figures the engine never produced: "
                    + ", ".join(narrative.rejected)
                ),
                icon="🛡️",
            )

    # --- aging timeline ---------------------------------------------------------------

    st.subheader("Time on lot")
    st.caption("See whether the vehicle is likely to cross key aging thresholds before it sells.")
    st.plotly_chart(
        ui_components.aging_timeline(
            days_in_inventory=vehicle["days_in_inventory"],
            additional_p50=scenario["additional_days_to_sale"]["p50"],
            additional_p90=scenario["additional_days_to_sale"]["p90"],
        )
    )
    projected = sales["projected_total_inventory_age"]
    st.caption(
        md(
            f"Expected total days on lot at sale **{projected['p50']:.0f} days (P50)**, "
            f"**{projected['p90']:.0f} days** in a conservative case (P90). "
            f"Risk of exceeding 90 days on lot: {sales['projected_age_exceedance']['over_90_days']:.0%}. "
            "A vehicle whose expected time clears 90 days can still carry a longer tail — that "
            "tail is the real exposure."
        )
    )

    # --- warnings ---------------------------------------------------------------------

    if result["warnings"]:
        def _observed_vs_threshold(w: dict) -> str | None:
            if w.get("observed") is not None and w.get("threshold") is not None:
                return (f"Observed {w['observed']:,.2f} against a limit of "
                        f"{w['threshold']:,.2f} ({w['unit'].lower()}).")
            return None
        render_review_panel(
            result["warnings"],
            heading="What to review before changing the price",
            detail_fn=_observed_vs_threshold,
        )

    if result["approvals_required"]:
        kinds = [a["approval_type"] for a in result["approvals_required"]]
        st.warning(
            "**Manager review required.** "
            + " ".join(dict.fromkeys(T.approval_why(k) for k in kinds)),
            icon="✋",
        )

    st.divider()

    # --- strategies -------------------------------------------------------------------

    left, right = st.columns([3, 2])

    with left:
        st.subheader("Profit and sales-speed trade-off")
        st.caption(
            "Compare how each pricing approach affects expected sales speed, front-end gross, "
            "and total economic value. The whiskers show the range from downside to conservative."
        )

        figure = go.Figure()
        for item in result["pricing_scenarios"]:
            is_recommended = item["strategy"] == recommended_strategy
            figure.add_trace(
                go.Scatter(
                    x=[item["additional_days_to_sale"]["p50"]],
                    y=[item["expected_front_end_gross"]["p50"]],
                    mode="markers+text",
                    text=[T.strategy_name(item["strategy"])],
                    textposition="top center",
                    marker=dict(size=22 if is_recommended else 14,
                                symbol="star" if is_recommended else "circle"),
                    error_x=dict(
                        type="data",
                        symmetric=False,
                        array=[item["additional_days_to_sale"]["p90"] - item["additional_days_to_sale"]["p50"]],
                        arrayminus=[item["additional_days_to_sale"]["p50"] - item["additional_days_to_sale"]["p10"]],
                        thickness=1,
                    ),
                    name=f"${item['proposed_list_price']:,.0f}",
                )
            )
        figure.add_hline(y=0, line_dash="dot", line_color="grey")
        figure.update_layout(
            xaxis_title="Expected days to sale (P50) — range shows downside to conservative",
            yaxis_title="Expected front-end gross (P50), $",
            height=380,
            margin=dict(t=20, b=10),
        )
        st.plotly_chart(style_fig(figure))

        # Decision-ordered columns; every value read from the result (no calculation here).
        table = pd.DataFrame(
            [
                {
                    "Pricing approach": T.strategy_name(s["strategy"]),
                    "Recommended asking price": s["proposed_list_price"],
                    "Expected days to sale (P50)": s["additional_days_to_sale"]["p50"],
                    # Whole percent: NumberColumn's "%%" format does not scale the value.
                    "Chance of selling within 30 days": s["sale_probabilities"]["within_30_days"] * 100.0,
                    "Expected front-end gross (P50)": s["expected_front_end_gross"]["p50"],
                    "Expected total economic value (P50)": s["expected_net_economic_value"]["p50"],
                    "Downside total economic value (P10)": s["expected_net_economic_value"]["p10"],
                }
                for s in result["pricing_scenarios"]
            ]
        )
        st.dataframe(
            table,
            hide_index=True,
            column_config={
                "Recommended asking price": st.column_config.NumberColumn(format="$%d"),
                "Expected days to sale (P50)": st.column_config.NumberColumn(format="%d"),
                "Chance of selling within 30 days": st.column_config.NumberColumn(format="%.0f%%"),
                "Expected front-end gross (P50)": st.column_config.NumberColumn(format="$%d"),
                "Expected total economic value (P50)": st.column_config.NumberColumn(format="$%d"),
                "Downside total economic value (P10)": st.column_config.NumberColumn(format="$%d"),
            },
        )
        st.caption("Compares how each asking price changes expected sales speed, profit, and "
                   "total economic value. Price position vs similar vehicles and the customer "
                   "value rating are in the comparable-listings section below.")

    with right:
        st.subheader("Financial safety")
        st.caption("Understand how far the asking price can move before crossing break-even or "
                   "approval rules.")
        floors = break_even["floors"]
        st.metric(T.metric("break_even"), f"${break_even['current_accounting_break_even']:,.0f}")
        st.metric(T.metric("min_safe"), f"${break_even['minimum_safe_list_price']:,.0f}")
        st.caption(
            f"Rule setting the lowest safe price: **{floors['binding_constraint'].replace('_', ' ').title()}** · "
            f"assumes {break_even['expected_discount_rate_used']:.1%} negotiation off asking price."
        )

        crossover = break_even["market_value_crossover_risk"]
        if crossover["break_even_exceeds_market_value_now"]:
            st.error(
                md(
                    f"Break-even of **${break_even['current_accounting_break_even']:,.0f}** exceeds "
                    f"market value of **${valuation['market_value']:,.0f}**. "
                    "This vehicle cannot be sold at a profit today."
                ),
                icon="🔴",
            )
        elif crossover["estimated_crossover_days"]:
            st.warning(
                f"Break-even overtakes market value in about "
                f"**{crossover['estimated_crossover_days']} days**.",
                icon="⏳",
            )

        st.metric(T.metric("max_safe_discount"), f"${headroom['max_safe_discount']:,.0f}")
        st.caption("Safe room for an additional discount before crossing the safety boundary.")

    # --- comparables ------------------------------------------------------------------

    st.subheader("Comparable vehicles")
    st.caption("See whether the recommended asking price is high, low, or aligned with similar "
               "vehicles.")
    included = [c for c in result["comparables"] if c["included"]]
    excluded = [c for c in result["comparables"] if not c["included"]]

    if included:
        comps_figure = go.Figure()
        comps_figure.add_trace(
            go.Scatter(
                x=[c["mileage"] for c in included],
                y=[c["list_price"] for c in included],
                mode="markers",
                name="Comparable listings",
                text=[c["listing_id"] for c in included],
                marker=dict(size=10, opacity=0.65),
            )
        )
        comps_figure.add_trace(
            go.Scatter(
                x=[vehicle["mileage"]],
                y=[recommended],
                mode="markers+text",
                name="This vehicle at recommended price",
                text=["This vehicle"],
                textposition="top center",
                marker=dict(size=20, symbol="star"),
            )
        )
        comps_figure.update_layout(
            xaxis_title="Mileage", yaxis_title="Asking price, $", height=360,
            margin=dict(t=20, b=10),
        )
        st.plotly_chart(style_fig(comps_figure))

    st.caption(
        md(
            f"{len(included)} comparables used, {len(excluded)} excluded. "
            f"Internal estimate: "
            + (
                f"${valuation['internal_estimate']:,.0f}"
                if valuation["internal_estimate"]
                else "not computed — too few comparables to form a second opinion"
            )
            + (
                f" · differs from the external anchor by {valuation['divergence']:.1%}"
                if valuation["divergence"] is not None
                else ""
            )
        )
    )

    with st.expander(f"Excluded comparables ({len(excluded)})"):
        if excluded:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Listing": c["listing_id"],
                            "Vehicle": f"{c['year']} {c['make']} {c['model']} {c['trim'] or ''}",
                            "Mileage": c["mileage"],
                            "Price": c["list_price"],
                            "Distance": c["distance_miles"],
                            "Reason": c["exclusion_reason"],
                        }
                        for c in excluded
                    ]
                ),
                hide_index=True,
            )
        else:
            st.write("None.")

    # --- receipts ---------------------------------------------------------------------

    with st.expander("Assumptions and audit trail"):
        audit = result["audit"]
        st.write(
            f"**Simulation** — {audit['simulation']['draw_count']:,} draws, seed "
            f"`{audit['simulation']['seed']}`, label `{audit['simulation']['model_label']}`"
        )
        st.write(
            f"**Versions** — config `{audit['config_version']}`, assumptions "
            f"`{audit['assumption_version']}`, percentile convention "
            f"`{audit['percentile_convention']}`"
        )
        st.write(
            f"**Valuation source** — anchor `{audit['valuation_source']['anchor']}`, "
            f"internal check computed: {audit['valuation_source']['internal_check_computed']}"
        )
        st.write("**MCP tools called**")
        st.dataframe(
            pd.DataFrame(audit["mcp_tools_called"]), hide_index=True
        )
        st.write("**Values the explanation layer may cite**")
        allow_list = pd.DataFrame(result["explanation_inputs"]["values"])

        # The allow-list is genuinely mixed by design — labels like MAXIMIZE_GROSS sit
        # alongside figures like 29195 in one column — so Arrow cannot infer a type and
        # throws on every render. Streamlit recovers, but noisily. Rendering the column as
        # text is the honest fix: this table is for reading, not arithmetic.
        def _cell(value) -> str:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return str(value)
            # Dollars and day counts are integral; ratios are not, and need their decimals.
            return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.4g}"

        if "value" in allow_list.columns:
            allow_list["value"] = allow_list["value"].map(_cell)
        st.dataframe(allow_list, hide_index=True)
        st.caption(
            "The narration layer receives only this table. A currency figure in generated "
            "prose that does not appear here fails the response rather than reaching you."
        )

    render_glossary()
