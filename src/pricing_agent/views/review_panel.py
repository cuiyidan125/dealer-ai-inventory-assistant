"""One shared "things to review" panel.

Every workflow surfaces its warnings the same calm way: a single bordered card with a small
severity dot per row and a muted remediation line — never a stack of full-width coloured alert
banners, which read as an error state ("is the site broken?") rather than a checklist. Presentation
only; it renders warning dicts the workflows already produce and computes nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import streamlit as st

from pricing_agent.views import terminology as T

# A small dot per row conveys priority without a colour-filled box. Warm-leaning where it can be:
# red only for the genuinely blocking, amber for high, quieter tones below.
_SEVERITY_DOT = {
    "BLOCKING": "🔴", "CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵", "INFO": "🔵",
}


def _md(text: str) -> str:
    """Streamlit reads ``$...$`` as LaTeX, so escape dollars to keep money intact."""
    return text.replace("$", r"\$")


def severity_dot(severity: str | None) -> str:
    """The severity dot for a warning, for callers that render their own compact row."""
    return _SEVERITY_DOT.get(severity or "", "🟠")


def render_review_panel(
    warnings: Sequence[dict],
    *,
    heading: str,
    label_fn: Callable[[str], str] | None = None,
    detail_fn: Callable[[dict], str | None] | None = None,
    show_codes: bool = True,
) -> None:
    """Render `warnings` as one calm bordered panel.

    heading    — bold title inside the panel (e.g. "3 things to review before you acquire").
    label_fn   — code → dealer label; defaults to the shared terminology map.
    detail_fn  — optional per-warning extra line (e.g. observed vs threshold), shown muted.
    show_codes — tuck the raw warning codes into an expander for auditability.
    """
    if not warnings:
        return
    label = label_fn or T.warning_label
    with st.container(border=True):
        st.markdown(f"**{heading}**")
        for w in warnings:
            dot = _SEVERITY_DOT.get(w.get("severity", ""), "🟠")
            st.markdown(_md(f"{dot} **{label(w['code'])}** — {w['message']}"))
            if detail_fn is not None:
                extra = detail_fn(w)
                if extra:
                    st.caption(_md(extra))
            if w.get("remediation"):
                st.caption(_md(w["remediation"]))
        if show_codes:
            with st.expander("View technical reason codes"):
                st.caption("Warning codes: "
                           + ", ".join(f"`{w['code']}`" for w in warnings))
