"""Deterministic natural-language routing. Phase 4, no model.

The assistant reads a dealer's question and decides which workflow it belongs to and,
for a pricing request, what vehicle it names. **Everything here is rules over strings.**
No LLM, no network, and — the load-bearing property — no computed number: the router
classifies and parses, it never prices, forecasts, or estimates. A `year` or a `mileage`
that appears in the result was typed by the user and is copied through verbatim; nothing
is derived.

Two steps live here:

* `classify_intent` — which of the four dealer workflows the text is about.
* `parse_vehicle` — the vehicle a pricing request names, with per-field confidence and an
  honest record of what was missing or ambiguous. It does not guess a trim, a mileage, or
  an id that the user did not state.

`route` runs both and returns a single `RouteResult`. Resolving the parsed vehicle against
real inventory is the resolver's job, not this module's — a name is not a match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from pricing_agent.workflows.context import WorkflowContext


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"


# Skill ids, matching skills/*/SKILL.md and the registry's SkillId.
SKILL_SINGLE_VEHICLE = "single-vehicle-valuation"
SKILL_PORTFOLIO = "inventory-portfolio-forecast"
SKILL_PROMOTION = "dealer-event-promotion-planner"

WORKFLOW_SKILL: dict[WorkflowContext, str] = {
    WorkflowContext.PRICE_INVENTORY: SKILL_SINGLE_VEHICLE,
    WorkflowContext.ACQUIRE_INVENTORY: SKILL_PORTFOLIO,
    WorkflowContext.MERCHANDISE_INVENTORY: SKILL_PROMOTION,
    # Improve Aging coordinates all three; the router names none as "the" skill because
    # this phase does not execute it.
    WorkflowContext.IMPROVE_AGING_INVENTORY: None,
}


# --- vehicle vocabulary ---------------------------------------------------------------

# Makes the parser recognises. Broader than the lot on purpose: recognising "Tesla" lets
# the resolver return an honest NO_MATCH rather than the parser dropping the make and the
# request looking incomplete.
KNOWN_MAKES: dict[str, str] = {
    "toyota": "Toyota", "honda": "Honda", "ford": "Ford", "nissan": "Nissan",
    "bmw": "BMW", "chevrolet": "Chevrolet", "chevy": "Chevrolet", "subaru": "Subaru",
    "ram": "Ram", "kia": "Kia", "jeep": "Jeep", "tesla": "Tesla", "gmc": "GMC",
    "dodge": "Dodge", "hyundai": "Hyundai", "mazda": "Mazda", "lexus": "Lexus",
    "audi": "Audi", "volkswagen": "Volkswagen", "vw": "Volkswagen", "mercedes": "Mercedes",
}

# Multi-word / punctuation-normalising model forms, applied before token matching so that
# "f 150", "rav 4", and "cr v" collapse to the canonical inventory spelling.
MODEL_NORMALIZATIONS: tuple[tuple[str, str], ...] = (
    (r"\bf[\s-]?150\b", "F-150"),
    (r"\bf[\s-]?250\b", "F-250"),
    (r"\bf[\s-]?350\b", "F-350"),
    (r"\brav[\s-]?4\b", "RAV4"),
    (r"\bcr[\s-]?v\b", "CR-V"),
    (r"\bcx[\s-]?5\b", "CX-5"),
    (r"\bcx[\s-]?9\b", "CX-9"),
    (r"\bhr[\s-]?v\b", "HR-V"),
    (r"\bmodel[\s-]?3\b", "Model 3"),
    (r"\bmodel[\s-]?y\b", "Model Y"),
    (r"\bbolt[\s-]?euv\b", "Bolt EUV"),
    (r"\bwrangler\b", "Wrangler"),
    (r"\btelluride\b", "Telluride"),
    (r"\boutback\b", "Outback"),
    (r"\baccord\b", "Accord"),
    (r"\bcamry\b", "Camry"),
    (r"\baltima\b", "Altima"),
    (r"\b1500\b", "1500"),
    (r"\b540i\b", "540i"),
)

# Trims the parser will accept as an explicit trailing descriptor. Kept as words so a bare
# "XLT" is read as a trim, not a model.
KNOWN_TRIMS: dict[str, str] = {
    "xle": "XLE", "xlt": "XLT", "ex": "EX", "sv": "SV", "lt": "LT", "le": "LE",
    "premium": "PREMIUM", "rebel": "REBEL", "sport": "SPORT", "base": "BASE",
    "se": "SE", "lx": "LX", "limited": "LIMITED", "touring": "TOURING",
    "denali": "DENALI", "sr5": "SR5", "laramie": "LARAMIE",
}


# --- intent signals -------------------------------------------------------------------

AGING_COHORT = re.compile(r"\b(aging|aged|stale|old(?:est|er)?)\b")
COHORT_PLURALITY = re.compile(r"\b(vehicles|units|cars|inventory|which|list|ones|them)\b")

PROMOTION_TERMS = re.compile(
    r"\b(promotion|promo|campaign|clearance|markdown|sale event|discount event|"
    r"event plan|promotion plan)\b"
)
EVENT_WORD = re.compile(r"\bevent\b")
NAMED_EVENTS = re.compile(
    r"\b(july\s*4th|july\s*fourth|independence\s*day|fourth\s*of\s*july|labor\s*day|"
    r"memorial\s*day|black\s*friday|summer\s*clearance|summer\s*sale|year[\s-]?end|"
    r"holiday|president'?s?\s*day)\b"
)

# Inventory-reduction pressure. Distinct from "plan an event": these frame the request as
# getting aged units *off* a too-full lot, which is the Improve Aging orchestration — even
# when an event is named as the vehicle for doing it.
INVENTORY_PRESSURE = re.compile(
    r"\b(lot is full|lot'?s full|too many|overstock\w*|over[\s-]?capacit\w+|"
    r"reduce (my |the )?inventory|reduce (my |the )?(inventory )?utilization|"
    r"bring (my |the )?(inventory|utilization) down|cut (my |the )?inventory|"
    r"reprice or promote|which vehicles should i (reprice|promote|move)|"
    # Freeing lot space is an Improve-Aging job (clear aged units), even when the dealer frames it
    # as a precursor to acquiring — "free up space before I acquire" must not read as a portfolio
    # follow-up.
    r"free up space|free up room|make room|clear (out )?(the )?(aged|old|space)|"
    r"plan to free)\b"
)

FORECAST_HORIZON = re.compile(r"\bnext\s+\d+\s+(day|days|week|weeks|month|months)\b")
PORTFOLIO_TERMS = re.compile(
    r"\b(inventory look|look like|forecast|capacity|open slots?|portfolio|acquire|"
    r"acquisition|buy more|how many|run[\s-]?off|30 days|90 days|thirty days|ninety days|"
    r"aging pressure|replacement)\b"
)

PRICING_TERMS = re.compile(
    r"\b(price|pricing|re[\s-]?price|revalue|re[\s-]?valuation|worth|value|valuation|"
    r"apprais(?:e|al)|priced|list price|list it|how much.*(sell|list|ask|worth)|"
    r"what.*worth|what should i list)\b"
)


@dataclass(frozen=True)
class ParsedVehicle:
    """A vehicle as named in free text. Not resolved against inventory."""

    vehicle_id: str | None = None
    vin: str | None = None
    year: int | None = None
    make: str | None = None
    model: str | None = None
    trim: str | None = None
    mileage: int | None = None
    field_confidence: dict[str, str] = field(default_factory=dict)
    ambiguous: tuple[str, ...] = ()

    IDENTITY_FIELDS = ("vehicle_id", "vin", "year", "make", "model", "trim", "mileage")

    def present(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in self.IDENTITY_FIELDS
            if getattr(self, name) is not None
        }

    def missing(self) -> tuple[str, ...]:
        return tuple(
            name for name in self.IDENTITY_FIELDS if getattr(self, name) is None
        )

    def has_identity(self) -> bool:
        """Enough to attempt resolution: an id, a VIN, or at least a make or a model.

        A make alone ("Toyota") or a model alone ("RAV4") is enough to narrow the lot — the
        resolver returns the matching vehicles as candidates for the dealer to choose between,
        rather than forcing a full year/make/model/trim description up front."""
        if self.vehicle_id or self.vin:
            return True
        return bool(self.make or self.model)


@dataclass(frozen=True)
class RouteResult:
    selected_workflow: WorkflowContext | None
    required_skill: str | None
    confidence: Confidence
    reason_codes: tuple[str, ...]
    extracted_entities: dict[str, object]
    missing_fields: tuple[str, ...]
    ambiguous_fields: tuple[str, ...]
    execution_allowed: bool
    parsed_vehicle: ParsedVehicle | None = None


# --- vehicle parsing ------------------------------------------------------------------

_VEHICLE_ID = re.compile(r"\bV-?(\d{4,6})\b", re.IGNORECASE)
_VIN = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")
_YEAR = re.compile(r"\b(19[89]\d|20[0-3]\d)\b")
_MILEAGE = re.compile(
    r"\b(\d{1,3}(?:,\d{3})|\d{2,6})\s*(?:k\b|thousand\b|miles\b|mi\b|mileage\b)",
    re.IGNORECASE,
)
_MILEAGE_K = re.compile(r"\b(\d{1,3})\s*k\b", re.IGNORECASE)


def parse_vehicle(text: str) -> ParsedVehicle:
    """Pull vehicle identity out of free text. Records confidence; never guesses."""
    confidence: dict[str, str] = {}
    ambiguous: list[str] = []

    vehicle_id = None
    if match := _VEHICLE_ID.search(text):
        vehicle_id = f"V-{match.group(1)}"
        confidence["vehicle_id"] = Confidence.HIGH.value

    vin = None
    if match := _VIN.search(text.upper()):
        # A 17-char token that is all digits is far more likely a phone or order number
        # than a VIN, and a real VIN is never all digits.
        candidate = match.group(1)
        if not candidate.isdigit():
            vin = candidate
            confidence["vin"] = Confidence.HIGH.value

    year = None
    if match := _YEAR.search(text):
        year = int(match.group(1))
        confidence["year"] = Confidence.HIGH.value

    lowered = text.lower()

    make = None
    for token, canonical in KNOWN_MAKES.items():
        if re.search(rf"\b{re.escape(token)}\b", lowered):
            if make is not None and canonical != make:
                ambiguous.append("make")
            else:
                make = canonical
                confidence["make"] = Confidence.HIGH.value

    model = None
    model_matches: list[str] = []
    for pattern, canonical in MODEL_NORMALIZATIONS:
        if re.search(pattern, lowered):
            model_matches.append(canonical)
    # De-duplicate while preserving order.
    seen: list[str] = []
    for candidate in model_matches:
        if candidate not in seen:
            seen.append(candidate)
    if len(seen) == 1:
        model = seen[0]
        confidence["model"] = Confidence.HIGH.value
    elif len(seen) > 1:
        model = seen[0]
        confidence["model"] = Confidence.LOW.value
        ambiguous.append("model")

    trim = None
    trim_matches: list[str] = []
    for token, canonical in KNOWN_TRIMS.items():
        if re.search(rf"\b{re.escape(token)}\b", lowered):
            trim_matches.append(canonical)
    if len(trim_matches) == 1:
        trim = trim_matches[0]
        confidence["trim"] = Confidence.HIGH.value
    elif len(trim_matches) > 1:
        # Do not silently pick one. Record the ambiguity; resolution can still narrow it.
        ambiguous.append("trim")

    mileage = None
    if match := _MILEAGE.search(text):
        raw = match.group(1).replace(",", "")
        value = int(raw)
        # "42k" means 42,000; a bare "42000 miles" is already absolute.
        if _MILEAGE_K.search(match.group(0)) or "thousand" in match.group(0).lower():
            value *= 1000
        mileage = value
        confidence["mileage"] = Confidence.HIGH.value

    return ParsedVehicle(
        vehicle_id=vehicle_id,
        vin=vin,
        year=year,
        make=make,
        model=model,
        trim=trim,
        mileage=mileage,
        field_confidence=confidence,
        ambiguous=tuple(dict.fromkeys(ambiguous)),
    )


# --- intent classification ------------------------------------------------------------


def classify_intent(
    text: str, parsed: ParsedVehicle
) -> tuple[WorkflowContext | None, Confidence, tuple[str, ...]]:
    """Choose a workflow by precedence, most specific intent first.

    Order matters where signals overlap: "which aging vehicles should I promote?" carries
    both an aging cue and the word "promote", and it is an aging-cohort question, so the
    aging cohort is tested before promotion.
    """
    lowered = text.lower()
    reasons: list[str] = []

    aging = bool(AGING_COHORT.search(lowered))
    cohort = bool(COHORT_PLURALITY.search(lowered))
    pressure = bool(INVENTORY_PRESSURE.search(lowered))
    if (aging and cohort) or pressure:
        if aging and cohort:
            reasons.append("AGING_COHORT_TERM")
        if pressure:
            reasons.append("INVENTORY_PRESSURE_TERM")
        return WorkflowContext.IMPROVE_AGING_INVENTORY, Confidence.HIGH, tuple(reasons)

    promotion = bool(PROMOTION_TERMS.search(lowered))
    named_event = bool(NAMED_EVENTS.search(lowered))
    event_word = bool(EVENT_WORD.search(lowered))
    if promotion or named_event or (event_word and ("plan" in lowered or "utilization" in lowered)):
        if promotion:
            reasons.append("PROMOTION_TERM")
        if named_event:
            reasons.append("NAMED_EVENT")
        if event_word:
            reasons.append("EVENT_TERM")
        confidence = Confidence.HIGH if (promotion or named_event) else Confidence.MEDIUM
        return WorkflowContext.MERCHANDISE_INVENTORY, confidence, tuple(reasons)

    forecast = bool(FORECAST_HORIZON.search(lowered))
    portfolio = bool(PORTFOLIO_TERMS.search(lowered))
    if forecast or portfolio:
        if forecast:
            reasons.append("FORECAST_HORIZON")
        if portfolio:
            reasons.append("CAPACITY_TERM")
        return WorkflowContext.ACQUIRE_INVENTORY, Confidence.HIGH, tuple(reasons)

    pricing_verb = bool(PRICING_TERMS.search(lowered))
    descriptor = parsed.has_identity() or parsed.year is not None or parsed.make is not None
    if pricing_verb or descriptor:
        if pricing_verb:
            reasons.append("PRICING_VERB")
        if descriptor:
            reasons.append("VEHICLE_DESCRIPTOR")
        # A pricing verb is an explicit ask; a bare descriptor with no verb is inferred.
        confidence = Confidence.HIGH if pricing_verb else Confidence.MEDIUM
        return WorkflowContext.PRICE_INVENTORY, confidence, tuple(reasons)

    reasons.append("NO_INTENT_SIGNAL")
    return None, Confidence.NONE, tuple(reasons)


def route(text: str) -> RouteResult:
    """Classify intent and, for a pricing request, parse the vehicle it names."""
    parsed = parse_vehicle(text)
    workflow, confidence, reasons = classify_intent(text, parsed)

    if workflow is None:
        return RouteResult(
            selected_workflow=None,
            required_skill=None,
            confidence=confidence,
            reason_codes=reasons,
            extracted_entities={},
            missing_fields=(),
            ambiguous_fields=(),
            execution_allowed=False,
            parsed_vehicle=None,
        )

    required_skill = WORKFLOW_SKILL.get(workflow)

    if workflow is WorkflowContext.PRICE_INVENTORY:
        extracted = parsed.present()
        # Execution may proceed to resolution only once there is something to resolve.
        execution_allowed = parsed.has_identity()
        return RouteResult(
            selected_workflow=workflow,
            required_skill=required_skill,
            confidence=confidence,
            reason_codes=reasons,
            extracted_entities=extracted,
            missing_fields=parsed.missing(),
            ambiguous_fields=parsed.ambiguous,
            execution_allowed=execution_allowed,
            parsed_vehicle=parsed,
        )

    if workflow is WorkflowContext.IMPROVE_AGING_INVENTORY:
        # Orchestration over all three skills; no single required_skill. The workflow can
        # always at least diagnose the portfolio, so execution is allowed and the assistant
        # extracts the target/event inputs.
        return RouteResult(
            selected_workflow=workflow,
            required_skill=None,
            confidence=confidence,
            reason_codes=reasons,
            extracted_entities={},
            missing_fields=(),
            ambiguous_fields=(),
            execution_allowed=True,
            parsed_vehicle=None,
        )

    # ACQUIRE needs no entity; MERCHANDISE's event context is resolved by the orchestrator
    # against the calendar, so the router allows it to proceed and lets resolution decide.
    return RouteResult(
        selected_workflow=workflow,
        required_skill=required_skill,
        confidence=confidence,
        reason_codes=reasons,
        extracted_entities={},
        missing_fields=(),
        ambiguous_fields=(),
        execution_allowed=True,
        parsed_vehicle=None,
    )
