"""Deterministic intent routing and vehicle parsing. Phase 4.

The router is rules over strings: it classifies a request and parses the vehicle it names,
and it must do neither creatively. These tests pin the intent precedence (aging beats
promotion beats forecast beats pricing), the entity extraction and normalization, and the
one property that makes the whole "LLM does not price" claim hold at this layer — the
router produces no computed number.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pricing_agent.agents.router import (
    Confidence,
    ParsedVehicle,
    classify_intent,
    parse_vehicle,
    route,
)
from pricing_agent.workflows.context import WorkflowContext

AGENTS = Path(__file__).resolve().parents[2] / "src" / "pricing_agent" / "agents"


# --- intent classification ------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("What should I price this vehicle?", WorkflowContext.PRICE_INVENTORY),
        ("What should I price 2020 Ford F-150 XLT?", WorkflowContext.PRICE_INVENTORY),
        ("How much is the RAV4 worth?", WorkflowContext.PRICE_INVENTORY),
        ("What will my inventory look like in the next 30 days?", WorkflowContext.ACQUIRE_INVENTORY),
        ("Show me the 90 day forecast", WorkflowContext.ACQUIRE_INVENTORY),
        ("How much open capacity do I have?", WorkflowContext.ACQUIRE_INVENTORY),
        ("Create a July 4th promotion plan.", WorkflowContext.MERCHANDISE_INVENTORY),
        ("Plan the Summer Clearance event to reach 70% utilization.", WorkflowContext.MERCHANDISE_INVENTORY),
        ("Which aging vehicles should I promote?", WorkflowContext.IMPROVE_AGING_INVENTORY),
        ("What should I do with my aged units?", WorkflowContext.IMPROVE_AGING_INVENTORY),
    ],
)
def test_intent_classification(text, expected):
    assert route(text).selected_workflow is expected


def test_pricing_maps_to_the_single_vehicle_skill():
    assert route("price the F-150").required_skill == "single-vehicle-valuation"


def test_portfolio_maps_to_the_forecast_skill():
    result = route("what will my inventory look like in the next 30 days?")
    assert result.required_skill == "inventory-portfolio-forecast"


def test_promotion_maps_to_the_promotion_skill():
    result = route("create a labor day promotion plan")
    assert result.required_skill == "dealer-event-promotion-planner"


def test_aging_is_routed_names_no_single_skill_and_is_now_executable():
    """Phase 5: Improve Aging is an orchestration over all three skills, so it names no
    single required_skill, but it now executes (it can always diagnose the portfolio)."""
    result = route("which aging vehicles should I promote?")
    assert result.selected_workflow is WorkflowContext.IMPROVE_AGING_INVENTORY
    assert result.required_skill is None
    assert result.execution_allowed is True


def test_inventory_pressure_routes_to_improve_aging_even_with_a_named_event():
    """"reduce inventory utilization during Summer Clearance" is an aging-reduction job, not
    a plain event plan — the inventory-pressure framing wins over the event."""
    result = route("reduce my inventory utilization to 70% during the Summer Clearance event")
    assert result.selected_workflow is WorkflowContext.IMPROVE_AGING_INVENTORY
    assert "INVENTORY_PRESSURE_TERM" in result.reason_codes


def test_plain_event_plan_still_routes_to_merchandise():
    """A promotion request without inventory-reduction framing stays MERCHANDISE (Phase 4)."""
    result = route("plan the Summer Clearance event to reach 70% utilization")
    assert result.selected_workflow is WorkflowContext.MERCHANDISE_INVENTORY


def test_aging_cohort_beats_promotion_when_both_words_appear():
    """"promote" alone would look like merchandising; the aging cohort is the real intent."""
    result = route("which aging vehicles should I promote?")
    assert result.selected_workflow is WorkflowContext.IMPROVE_AGING_INVENTORY


def test_named_event_beats_forecast_when_utilization_and_event_both_appear():
    result = route("can I reach 70 percent utilization by the end of the Summer Clearance event?")
    assert result.selected_workflow is WorkflowContext.MERCHANDISE_INVENTORY


def test_unclassifiable_text_selects_no_workflow():
    result = route("hello there")
    assert result.selected_workflow is None
    assert result.confidence is Confidence.NONE
    assert result.execution_allowed is False


def test_reason_codes_are_populated_for_every_route():
    for text in ("price the F-150", "next 30 days", "labor day sale", "aged units", "xyz"):
        assert route(text).reason_codes


# --- vehicle entity extraction --------------------------------------------------------


def test_extracts_full_vehicle_description():
    parsed = parse_vehicle("What should I price 2020 Ford F-150 XLT with 68,000 miles?")
    assert parsed.year == 2020
    assert parsed.make == "Ford"
    assert parsed.model == "F-150"
    assert parsed.trim == "XLT"
    assert parsed.mileage == 68000


def test_extracts_vehicle_id():
    parsed = parse_vehicle("price V-10003 for me")
    assert parsed.vehicle_id == "V-10003"


def test_extracts_vehicle_id_without_dash():
    assert parse_vehicle("look at V10003").vehicle_id == "V-10003"


def test_extracts_vin():
    parsed = parse_vehicle("price VIN 1FTEW1EP5LKD10203 please")
    assert parsed.vin == "1FTEW1EP5LKD10203"


def test_does_not_read_a_17_digit_number_as_a_vin():
    assert parse_vehicle("order 12345678901234567").vin is None


@pytest.mark.parametrize(
    ("text", "model"),
    [
        ("price the F150", "F-150"),
        ("price the f 150", "F-150"),
        ("value my RAV 4", "RAV4"),
        ("value my rav4", "RAV4"),
        ("what's the CRV worth", "CR-V"),
        ("what's the cr-v worth", "CR-V"),
        ("price the Model 3", "Model 3"),
    ],
)
def test_model_name_normalization(text, model):
    assert parse_vehicle(text).model == model


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("42,000 miles", 42000),
        ("42000 miles", 42000),
        ("42k miles", 42000),
        ("68,000 mi", 68000),
    ],
)
def test_mileage_parsing(text, expected):
    assert parse_vehicle(f"price the F-150 with {text}").mileage == expected


def test_missing_fields_are_preserved_not_guessed():
    parsed = parse_vehicle("What should I price this vehicle?")
    assert parsed.make is None and parsed.model is None
    assert parsed.trim is None and parsed.mileage is None
    assert set(parsed.missing()) == set(ParsedVehicle.IDENTITY_FIELDS)


def test_does_not_guess_a_trim():
    parsed = parse_vehicle("price the 2020 Ford F-150")
    assert parsed.trim is None


def test_ambiguous_trim_is_flagged_not_chosen():
    parsed = parse_vehicle("price the Ford F-150 XLT LT")
    assert "trim" in parsed.ambiguous
    assert parsed.trim is None


def test_confidence_recorded_for_extracted_fields():
    parsed = parse_vehicle("2020 Ford F-150 XLT")
    assert parsed.field_confidence["year"] == Confidence.HIGH.value
    assert parsed.field_confidence["make"] == Confidence.HIGH.value


def test_has_identity_requires_id_vin_or_a_make_or_model():
    assert parse_vehicle("2020 Ford F-150").has_identity()
    assert parse_vehicle("V-10003").has_identity()
    assert parse_vehicle("a 2020 Ford").has_identity()      # make alone — enough to list candidates
    assert parse_vehicle("the RAV4").has_identity()          # model alone — enough to list candidates
    assert not parse_vehicle("price this vehicle").has_identity()  # neither make nor model


# --- execution_allowed on the route --------------------------------------------------


def test_pricing_without_a_vehicle_is_not_execution_allowed():
    assert route("what should I price this vehicle?").execution_allowed is False


def test_pricing_with_a_vehicle_is_execution_allowed():
    assert route("what should I price the 2020 Ford F-150 XLT?").execution_allowed is True


def test_portfolio_needs_no_entity_and_is_execution_allowed():
    assert route("what will my inventory look like in the next 30 days?").execution_allowed is True


# --- the router generates no numbers --------------------------------------------------


def test_extracted_entities_are_only_user_stated_values():
    """A number in the result was typed by the user, never computed. Every extracted numeric
    entity must appear verbatim in the input text."""
    text = "price the 2020 Ford F-150 XLT with 68000 miles"
    entities = route(text).extracted_entities
    for key in ("year", "mileage"):
        if key in entities:
            assert str(entities[key]) in text.replace(",", "")


def test_route_result_carries_no_pricing_or_forecast_fields():
    result = route("price the 2020 Ford F-150 XLT")
    banned = {
        "price", "recommended_price", "gross", "days_to_sale", "break_even",
        "probability", "valuation", "forecast",
    }
    assert not (banned & set(result.extracted_entities))


def test_router_module_imports_no_model_or_skill():
    """The router classifies; it must not reach a model, a skill, or the calculation
    layer — a route decision is not a computation."""
    tree = ast.parse((AGENTS / "router.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imported.append(node.module)

    for name in imported:
        assert not name.startswith(
            ("anthropic", "openai", "pricing_agent.llm", "pricing_agent.skills",
             "pricing_agent.domain", "pricing_agent.simulation")
        ), f"router imports {name}"
