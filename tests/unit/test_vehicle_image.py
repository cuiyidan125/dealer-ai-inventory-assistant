"""Vehicle imagery: every demo record has one, and the fallback still works.

The fallback chain is the point. A fixture may name a file that is absent, unreadable, or
remote, and none of those may put a broken image in front of a customer — so the negative
cases get as much attention as the happy path.
"""

from __future__ import annotations

import base64
import json

import pytest

import ui_components as ui
from pricing_agent.config.loader import REPO_ROOT

ASSETS = REPO_ROOT / "assets" / "vehicles"


def _inventory() -> list[dict]:
    doc = json.loads(
        (REPO_ROOT / "mocks" / "inventory" / "dealer-1001-inventory.json").read_text(
            encoding="utf-8"
        )
    )
    return doc["data"]["vehicles"]


VEHICLES = _inventory()
IDS = [v["vehicle_id"] for v in VEHICLES]

# Every demo unit now carries a sourced, licensed photograph (Wikimedia Commons, CC0/CC BY-SA).
# The silhouette fallback remains available for any vehicle without a usable image_url; it is
# exercised directly by the fallback tests below rather than by a fixture with no photo.
PHOTO_VEHICLES = [v for v in VEHICLES if v.get("image_url")]
PHOTO_IDS = [v["vehicle_id"] for v in PHOTO_VEHICLES]
SILHOUETTE_VEHICLES = [v for v in VEHICLES if not v.get("image_url")]
SILHOUETTE_IDS = [v["vehicle_id"] for v in SILHOUETTE_VEHICLES]


# --- every demo vehicle has a working image -------------------------------------------


def test_the_demo_fleet_is_the_expected_size():
    assert len(VEHICLES) == 20
    assert len(PHOTO_VEHICLES) == 20       # every unit carries a sourced photograph
    assert len(SILHOUETTE_VEHICLES) == 0   # no unit relies on the silhouette fallback


@pytest.mark.parametrize("vehicle", PHOTO_VEHICLES, ids=PHOTO_IDS)
def test_every_vehicle_has_a_non_empty_image_url(vehicle):
    assert vehicle.get("image_url"), f"{vehicle['vehicle_id']} has no image_url"
    assert vehicle["image_url"].startswith("assets/vehicles/")


@pytest.mark.parametrize("vehicle", PHOTO_VEHICLES, ids=PHOTO_IDS)
def test_every_referenced_file_exists_and_is_a_real_image(vehicle):
    path = ui.resolve_image(vehicle["image_url"])
    assert path is not None, f"{vehicle['image_url']} did not resolve"
    assert path.is_file()
    assert path.stat().st_size > 20_000, "suspiciously small for a photograph"
    assert path.read_bytes()[:3] == b"\xff\xd8\xff", "not a JPEG"


@pytest.mark.parametrize("vehicle", PHOTO_VEHICLES, ids=PHOTO_IDS)
def test_every_vehicle_renders_a_photo_not_a_silhouette(vehicle):
    markup = ui.vehicle_photo_html(ui.resolve_image(vehicle["image_url"]))
    assert "data:image/jpeg;base64," in markup
    assert "<svg" not in markup, "this vehicle fell back to the silhouette"


def test_every_asset_on_disk_is_referenced_by_a_vehicle():
    """Catches images left behind after a swap, which would bloat the repo silently."""
    referenced = {v["image_url"].rsplit("/", 1)[-1] for v in PHOTO_VEHICLES}
    on_disk = {p.name for p in ASSETS.glob("*.jpg")}
    assert on_disk == referenced, f"orphaned: {sorted(on_disk - referenced)}"


def test_the_two_rav4s_have_distinct_images():
    """V-10001 and V-10007 are the duplicate-inventory pair; identical photos would make
    the cannibalization story confusing to look at."""
    pair = [v["image_url"] for v in VEHICLES if v["vehicle_id"] in ("V-10001", "V-10007")]
    assert len(pair) == 2 and pair[0] != pair[1]


# --- fallback -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "assets/vehicles/does_not_exist.jpg",
        "https://example.com/car.jpg",
        "http://example.com/car.jpg",
        "//example.com/car.jpg",
    ],
)
def test_unusable_values_resolve_to_none_so_the_silhouette_takes_over(value):
    """Remote URLs are refused deliberately: the demo must work with no network, and a
    runtime fetch would put an unreachable host in front of a customer."""
    assert ui.resolve_image(value) is None
    assert ui.thumbnail_uri(value) is None


def test_paths_cannot_escape_the_repository():
    assert ui.resolve_image("../../../etc/passwd") is None


def test_silhouette_is_still_available_for_every_body_style():
    for segment in ("SEDAN", "SUV", "TRUCK", "VAN", "LUXURY", "EV"):
        markup = ui.vehicle_silhouette_svg(segment, None)
        assert markup.startswith("<!DOCTYPE html>")
        assert "<svg" in markup


def test_resolution_is_repo_relative_not_cwd_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert ui.resolve_image(VEHICLES[0]["image_url"]) is not None


# --- rendering rules ------------------------------------------------------------------


def test_photo_markup_embeds_rather_than_links():
    markup = ui.vehicle_photo_html(ui.resolve_image(VEHICLES[0]["image_url"]))
    assert "http://" not in markup and "https://" not in markup
    encoded = markup.split("base64,", 1)[1].split('"', 1)[0]
    assert base64.b64decode(encoded[:64] + "=" * (-len(encoded[:64]) % 4))[:3] == b"\xff\xd8\xff"


def test_photo_fills_the_card_without_distorting_the_vehicle():
    markup = ui.vehicle_photo_html(ui.resolve_image(VEHICLES[0]["image_url"]))
    assert "object-fit:cover" in markup
    assert "border-radius:10px" in markup
    assert "width:100%" in markup
    assert f"height:{ui.CARD_HEIGHT}px" in markup
    assert "background:transparent" in markup


def test_thumbnails_are_far_smaller_than_the_full_asset():
    """A dozen full-size images inlined into a dataframe would push megabytes through
    every rerun."""
    vehicle = VEHICLES[0]
    thumbnail = ui.thumbnail_uri(vehicle["image_url"])
    assert thumbnail and thumbnail.startswith("data:image/jpeg;base64,")

    full_size = ui.resolve_image(vehicle["image_url"]).stat().st_size
    assert len(thumbnail) < full_size / 2


def test_thumbnails_are_cached_by_path():
    first = ui.thumbnail_uri(VEHICLES[1]["image_url"])
    second = ui.thumbnail_uri(VEHICLES[1]["image_url"])
    assert first is second


# --- attribution ----------------------------------------------------------------------


def test_attribution_covers_every_vehicle_and_file():
    text = (ASSETS / "ATTRIBUTION.md").read_text(encoding="utf-8")
    for vehicle in PHOTO_VEHICLES:
        assert vehicle["vehicle_id"] in text, f"no attribution entry for {vehicle['vehicle_id']}"
        assert vehicle["image_url"].rsplit("/", 1)[-1] in text
    for required in ("Source website", "Licence", "Creator", "commons.wikimedia.org"):
        assert required in text
