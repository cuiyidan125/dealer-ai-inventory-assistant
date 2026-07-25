"""Presentation helpers for the Streamlit pages. No calculation happens here.

Two things live here:

* **Vehicle imagery.** `image_url` is null throughout the prototype because there is no
  image source, and using real manufacturer photography in a customer demo raises
  licensing questions we should not quietly take on. So the fallback is a generated
  body-style silhouette: it reads as deliberate rather than broken, and the moment a real
  integration populates `image_url` the photo takes over with no code change.
* **The aging timeline**, which turns "37 days in, about 30 more to go" into something a
  manager sees rather than computes.
"""

from __future__ import annotations

import base64
import io
import mimetypes
from functools import lru_cache
from pathlib import Path

import plotly.graph_objects as go

from pricing_agent.config.loader import REPO_ROOT

# Both the photo and the silhouette render at this height, so the card is the same size
# whichever one a vehicle gets and the page does not reflow between vehicles.
CARD_HEIGHT = 130

# Silhouettes are schematic on purpose. A crude attempt at photorealism would read as a
# failed photo; a clean icon reads as a placeholder that someone chose.
_BODIES: dict[str, str] = {
    "SEDAN": (
        "M18,60 C18,52 22,47 32,46 L70,44 L92,29 C96,26 101,25 107,25 "
        "L132,25 C139,25 145,28 149,33 L160,45 L176,48 C183,50 186,54 186,60 Z"
    ),
    "SUV": (
        "M16,60 L16,44 C16,38 20,34 28,33 L64,32 L78,20 C81,17 86,16 92,16 "
        "L138,16 C145,16 150,19 153,24 L160,33 L180,36 C186,38 188,42 188,48 L188,60 Z"
    ),
    "TRUCK": (
        "M14,60 L14,42 C14,37 18,34 25,33 L58,32 L70,19 C73,16 77,15 82,15 "
        "L108,15 C114,15 118,18 120,23 L126,33 L130,33 L130,39 L190,39 L190,60 Z"
    ),
    "VAN": (
        "M16,60 L16,30 C16,23 21,19 30,18 L150,18 C166,18 178,26 186,40 "
        "L188,48 L188,60 Z"
    ),
}

_WHEELS: dict[str, tuple[int, int, int]] = {
    # (front cx, rear cx, radius)
    "SEDAN": (58, 150, 12),
    "SUV": (54, 152, 13),
    "TRUCK": (52, 160, 13),
    "VAN": (52, 156, 13),
}

_WINDOWS: dict[str, str] = {
    "SEDAN": "M96,31 L118,31 L118,43 L86,43 Z M124,31 L142,31 L150,43 L124,43 Z",
    "SUV": "M84,22 L112,22 L112,36 L74,36 Z M118,22 L140,22 L150,36 L118,36 Z",
    "TRUCK": "M76,21 L96,21 L96,33 L68,33 Z M102,21 L112,21 L119,33 L102,33 Z",
    "VAN": "M34,24 L74,24 L74,38 L34,38 Z M80,24 L120,24 L120,38 L80,38 Z",
}

# Segment is the vAuto field; body style is what a silhouette needs.
_SEGMENT_TO_BODY = {
    "SEDAN": "SEDAN",
    "LUXURY": "SEDAN",
    "SUV": "SUV",
    "EV": "SUV",
    "TRUCK": "TRUCK",
    "VAN": "VAN",
}


def resolve_image(image_url: str | None) -> Path | None:
    """Resolve a fixture `image_url` to a file on disk, or None.

    Resolved against the repository root rather than the working directory: Streamlit is
    usually launched from the root, but a page must not depend on that, and the same path
    is read by tests running from anywhere.

    Absolute paths and remote URLs are rejected. The demo must work offline, so a runtime
    fetch is not an acceptable fallback — an unreachable host would put a broken image in
    front of a customer.
    """
    if not image_url:
        return None
    if image_url.startswith(("http://", "https://", "//")):
        return None

    candidate = (REPO_ROOT / image_url).resolve()
    try:
        candidate.relative_to(REPO_ROOT.resolve())
    except ValueError:
        return None  # escaped the repository
    return candidate if candidate.is_file() else None


@lru_cache(maxsize=32)
def _data_uri(path: str, mtime: float) -> str:
    """Base64 data URI for a local image. Keyed on mtime so edits invalidate the cache."""
    file = Path(path)
    mime = mimetypes.guess_type(file.name)[0] or "image/jpeg"
    encoded = base64.b64encode(file.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


@lru_cache(maxsize=64)
def _thumbnail_uri(path: str, mtime: float, width: int) -> str:
    from PIL import Image  # local import: only the table path needs it

    with Image.open(path) as image:
        image = image.convert("RGB")
        if image.width > width:
            image = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=78, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def thumbnail_uri(image_url: str | None, width: int = 220) -> str | None:
    """A small data URI for table cells, or None when there is no usable image.

    Deliberately not the full asset: a dozen 400 KB images inlined into a dataframe would
    push several megabytes into every rerun. Cached on path and mtime.
    """
    path = resolve_image(image_url)
    if path is None:
        return None
    try:
        return _thumbnail_uri(str(path), path.stat().st_mtime, width)
    except OSError:
        return None


def vehicle_photo_html(path: Path, height: int = CARD_HEIGHT) -> str:
    """A local photo, embedded as a data URI and rendered through `components.html`.

    Embedded rather than served: Streamlit will not serve arbitrary repository paths to
    the browser, and a data URI keeps the page working with no network at all.

    `object-fit: cover` fills the card at a fixed height without distorting the car —
    the image is cropped, never stretched, which matters because a squashed vehicle in a
    dealer demo looks like a bug.
    """
    uri = _data_uri(str(path), path.stat().st_mtime)
    return f"""
<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  html {{ margin:0; padding:0; background:transparent; }}
  /* A little inset so the soft shadow has room to fall instead of clipping at the iframe edge. */
  body {{ margin:0; padding:3px 4px 12px; background:transparent; }}
  img {{ width:100%; height:{height}px; object-fit:cover; border-radius:12px;
         display:block; border:1px solid #EBE2DB;
         box-shadow:0 1px 2px rgba(30,26,23,.04), 0 6px 18px rgba(30,26,23,.10); }}
</style></head>
<body><img src="{uri}" alt="Vehicle photograph"></body></html>
""".strip()


def body_style(segment: str | None, model: str | None = None) -> str:
    """Map a segment to a body style, with a couple of model-level corrections.

    The EV segment covers both crossovers and sedans, so the segment alone is not enough
    where the shape would be obviously wrong.
    """
    name = (model or "").lower()
    if any(token in name for token in ("bolt", "leaf", "model 3", "ioniq 6")):
        return "SUV" if "euv" in name or "bolt euv" in name else "SEDAN"
    return _SEGMENT_TO_BODY.get((segment or "").upper(), "SEDAN")


def vehicle_silhouette_svg(segment: str | None, model: str | None = None) -> str:
    """A self-contained SVG silhouette, for rendering via `components.html`.

    NOT for `st.html` or `st.markdown(unsafe_allow_html=True)`: Streamlit's sanitizer
    strips `<svg>` entirely, leaving the wrapper div and its styling behind — which
    renders as a thin empty bar rather than an obvious failure. `components.html` puts it
    in an iframe, which is not sanitized.

    Colours are semi-transparent neutrals so the icon reads acceptably against either a
    light or a dark Streamlit theme, and the iframe body is left transparent so it
    inherits the page background rather than punching a white rectangle into a dark one.
    """
    style = body_style(segment, model)
    path = _BODIES[style]
    front, rear, radius = _WHEELS[style]
    windows = _WINDOWS[style]

    body = "rgba(100,116,139,0.88)"
    glass = "rgba(226,232,240,0.55)"
    backdrop = "rgba(100,116,139,0.10)"

    return f"""
<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  html,body {{ margin:0; padding:0; background:transparent; }}
  .frame {{ width:100%; height:100%; border-radius:10px; background:{backdrop};
            display:flex; align-items:center; justify-content:center; box-sizing:border-box; }}
</style></head>
<body>
  <div class="frame">
    <svg viewBox="0 0 200 80" width="94%" preserveAspectRatio="xMidYMid meet" role="img"
         aria-label="{style.title()} silhouette; no photograph available">
      <path d="{path}" fill="{body}"/>
      <path d="{windows}" fill="{glass}"/>
      <circle cx="{front}" cy="60" r="{radius}" fill="{body}"/>
      <circle cx="{front}" cy="60" r="{radius - 5}" fill="{glass}"/>
      <circle cx="{rear}" cy="60" r="{radius}" fill="{body}"/>
      <circle cx="{rear}" cy="60" r="{radius - 5}" fill="{glass}"/>
      <line x1="6" y1="62" x2="194" y2="62" stroke="{body}" stroke-width="1.5"
            stroke-linecap="round" opacity="0.45"/>
    </svg>
  </div>
</body></html>
""".strip()


def aging_timeline(
    days_in_inventory: int,
    additional_p50: float,
    additional_p90: float,
    thresholds: tuple[int, ...] = (90, 120),
) -> go.Figure:
    """Time on the lot so far, plus the projected run to sale.

    The P90 whisker is what makes this worth showing: a car whose median sale date clears
    90 days comfortably can still have a tail well past it, and the tail is what a manager
    is actually exposed to.
    """
    figure = go.Figure()

    figure.add_trace(
        go.Bar(
            y=["Age"],
            x=[days_in_inventory],
            base=0,
            orientation="h",
            name="Time on lot",
            marker=dict(color="rgba(122,111,101,0.85)"),  # warm neutral: the past/context
            hovertemplate="On the lot %{x:.0f} days<extra></extra>",
        )
    )
    figure.add_trace(
        go.Bar(
            y=["Age"],
            x=[additional_p50],
            base=days_in_inventory,
            orientation="h",
            name="Expected time to sale (P50)",
            marker=dict(color="rgba(238,90,42,0.9)"),  # vAuto orange: the P50 projection
            error_x=dict(
                type="data",
                symmetric=False,
                array=[max(0.0, additional_p90 - additional_p50)],
                thickness=1.4,
                width=6,
            ),
            hovertemplate="Median sale at %{x:.0f} more days<extra></extra>",
        )
    )

    for threshold in thresholds:
        figure.add_vline(
            x=threshold,
            line_dash="dot",
            line_color="rgba(178,44,20,0.7)",  # warm brick: the 90/120-day danger line
            annotation_text=f"{threshold}d",
            annotation_position="top",
        )

    figure.update_layout(
        barmode="stack",
        height=190,
        margin=dict(t=34, b=10, l=10, r=10),
        xaxis_title="Days on lot",
        yaxis=dict(showticklabels=False),
        legend=dict(orientation="h", yanchor="bottom", y=-0.55, x=0),
    )
    return figure
