"""Valuation. Implements D5 — external market data primary, internal comparable engine as check.

docs/valuation-methodology.md. The rule in one line: the external source anchors the
number, the internal estimate always runs beside it, and disagreement widens the range
and lowers confidence but **never moves the point estimate**.

A blended number is one neither source would defend and neither can explain to a general
manager, which is why reconciliation does not average.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from pricing_agent.config import Config


@dataclass(frozen=True)
class Comparable:
    listing_id: str
    year: int
    make: str
    model: str
    trim: str | None
    mileage: int
    condition: str
    list_price: float
    distance_miles: float
    days_on_market: int
    similarity_score: float
    adjusted_price: float | None = None
    adjustments: dict = field(default_factory=dict)
    weight: float | None = None
    included: bool = True
    exclusion_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "listing_id": self.listing_id,
            "year": self.year,
            "make": self.make,
            "model": self.model,
            "trim": self.trim,
            "mileage": self.mileage,
            "condition": self.condition,
            "list_price": self.list_price,
            "adjusted_price": self.adjusted_price,
            "adjustments": self.adjustments,
            "distance_miles": self.distance_miles,
            "days_on_market": self.days_on_market,
            "similarity_score": self.similarity_score,
            "weight": self.weight,
            "included": self.included,
            "exclusion_reason": self.exclusion_reason,
        }


def normalize_comparables(
    subject_year: int,
    subject_mileage: int,
    subject_trim: str | None,
    subject_condition: str,
    payloads: list[dict],
    config: Config,
) -> list[Comparable]:
    """Select, exclude, and normalize comparables to subject-equivalence.

    Sign convention worth stating explicitly: a comparable with MORE miles than the
    subject adjusts UPWARD, because the subject is worth more than that listing. Getting
    this backwards is silent and plausible-looking, so it is asserted in the tests.
    """
    selection = config.valuation["comparable_selection"]
    norm = config.valuation["normalization"]
    weighting = config.valuation["weighting"]

    trim_delta = norm["trim_delta"]
    condition_delta = norm["condition_delta"]
    subject_trim_key = (subject_trim or "UNKNOWN").upper()

    results: list[Comparable] = []
    for payload in payloads:
        comp_trim = (payload.get("trim") or "UNKNOWN").upper()
        comp_condition = payload.get("condition") or "UNKNOWN"

        reason = _exclusion_reason(
            payload, subject_year, subject_mileage, selection
        )
        if reason is not None:
            results.append(
                Comparable(
                    listing_id=payload["listing_id"],
                    year=payload["year"],
                    make=payload["make"],
                    model=payload["model"],
                    trim=payload.get("trim"),
                    mileage=payload["mileage"],
                    condition=comp_condition,
                    list_price=float(payload["list_price"]),
                    distance_miles=float(payload.get("distance_miles", 0.0)),
                    days_on_market=int(payload.get("days_on_market", 0)),
                    similarity_score=float(payload.get("similarity_score", 0.0)),
                    included=False,
                    exclusion_reason=reason,
                )
            )
            continue

        mileage_adj = (int(payload["mileage"]) - subject_mileage) * float(
            norm["mileage_rate_per_mile"]
        )
        year_adj = (subject_year - int(payload["year"])) * float(norm["year_value"])
        trim_adj = float(trim_delta.get(subject_trim_key, 0)) - float(
            trim_delta.get(comp_trim, 0)
        )
        condition_adj = float(condition_delta.get(subject_condition, 0)) - float(
            condition_delta.get(comp_condition, 0)
        )

        adjusted = float(payload["list_price"]) + mileage_adj + year_adj + trim_adj + condition_adj

        similarity = float(payload.get("similarity_score", 0.0))
        recency = 0.5 ** (
            float(payload.get("days_on_market", 0))
            / float(weighting["recency_half_life_days"])
        )
        proximity = 0.5 ** (
            float(payload.get("distance_miles", 0.0))
            / float(weighting["proximity_half_life_miles"])
        )

        results.append(
            Comparable(
                listing_id=payload["listing_id"],
                year=payload["year"],
                make=payload["make"],
                model=payload["model"],
                trim=payload.get("trim"),
                mileage=payload["mileage"],
                condition=comp_condition,
                list_price=float(payload["list_price"]),
                distance_miles=float(payload.get("distance_miles", 0.0)),
                days_on_market=int(payload.get("days_on_market", 0)),
                similarity_score=similarity,
                adjusted_price=adjusted,
                adjustments={
                    "mileage_adj": mileage_adj,
                    "year_adj": year_adj,
                    "trim_adj": trim_adj,
                    "condition_adj": condition_adj,
                },
                weight=similarity * recency * proximity,
                included=True,
            )
        )
    return results


def _exclusion_reason(payload: dict, year: int, mileage: int, selection: dict) -> str | None:
    if abs(int(payload["year"]) - year) > int(selection["max_year_difference"]):
        return "YEAR_OUT_OF_RANGE"
    if abs(int(payload["mileage"]) - mileage) > int(selection["max_mileage_difference"]):
        return "MILEAGE_OUT_OF_RANGE"
    if float(payload.get("distance_miles", 0.0)) > float(selection["max_radius_miles"]):
        return "OUT_OF_RADIUS"
    if float(payload.get("similarity_score", 0.0)) < float(selection["min_similarity_score"]):
        return "LOW_SIMILARITY"
    return None


def internal_estimate(comparables: list[Comparable], config: Config) -> float | None:
    """Weighted, decile-trimmed median of adjusted prices.

    Returns None below `min_comparables`. §9.3 supplies asking prices, not sold prices,
    so a stale overpriced listing stays in the set indefinitely and drags a naive mean
    upward — trimming is what keeps the check honest. Below the minimum the check is not
    computed at all: a weighted median of three listings is noise presented as a second
    opinion, which is worse than having no second opinion.
    """
    selection = config.valuation["comparable_selection"]
    included = [c for c in comparables if c.included and c.adjusted_price is not None]

    if len(included) < int(selection["min_comparables"]):
        return None

    included.sort(key=lambda c: c.adjusted_price)  # type: ignore[arg-type]

    if len(included) >= int(selection["trim_outliers_at_count"]):
        cut = max(1, int(len(included) * float(selection["trim_fraction"])))
        included = included[cut : len(included) - cut]

    prices = np.array([c.adjusted_price for c in included], dtype=float)
    weights = np.array([c.weight or 0.0 for c in included], dtype=float)
    if weights.sum() <= 0:
        return float(np.median(prices))

    cumulative = np.cumsum(weights) / weights.sum()
    index = int(np.searchsorted(cumulative, 0.5))
    return float(prices[min(index, len(prices) - 1)])


@dataclass(frozen=True)
class Valuation:
    market_value: float
    range_low: float
    range_high: float
    external_estimate: float | None
    external_methodology: str | None
    internal_estimate: float | None
    divergence: float | None
    anchor: str
    confidence: dict
    warnings: list[str]

    def as_dict(self) -> dict:
        return {
            "market_value": self.market_value,
            "market_supported_range": {"low": self.range_low, "high": self.range_high},
            "external_estimate": self.external_estimate,
            "external_source_methodology": self.external_methodology,
            "internal_estimate": self.internal_estimate,
            "divergence": self.divergence,
            "anchor": self.anchor,
            "confidence": self.confidence,
        }


def reconcile(
    external_estimate: float | None,
    external_range: tuple[float, float] | None,
    external_methodology: str | None,
    internal: float | None,
    comparables: list[Comparable],
    config: Config,
    data_age_hours: float = 0.0,
    external_stale: bool = False,
) -> Valuation:
    """Combine the two sources per D5."""
    rules = config.valuation["reconciliation"]
    warnings: list[str] = []

    external_usable = external_estimate is not None and not external_stale

    if not external_usable:
        # Branch 4: fall back to the internal estimate, with a warning.
        if internal is None:
            raise ValueError("No valuation available from either source")
        warnings.append("EXTERNAL_VALUATION_UNAVAILABLE")
        band = float(rules["fallback_band_pct"])
        confidence = score_confidence(comparables, None, data_age_hours, config, cap="MEDIUM")
        return Valuation(
            market_value=internal,
            range_low=internal * (1 - band),
            range_high=internal * (1 + band),
            external_estimate=external_estimate,
            external_methodology=external_methodology,
            internal_estimate=internal,
            divergence=None,
            anchor="INTERNAL_FALLBACK",
            confidence=confidence,
            warnings=warnings,
        )

    assert external_estimate is not None
    if external_range is not None:
        low, high = float(external_range[0]), float(external_range[1])
    else:
        band = float(rules["fallback_band_pct"])
        low, high = external_estimate * (1 - band), external_estimate * (1 + band)

    divergence: float | None = None
    cap: str | None = None
    if internal is not None and external_estimate:
        divergence = abs(internal - external_estimate) / external_estimate
        if divergence > float(rules["variance_threshold_high"]):
            warnings.append("EXTERNAL_PROVIDER_VARIANCE")
            cap = "MEDIUM"
        elif divergence > float(rules["variance_threshold_medium"]):
            warnings.append("EXTERNAL_PROVIDER_VARIANCE")
            cap = "MEDIUM"

        if divergence > float(rules["widen_range_above"]):
            low, high = min(low, internal), max(high, internal)

    if internal is None:
        warnings.append("LOW_VALUATION_CONFIDENCE")

    confidence = score_confidence(comparables, divergence, data_age_hours, config, cap=cap)

    return Valuation(
        market_value=float(external_estimate),  # the anchor never moves
        range_low=float(low),
        range_high=float(high),
        external_estimate=float(external_estimate),
        external_methodology=external_methodology,
        internal_estimate=internal,
        divergence=divergence,
        anchor="EXTERNAL",
        confidence=confidence,
        warnings=warnings,
    )


_LEVEL_ORDER = ["LOW", "MEDIUM", "HIGH"]


def score_confidence(
    comparables: list[Comparable],
    divergence: float | None,
    data_age_hours: float,
    config: Config,
    cap: str | None = None,
) -> dict:
    """Deterministic 0-100 score. Factors are reported individually so the user can see
    which input is weak rather than being handed a bare number."""
    settings = config.valuation["confidence"]
    weights = settings["weights"]
    full = settings["full_credit"]

    included = [c for c in comparables if c.included and c.adjusted_price is not None]
    factors: list[dict] = []
    total = 0.0

    def credit(name: str, ratio: float, note: str) -> None:
        nonlocal total
        weight = float(weights[name])
        earned = weight * float(np.clip(ratio, 0.0, 1.0))
        total += earned
        factors.append({"name": name, "contribution": round(earned, 2), "note": note})

    count = len(included)
    credit(
        "comparable_count",
        count / float(full["comparable_count"]),
        f"{count} usable comparables",
    )

    if count >= 2:
        prices = np.array([c.adjusted_price for c in included], dtype=float)
        cv = float(prices.std() / prices.mean()) if prices.mean() else 1.0
        credit(
            "price_dispersion",
            float(full["price_dispersion_cv"]) / cv if cv > 0 else 1.0,
            f"coefficient of variation {cv:.3f}",
        )
        distance = float(np.mean([c.distance_miles for c in included]))
        credit(
            "mean_distance",
            float(full["mean_distance_miles"]) / distance if distance > 0 else 1.0,
            f"mean distance {distance:.0f} miles",
        )
    else:
        factors.append(
            {"name": "price_dispersion", "contribution": 0.0, "note": "too few comparables"}
        )
        factors.append(
            {"name": "mean_distance", "contribution": 0.0, "note": "too few comparables"}
        )

    if divergence is None:
        factors.append(
            {
                "name": "source_agreement",
                "contribution": 0.0,
                "note": "no independent internal estimate to compare",
            }
        )
    else:
        credit(
            "source_agreement",
            float(full["source_divergence"]) / divergence if divergence > 0 else 1.0,
            f"sources differ by {divergence:.1%}",
        )

    credit(
        "data_freshness",
        float(full["data_age_hours"]) / data_age_hours if data_age_hours > 0 else 1.0,
        f"market data {data_age_hours:.0f}h old",
    )

    thresholds = settings["level_thresholds"]
    if total >= float(thresholds["HIGH"]):
        level = "HIGH"
    elif total >= float(thresholds["MEDIUM"]):
        level = "MEDIUM"
    else:
        level = "LOW"

    if cap is not None and _LEVEL_ORDER.index(level) > _LEVEL_ORDER.index(cap):
        level = cap

    return {"level": level, "score": round(total, 1), "factors": factors}
