# Valuation Methodology

**Companion to** `docs/product-spec.md` §9.2–§9.4, §11.3, §13.4.
Implements **D5** — external market data primary, internal engine as check.

---

## 1. The rule

§13.4 steps 6–7 ask the skill to "compare valuation sources" and "calculate a normalized
market-supported range" without stating how. That number anchors every downstream figure —
pricing scenarios, headroom, depreciation, net value — so leaving it undefined is precisely
the gap where an LLM would end up selecting a price, which §4.1 exists to prevent.

**The settled rule:**

1. The external reference price and recommended range **anchor** the market-supported range.
2. The internal comparable-based valuation is computed **independently and always**, whenever
   comparables are available. It is never skipped because the external source responded.
3. Disagreement between them is measured, surfaced, and reduces confidence.
4. If the external source is unavailable or stale, the internal valuation takes over with a
   warning.

Step 2 is not optional. Without it, the product has no independent opinion and the external
number is unfalsifiable.

---

## 2. External anchor

From `get_vehicle_market_position` and `get_vehicle_pricing_recommendation`:

```text
external_estimate = recommended_price            when available
                  = market_reference_price       otherwise
external_range    = recommended_range             when available
                  = ± configured_band × external_estimate
```

Provider deal-rating thresholds are used when supplied. Internal thresholds substitute only
when the provider gives none, and the substitution is recorded in audit.

---

## 3. Internal check

### 3.1 Comparable selection

From `get_vehicle_comparables`, include a listing when all hold:

| Filter | Default |
| --- | --- |
| Same make and model | required |
| Model year difference | ≤ 2 |
| Mileage difference | ≤ 25,000 |
| Distance | ≤ `max_radius_miles` (100) |
| Similarity score | ≥ 0.60 |
| Listing age | ≤ `DAILY` freshness threshold |

Excluded comparables are retained with an exclusion reason. The result reports both sets, so a
pricing analyst (§3.2) can audit what was thrown away — the review job the specification gives
that user is impossible if exclusions are invisible.

### 3.2 Normalization to subject-equivalence

```text
adjusted_price = list_price
               + (comp_mileage − subject_mileage) × mileage_rate_per_mile
               + (subject_year − comp_year)       × year_value
               + trim_delta[subject_trim]      − trim_delta[comp_trim]
               + condition_delta[subject_cond] − condition_delta[comp_cond]
```

A comparable with **more** miles than the subject adjusts **upward**, because the subject is
worth more than that listing. Getting this sign backwards is a common and silent error; the
test suite asserts the direction in both directions.

All rates are in `config/assumptions/valuation.yaml` and are prototype assumptions.

### 3.3 Weighted estimate

```text
weight = similarity_score × recency_weight × proximity_weight
```

`recency_weight` and `proximity_weight` decay with listing age and distance. The internal
estimate is the **weighted trimmed median** of adjusted prices, dropping the top and bottom
decile once at least 8 comparables survive.

Trimming matters because §9.3 returns asking prices, not sold prices. A stale, overpriced
listing that never sells stays in the comparable set indefinitely and pulls a naive mean
upward.

### 3.4 Insufficient comparables

Below `min_comparables` (default 5), the internal check is **not computed**. The result
reports the external anchor alone with `LOW_VALUATION_CONFIDENCE`. A weighted median of three
listings is noise presented as a second opinion, which is worse than having no second opinion.

---

## 4. Reconciliation

```text
divergence = |internal_estimate − external_estimate| ÷ external_estimate
```

| Divergence | Warning | Effect |
| --- | --- | --- |
| ≤ 5% | none | Range = external range |
| > 5% | `EXTERNAL_PROVIDER_VARIANCE` (MEDIUM) | Confidence drops one level |
| > 10% | `EXTERNAL_PROVIDER_VARIANCE` (HIGH) | Confidence drops one level; range widens to contain both estimates |

```text
market_value = external_estimate                         # always the anchor
market_supported_range =
    external_range                                        if divergence ≤ 0.10
    [min(external_low, internal), max(external_high, internal)]   otherwise
```

The point estimate never moves toward the internal figure. Divergence widens the range and
lowers confidence; it does not blend. A blended number is one neither source would defend and
neither can explain to a general manager.

### 4.1 External source unavailable

Unavailable, or stale beyond the §21 `DAILY` threshold:

```text
market_value = internal_estimate
warnings    += EXTERNAL_VALUATION_UNAVAILABLE
confidence   = min(confidence, MEDIUM)
```

Both sources missing is a hard stop — no valuation, no recommendation.

---

## 5. Confidence

Deterministic score, 0–100, from five factors in `config/assumptions/valuation.yaml`:

| Factor | Weight | Full credit at |
| --- | --- | --- |
| Comparable count | 25 | ≥ 12 |
| Price dispersion (coefficient of variation) | 25 | ≤ 0.08 |
| Source agreement (1 − divergence) | 25 | ≤ 0.03 |
| Mean comparable distance | 15 | ≤ 25 miles |
| Data freshness | 10 | ≤ 6 hours |

`HIGH` ≥ 75, `MEDIUM` ≥ 50, `LOW` below. Contributing factors are reported individually, not
just the total, so the manager can see *which* input is weak rather than being handed a number.

---

## 6. What this methodology does not establish

Stated plainly because §4.4 requires assumptions to be preserved, and because a valuation
carries more authority than it has earned when its limits are not printed alongside it.

* **Asking prices, not sold prices.** §9.3 returns listings. Asking prices are biased upward
  and the bias is not constant across segments. Trimming dampens it; nothing here removes it.
* **The external anchor is not independently verifiable.** Its methodology is a
  `source_methodology` identifier, not a computation this system can inspect or reproduce.
* **Normalization rates are assumed.** `mileage_rate_per_mile`, `year_value`, and the trim and
  condition deltas are configured constants, not fitted values (`open-questions.md` C2).
* **The internal check shares an input with the anchor.** Both ultimately rest on the same
  provider's view of the local market. Agreement between them is weaker evidence than it
  appears — it is not the agreement of two independent sources.
