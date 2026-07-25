"""Single source of the Streamlit page-configuration values.

`st.set_page_config` cannot be fully centralised into one call while filesystem pages
remain active, because Streamlit executes each page script as its own top-level script —
so each needs its own call to control its browser tab title. What *can* be centralised is
the values, which is what this module does: one place defines the icon and layout, and
each entry point makes one call through `configure_page`.

The navigation spike confirmed a repeat call does not raise on Streamlit 1.60 — the last
call wins — so this stays safe once `st.navigation` also configures the page.
"""

from __future__ import annotations

import streamlit as st

DEFAULT_ICON = "🚗"
DEFAULT_LAYOUT = "wide"

APP_TITLE = "Used Vehicle Pricing Advisor"
VEHICLE_DETAIL_TITLE = "Vehicle Detail"
PROMOTION_TITLE = "Promotion Planner"
PROMOTION_ICON = "🏷️"


def configure_page(title: str, icon: str = DEFAULT_ICON) -> None:
    """Configure the page. One call per top-level script execution."""
    st.set_page_config(page_title=title, page_icon=icon, layout=DEFAULT_LAYOUT)
    # Apply the shared style layer immediately after page config (must follow set_page_config).
    from pricing_agent.views.style import inject_css
    inject_css()
