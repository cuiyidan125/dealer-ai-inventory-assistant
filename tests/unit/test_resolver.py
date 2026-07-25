"""Resolving a parsed vehicle against real inventory. Phase 4.

A name is not a match. These tests hold the four things the resolver must never get wrong:
it resolves a unique description, it refuses to pick between several silently, it does not
fabricate a vehicle that is not on the lot, and it says plainly when there is too little to
go on.
"""

from __future__ import annotations

from pricing_agent.agents.resolver import MatchStatus, resolve_vehicle
from pricing_agent.agents.router import ParsedVehicle, parse_vehicle

# A small inventory with a deliberate duplicate: two 2022 Toyota RAV4 XLE.
INVENTORY = [
    {"vehicle_id": "V-10001", "vin": "JTMWFREV6ND512001", "year": 2022, "make": "Toyota",
     "model": "RAV4", "trim": "XLE", "mileage": 42000, "current_list_price": 28995,
     "days_in_inventory": 37},
    {"vehicle_id": "V-10003", "vin": "1FTEW1EP5LKD10203", "year": 2020, "make": "Ford",
     "model": "F-150", "trim": "XLT", "mileage": 68000, "current_list_price": 32995,
     "days_in_inventory": 51},
    {"vehicle_id": "V-10007", "vin": "JTMWFREV1ND512007", "year": 2022, "make": "Toyota",
     "model": "RAV4", "trim": "XLE", "mileage": 39000, "current_list_price": 29495,
     "days_in_inventory": 29},
]


def resolve(text: str):
    return resolve_vehicle(parse_vehicle(text), INVENTORY)


# --- exact matches --------------------------------------------------------------------


def test_exact_match_by_year_make_model_trim():
    match = resolve("price the 2020 Ford F-150 XLT")
    assert match.status is MatchStatus.EXACT
    assert match.vehicle_id == "V-10003"


def test_exact_match_by_vehicle_id():
    match = resolve_vehicle(ParsedVehicle(vehicle_id="V-10003"), INVENTORY)
    assert match.status is MatchStatus.EXACT
    assert match.vehicle_id == "V-10003"
    assert "MATCHED_VEHICLE_ID" in match.reason_codes


def test_exact_match_by_vin():
    match = resolve_vehicle(ParsedVehicle(vin="1FTEW1EP5LKD10203"), INVENTORY)
    assert match.status is MatchStatus.EXACT
    assert match.vehicle_id == "V-10003"
    assert "MATCHED_VIN" in match.reason_codes


def test_make_model_unique_match_without_year():
    match = resolve("price the Ford F-150")
    assert match.status is MatchStatus.EXACT
    assert match.vehicle_id == "V-10003"


def test_model_punctuation_is_ignored_when_matching():
    assert resolve("price the Ford F150").vehicle_id == "V-10003"


# --- ambiguous ------------------------------------------------------------------------


def test_ambiguous_match_returns_all_candidates():
    match = resolve("price the 2022 Toyota RAV4 XLE")
    assert match.status is MatchStatus.AMBIGUOUS
    ids = {c["vehicle_id"] for c in match.candidates}
    assert ids == {"V-10001", "V-10007"}


def test_ambiguous_match_chooses_nothing():
    assert resolve("price the 2022 Toyota RAV4 XLE").vehicle_id is None


def test_candidates_carry_only_fixture_values_not_computed_ones():
    match = resolve("price the 2022 Toyota RAV4 XLE")
    for candidate in match.candidates:
        assert set(candidate) <= {
            "vehicle_id", "vin", "year", "make", "model", "trim", "mileage",
            "current_list_price", "days_in_inventory",
        }


# --- no match -------------------------------------------------------------------------


def test_no_match_for_a_vehicle_not_in_inventory():
    match = resolve("price a 2019 Tesla Model 3")
    assert match.status is MatchStatus.NONE


def test_no_match_does_not_fabricate_a_vehicle_id():
    assert resolve("price a 2019 Tesla Model 3").vehicle_id is None


def test_unknown_vehicle_id_is_no_match():
    match = resolve_vehicle(ParsedVehicle(vehicle_id="V-99999"), INVENTORY)
    assert match.status is MatchStatus.NONE


def test_year_not_in_inventory_is_no_match():
    match = resolve("price the 1998 Ford F-150")
    assert match.status is MatchStatus.NONE


# --- insufficient ---------------------------------------------------------------------


def test_insufficient_when_no_make_or_model():
    match = resolve("what should I price this vehicle?")
    assert match.status is MatchStatus.INSUFFICIENT


def test_make_alone_resolves_when_unique():
    # Only one Ford on the lot — a make alone is enough to resolve it, no full description needed.
    match = resolve_vehicle(ParsedVehicle(make="Ford"), INVENTORY)
    assert match.status is MatchStatus.EXACT
    assert match.vehicle_id == "V-10003"


def test_make_alone_surfaces_candidates_when_several():
    # Two Toyotas — a make alone narrows the lot to a set the dealer picks from, not a dead end.
    match = resolve_vehicle(ParsedVehicle(make="Toyota"), INVENTORY)
    assert match.status is MatchStatus.AMBIGUOUS
    assert {c["vehicle_id"] for c in match.candidates} == {"V-10001", "V-10007"}


def test_model_alone_surfaces_candidates():
    match = resolve_vehicle(ParsedVehicle(model="RAV4"), INVENTORY)
    assert match.status is MatchStatus.AMBIGUOUS
    assert {c["vehicle_id"] for c in match.candidates} == {"V-10001", "V-10007"}


# --- trim narrowing -------------------------------------------------------------------


def test_non_matching_trim_surfaces_make_model_candidates():
    """A stated trim that matches nothing should not dead-end into NO_MATCH when the
    make/model do match — show what did match."""
    match = resolve("price the 2022 Toyota RAV4 LIMITED")
    assert match.status is MatchStatus.AMBIGUOUS
    assert {c["vehicle_id"] for c in match.candidates} == {"V-10001", "V-10007"}
