"""One small, tasteful CSS layer applied on every page.

Pure presentation: it changes no application logic and adds no components. It targets Streamlit's
stable ``data-testid`` hooks to give the default components a designed, vAuto-warm feel — metric
cards, bordered containers, buttons, the chat composer, expanders, and heading rhythm. No accent
stripes or underlines (those read as AI filler); the polish is soft surfaces, rounded corners, and
gentle shadows on the warm palette already declared in ``.streamlit/config.toml``.
"""

from __future__ import annotations

import streamlit as st

# Palette echoes .streamlit/config.toml so the CSS and the theme never drift.
_CSS = """
<style>
:root {
  --v-orange: #EE5A2A;
  --v-surface: #FBF8F6;
  --v-surface-2: #F5F1EE;
  --v-ink: #1E1A17;
  --v-muted: #7C6E63;
  --v-border: #EBE2DB;
  --v-shadow: 0 1px 2px rgba(30,26,23,.04), 0 6px 18px rgba(30,26,23,.06);
  --v-radius: 14px;
}

/* Roomier, centred content on wide pages so long lines stay readable. */
.block-container { padding-top: 2.2rem; max-width: 1320px; }

/* Heading rhythm: tighter, warmer, a touch more weight. */
h1, h2, h3 { color: var(--v-ink); letter-spacing: -.01em; font-weight: 700; }
h1 { margin-bottom: .35rem; }
h2 { margin-top: 1.4rem; }
h3 { margin-top: 1.0rem; }

/* Metric cards: the single biggest lift — no more bare numbers on white. */
[data-testid="stMetric"] {
  background: var(--v-surface);
  border: 1px solid var(--v-border);
  border-radius: var(--v-radius);
  padding: 15px 18px;
  box-shadow: var(--v-shadow);
  transition: box-shadow .15s ease, transform .15s ease;
}
[data-testid="stMetric"]:hover {
  box-shadow: 0 2px 4px rgba(30,26,23,.05), 0 12px 26px rgba(30,26,23,.09);
  transform: translateY(-1px);
}
[data-testid="stMetricLabel"] { color: var(--v-muted); font-weight: 600; }
[data-testid="stMetricValue"] { font-weight: 700; letter-spacing: -.02em; }

/* Bordered containers (st.container(border=True)) — softer than the default hairline. */
[data-testid="stVerticalBlockBorderWrapper"] {
  border-radius: var(--v-radius) !important;
  border-color: var(--v-border) !important;
  box-shadow: var(--v-shadow);
}

/* Buttons: rounded, confident, warm hover. */
.stButton > button, .stDownloadButton > button, [data-testid="stFormSubmitButton"] button {
  border-radius: 10px;
  font-weight: 600;
  padding: .5rem 1.15rem;
  border: 1px solid var(--v-border);
  transition: transform .12s ease, box-shadow .12s ease, filter .12s ease;
}
.stButton > button[kind="primary"] {
  box-shadow: 0 2px 10px rgba(238,90,42,.28);
  border: none;
}
.stButton > button[kind="primary"]:hover { filter: brightness(1.05); transform: translateY(-1px); }
.stButton > button:hover { transform: translateY(-1px); box-shadow: var(--v-shadow); }

/* Chat composer: reads like a real input, not a bare box. */
[data-testid="stChatInput"] {
  border-radius: 12px;
  border: 1px solid var(--v-border);
  box-shadow: var(--v-shadow);
  background: var(--v-surface);
}

/* Expanders: rounded, quiet until opened. */
[data-testid="stExpander"] details {
  border-radius: 10px;
  border: 1px solid var(--v-border);
  background: var(--v-surface);
}
[data-testid="stExpander"] summary { font-weight: 600; }

/* Alert callouts (st.info/success/warning): a touch rounder, less boxy. */
[data-testid="stAlert"] { border-radius: 12px; }

/* Sidebar: a hair more separation from the page. */
[data-testid="stSidebar"] { border-right: 1px solid var(--v-border); }

/* Dataframes: rounded frame so tables match the cards. */
[data-testid="stDataFrame"] { border-radius: 12px; overflow: hidden; border: 1px solid var(--v-border); }

/* Hero assistant input: the page's primary call to action, made unmistakable — a warm surface,
   a confident orange-tinted border, and a clear focus ring, rather than a faint grey box. */
.st-key-assistant_input label p { font-size: 1.05rem; font-weight: 700; color: var(--v-ink); }
.st-key-assistant_input div:has(> textarea) {
  border: 2px solid #F1C7B4 !important;
  background: var(--v-surface) !important;
  border-radius: 14px !important;
  box-shadow: 0 3px 16px rgba(238,90,42,.12);
  transition: border-color .15s ease, box-shadow .15s ease;
}
.st-key-assistant_input div:has(> textarea):focus-within {
  border-color: var(--v-orange) !important;
  box-shadow: 0 0 0 4px rgba(238,90,42,.16);
}
.st-key-assistant_input textarea { font-size: 1.06rem !important; }
.st-key-assistant_input textarea::placeholder { color: #B6A89C; }
</style>
"""


def inject_css() -> None:
    """Apply the shared style layer. Safe to call once per page, after set_page_config."""
    st.markdown(_CSS, unsafe_allow_html=True)


# --- Plotly styling -----------------------------------------------------------------------
# So charts sit *on* the warm card surfaces instead of punching a white rectangle into them.
_CHART_INK = "#1E1A17"
_CHART_MUTED = "#7C6E63"
_CHART_GRID = "#EBE2DB"
# Warm colourway, echoing the vAuto orange. Only touches traces that set no explicit colour,
# so the promotion feasibility bars (green/amber by outcome) keep their own meaning.
_CHART_COLORWAY = ["#EE5A2A", "#E8A33D", "#C2410C", "#B08968", "#7C6E63"]

_AXIS = dict(
    gridcolor=_CHART_GRID,
    linecolor=_CHART_GRID,
    zerolinecolor="#E0D5CC",
    tickfont=dict(color=_CHART_MUTED, size=11),
    title_font=dict(color=_CHART_MUTED, size=12),
)


def style_fig(fig):
    """Give a Plotly figure the shared warm, transparent look. Returns it for chaining.

    Presentation only — it never changes a trace's data or an explicitly set colour. It clears
    the canvas to transparent, applies a warm colourway to un-coloured traces, quiets the
    gridlines, and mutes the axis type. Call right before ``st.plotly_chart``.
    """
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        colorway=_CHART_COLORWAY,
        font=dict(color=_CHART_INK, size=12),
        legend=dict(font=dict(color=_CHART_MUTED, size=11)),
    )
    fig.update_xaxes(**_AXIS)
    fig.update_yaxes(**_AXIS)
    return fig
