# MCP Tool Contracts

**Companion to** `docs/product-spec.md` §8–§10.
Decisions referenced as **D1**–**D9** are recorded in `docs/open-questions.md`.

---

## 0. Status of this contract

Per §8.1, this is a **proposed** integration surface. It is shaped to what the specification
needs, not to an observed provider API. Nothing here should be read as a claim that these
endpoints exist in this form.

Plausibility varies by tool, and that variance is itself a design input:

| Confidence that an authorized equivalent exists | Tools |
| --- | --- |
| High | `get_dealer_inventory`, `get_vehicle_price_history`, `get_vehicle_inventory_age` |
| Moderate | `get_vehicle_market_position`, `get_vehicle_comparables`, `get_vehicle_pricing_recommendation`, `get_market_sales_velocity`, `get_dealer_sales_history` |
| Low | `get_shopper_engagement` |

The degradation model in `docs/architecture.md` §9 is written so that the low-confidence tool
is never load-bearing. Contract drift is the largest integration risk in this design
(`open-questions.md` C5).

All prototype implementations live in `mocks/` and are deterministic.

---

## 1. Common envelope

Every response carries provenance. The freshness policy (§21) and the audit record (§23)
both depend on it, so it is mandatory rather than conventional.

```json
{
  "data": { },
  "meta": {
    "source": "MARKET_DATA | INTERNAL_COST | INTERNAL_CAPACITY | INTERNAL_EVENT | CONFIG",
    "data_timestamp": "2026-07-29T09:15:00Z",
    "source_version": "market-data-mcp-0.3.0",
    "confidence": "HIGH | MEDIUM | LOW | UNKNOWN",
    "coverage": 0.94,
    "request_id": "req_01J..."
  }
}
```

`coverage` is the fraction of requested entities actually returned. Portfolio calls routinely
return partial data; `data_coverage` in the portfolio result (§14.9) aggregates this field
rather than assuming completeness.

### Freshness classes (§21)

| Class | Max age | Tools | Stale behavior |
| --- | --- | --- | --- |
| `REALTIME` | 1 h | `get_dealer_inventory`, price fields | Analysis proceeds, **publication barred** |
| `NEAR_REALTIME` | 4 h | `get_dealer_capacity` | Warning; portfolio confidence reduced |
| `DAILY` | 24 h | market position, comparables, velocity, engagement, cost basis, events | Warning; valuation confidence reduced one level |
| `VERSIONED` | n/a | `get_dealer_sales_history` | Model-version controlled |

Every client takes an injected `as_of` timestamp (D8). No freshness check reads the wall clock.

### Error model

| Condition | Response | Caller behavior |
| --- | --- | --- |
| `NOT_FOUND` | empty `data`, `coverage: 0` | Degrade per architecture §9 |
| `UNAUTHORIZED` | error, no partial data | Hard stop; surfaced to user |
| `UNAVAILABLE` | error | Degrade per architecture §9 |
| `PARTIAL` | `data` present, `coverage < 1.0` | Proceed, emit coverage warning |

No tool returns a silent default. A missing number is absent, never zero — a zero acquisition
cost would produce a floor of zero and defeat §4.5.

---

## 2. Market-data MCP tools

### 2.1 `get_dealer_inventory`

Freshness `REALTIME`. Read-only.

**Input**

```json
{ "dealer_id": "DEALER-1001", "status": ["ACTIVE"], "include_pending": false }
```

**Output** — as §9.1, per vehicle:

| Field | Type | Notes |
| --- | --- | --- |
| `vehicle_id` | string | primary key across all tools |
| `vin` | string | 17 characters, validated |
| `year` `make` `model` `trim` | | |
| `mileage` | integer | |
| `current_list_price` | number \| null | null = not yet priced |
| `days_in_inventory` | integer | |
| `status` | enum | `ACTIVE \| PENDING \| SOLD \| WHOLESALE` |
| `segment` | string | required by depreciation and event-lift assumptions |
| `powertrain` | enum | `ICE \| HYBRID \| PHEV \| BEV` — required by depreciation (§18) |
| `image_url` | string \| null | Primary merchandising photo |

`segment` and `powertrain` are additions to §9.1. Both are required inputs to the depreciation
model, and neither is derivable from the fields §9.1 lists.

`image_url` is a third addition, for display only — no calculation consumes it. It is null
for every vehicle in `mocks/`, because this prototype has no image source and real
manufacturer photography in a customer demo would raise licensing questions the prototype
should not quietly take on. The UI renders a generated body-style silhouette instead, so
the empty state looks deliberate rather than broken; populating the field in a real
integration switches to the photograph with no code change.

---

### 2.2 `get_vehicle_market_position`

Freshness `DAILY`. Supplies the anchor for D5.

| Field | Type |
| --- | --- |
| `market_reference_price` | number |
| `price_to_market_ratio` | number |
| `market_percentile` | number, 0–100 |
| `deal_rating` | enum `GREAT \| GOOD \| FAIR \| HIGH \| NO_RATING` |
| `good_deal_threshold` `fair_deal_threshold` `poor_deal_threshold` | number, price-to-market ratios |
| `comparable_count` | integer |
| `effective_date` | date |

`comparable_count` is an addition: the confidence rule needs to know how much support the
reference price has, and §9.2 does not supply it.

Deal-rating thresholds are consumed from this response when present. Internal thresholds
(`config/assumptions/pricing.yaml`) are used only when the provider supplies none, and that
substitution is recorded in audit.

---

### 2.3 `get_vehicle_comparables`

Freshness `DAILY`.

Per comparable: `listing_id`, `year`, `make`, `model`, `trim`, `mileage`, `condition` (nullable),
`list_price`, `distance_miles`, `days_on_market`, `similarity_score` (0–1), `seller_type`,
`data_timestamp`.

Consumed by the **internal** valuation that checks the external anchor (D5 step 2). Comparable
normalization — mileage, year, trim, condition adjustment to subject-equivalence — happens in
`domain/valuation.py`, never in the client.

Below `min_comparables` (config, default 5) the internal check is not computed; the result
reports the external anchor alone with `LOW_VALUATION_CONFIDENCE`.

---

### 2.4 `get_vehicle_pricing_recommendation`

Freshness `DAILY`.

`recommended_price`, `recommended_range: {low, high}`, `source_methodology`, `market_position`,
`confidence`, `effective_date`, `service_version`.

Per §9.4 and D5 this anchors the range but is not accepted uncritically: the internal
comparable-based valuation runs alongside it and disagreement beyond the configured thresholds
raises `EXTERNAL_PROVIDER_VARIANCE`.

---

### 2.5 `get_vehicle_price_history`

Freshness `DAILY`. `original_list_price`, `current_list_price`, `price_changes[] {date, from, to, reason}`,
`cumulative_markdown`, `campaign_participation[]`.

Feeds `used headroom` (§13.9) and the §15.6 exclusion of vehicles already assigned to a campaign.

---

### 2.6 `get_vehicle_inventory_age`

Freshness `REALTIME`. `days_in_inventory`, `acquisition_date`, `merchandising_start_date`,
`aging_bucket` (§14.4 buckets), `aging_status`.

`days_in_inventory` is measured from acquisition, not merchandising start. Where the two
differ the difference is reported, because holding cost accrues from acquisition while market
exposure begins at merchandising.

---

### 2.7 `get_market_sales_velocity`

Freshness `DAILY`. Supplies the base hazard for the simulation (§16.2).

`avg_days_to_sale`, `median_days_to_sale`, `sales_volume`, `active_supply`,
`supply_to_sales_ratio`, `seasonal_indicators`, `confidence`, `market_radius_miles`,
`segment`.

Scoped to segment and radius, not to a single vehicle. The vehicle-specific adjustment —
price-to-market, mileage, condition, engagement — is applied in `simulation/`, using
elasticities from config that are **assumed, not calibrated** (`open-questions.md` C3).

---

### 2.8 `get_shopper_engagement`

Freshness `DAILY`. **Optional** — the least likely tool to exist in this form.

`listing_views`, `vdp_views`, `saved_vehicles`, `leads`, `calls`, `appointments`,
`observation_window_days`.

Absent, the hazard model drops its engagement term and confidence is reduced (§9.8). No
downstream calculation may require this tool. Note that engagement is partly an *effect* of
price rather than a cause of sale; the prototype applies it as a bounded multiplier and does
not attempt to separate the two.

---

### 2.9 `get_dealer_sales_history`

Freshness `VERSIONED`.

Per sale: `vehicle_id`, `transaction_price`, **`list_price_at_sale`**, `days_to_sale`,
`front_end_gross`, `segment`, `event_participation`, `sale_date`, `promoted` (boolean).

`list_price_at_sale` is the §9.9 addition from **D4**. Without it, `expected_discount_rate`
cannot be calibrated from history and the minimum-safe-list-price derivation rests entirely on
a configured constant. The MVP still uses the constant; the field exists so a real integration
can replace it.

`promoted` versus baseline is what makes event lift measurable at all (§26.3).

---

## 3. Internal MCP tools

Dealer systems, not the market-data provider.

### 3.1 `get_vehicle_cost_basis`

Freshness `DAILY`. **Hard dependency** — see architecture §9.

`acquisition_cost`, `auction_fee`, `transportation_cost`, `reconditioning_cost`,
`accrued_holding_cost`, `financing_amount`, `direct_selling_costs`.

`accrued_holding_cost` is cash cost already incurred (D3). It enters current accounting
break-even. Slot opportunity cost is never included here — it is imputed and is computed in
`domain/holding_cost.py`.

---

### 3.2 `get_dealer_capacity`

Freshness `NEAR_REALTIME`.

| Field | Definition |
| --- | --- |
| `total_physical_slots` | Lot capacity. **The basis for utilization targets (D6).** |
| `current_inventory` | Units physically on the lot |
| `reserved_slots` | Slots committed and unavailable |
| `confirmed_inbound` | Units purchased and en route |
| `expected_exits` | Units expected to leave for non-retail reasons |
| `target_utilization` | Dealer policy, 0–1 |

**Relationship, defined here because §10.2 leaves it open (D6):**

```text
reserved_slots ⊇ confirmed_inbound
reserved_not_inbound = reserved_slots − confirmed_inbound
```

Reserved slots include those held for confirmed inbound units. The §15.4 flow equation uses
`confirmed_inbound`; `reserved_not_inbound` is deducted separately. Counting both
`reserved_slots` and `confirmed_inbound` would double-count every inbound vehicle and inflate
the promotional unit target — the specific defect D6 exists to prevent.

Derived fields (`physical_open_slots`, `effective_open_slots`, `current_utilization`,
`projected_utilization`) are computed in `domain/portfolio.py` from the primitives above, not
returned by the tool. A returned derived field that disagrees with the primitives would be
unresolvable.

---

### 3.3 `get_sales_event_calendar`

Freshness `DAILY`.

`event_id`, `event_name`, `start_date`, `end_date`, `event_type`, `eligible_inventory_filter`,
`promotion_budget`, `dealer_funded_incentives`, `partner_funded_incentives`,
`historical_demand_lift` (nullable), `lift_confidence`, `lift_basis` (count of prior
comparable events).

`historical_demand_lift` is nullable by design: §26.3 requires both an event with validated
lift and one without. Null lift forces the configured default and raises
`LOW_EXPECTED_EVENT_LIFT`.

---

### 3.4 `get_inbound_inventory`

Freshness `NEAR_REALTIME`. `inbound_count`, per unit `{expected_arrival_date, segment,
committed_slot, acquisition_status}`.

Only units with `committed_slot: true` are counted as `confirmed_inbound` in §3.2, keeping the
two tools consistent.

---

### 3.5 `get_dealer_pricing_policy` — **proposed addition**

Freshness `DAILY`. Not in §10.

`policy_price_floor_rule`, `minimum_gross_policy`, `risk_floor_pct`,
`approval_thresholds: {aggressive_adjustment_pct, material_gross_reduction_pct}`,
`excluded_from_promotion[]`.

§11.9 and §11.10 make policy floor and configured risk floor two of the four inputs to minimum
safe transaction price — the central financial-safety control — and no tool in §10 returns
them (`open-questions.md` C1). The prototype reads these from
`config/assumptions/pricing.yaml`; the tool is specified so a real deployment has somewhere to
put dealer policy that is not a config file in the application repository.

The same tool supplies the magnitudes §22.2 leaves undefined ("unusually aggressive",
"material gross reduction").

---

## 4. Write tools

Physically separated from every read path (architecture §8).

### 4.1 `request_manager_approval`

Input: `pricing_decision_id`, `approval_type`, `justification`, quantified impact
(`immediate_loss`, `expected_future_loss`, `expected_holding_cost`, `expected_depreciation`,
`capacity_opportunity_cost`), `requesting_user`.

Returns `approval_id` and `status: PENDING | APPROVED | DENIED`. Never auto-approves.

### 4.2 `save_pricing_decision`

Persists the §23 audit record: system recommendation, user-selected price, override reason,
user, approval reference, timestamps, and every data, model, config, and assumption version.

Returns `pricing_decision_id`. Requires explicit user confirmation (§22.3).

### 4.3 `publish_vehicle_price`

The only tool that changes a public-facing price. **Never called automatically** (§10.7).

Requires all of: `pricing_decision_id`, `final_price`, `confirmed_by_user`,
`approval_id` where §22.2 applies, and `idempotency_key`.

Preconditions verified server-side, not merely by the caller:

1. No unresolved `BLOCKING` warning
2. Approval satisfied where required
3. No `REALTIME`-class data stale per §21
4. `final_price` equals the price in the referenced decision

`idempotency_key = hash(pricing_decision_id + final_price)`. A retry cannot double-publish,
and a changed price cannot reuse an existing approval.
