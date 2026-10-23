# Portfolio Forecast Methodology

**Companion to** `docs/product-spec.md` §14.
Implements **D2** (draw-level aggregation) and **D6** (capacity basis).

---

## 1. The aggregation rule

§14.7 states it directly: do not add individual P50 forecasts. The reason is that the P50 of a
sum is not the sum of P50s except under independence assumptions that do not hold here —
vehicles share a market, and a slow month is slow for all of them at once.

**The rule:** one seeded simulation covers every vehicle in the portfolio. For each draw,
outcomes are summed *across vehicles* to produce one portfolio outcome. Percentiles are taken
across the 2000 resulting portfolio outcomes.

```text
for draw d in 1..2000:
    units_sold[d]  = count(v : days_to_sale[d,v] ≤ H)
    revenue[d]     = Σ transaction_price[d,v]   for v sold within H
    gross[d]       = Σ front_end_gross[d,v]     for v sold within H
    holding[d]     = Σ cash_holding_cost[d,v]   for all v, capped at H
    net_value[d]   = Σ net_economic_value[d,v]  for v sold within H
    ending_inv[d]  = current_inventory − units_sold[d] + arrivals(H) − other_exits(H)

portfolio_forecast = summarize(units_sold, revenue, gross, net_value, ending_inv)
```

Every figure carries the same `simulation_id`, so the reported P10 revenue and P10 units are
consistent with one another by construction.

A worked consequence: summing vehicle-level P50 revenue overstates portfolio P50 revenue,
because it credits every vehicle with a median outcome simultaneously. The draw-level result
is lower and correct.

---

## 2. Portfolio valuation

Point-in-time, no simulation required (§14.3).

| Figure | Derivation |
| --- | --- |
| `total_cost_basis` | Σ acquisition + transport + recon + auction fee |
| `total_current_list_value` | Σ current list price |
| `total_external_market_value` | Σ external reference price (D5 anchor) |
| `total_internal_base_value` | Σ internal comparable estimate, where computable |
| `total_expected_transaction_value` | Σ P50 transaction price **from draws** |
| `total_pricing_variance` | list value − external market value |
| `total_promotional_headroom` | Σ per-vehicle max safe discount |
| `cash_tied_up` | total cost basis − floorplan financing outstanding |
| `total_liquidation_value` | **null in MVP** — no source (`open-questions.md` C1) |

`total_expected_transaction_value` is the one entry that comes from the simulation, because
transaction price is a distribution rather than a stored fact.

---

## 3. One-month forecast (30 days)

Horizon H = 30. Distributions for unit sales, revenue, front-end gross, and net economic value
per §14.5, plus:

```text
ending_inventory      per draw, then summarized
ending_utilization    = ending_inventory ÷ total_physical_slots       (D6 basis)
open_slots            = total_physical_slots − ending_inventory
holding_cost          Σ cash holding cost accrued within H
depreciation_loss     Σ depreciation on unsold units within H

P(revenue < target)   = count(draws : revenue[d] < revenue_target) ÷ draw_count
P(utilization > target) = count(draws : ending_utilization[d] > target) ÷ draw_count
```

Both probabilities are direct draw counts, not normal approximations — the underlying
distributions are skewed and bounded, and a normal approximation would misstate exactly the
tail the general manager is asking about.

---

## 4. Three-month forecast (90 days)

H = 90, with the additional dynamics §14.6 requires.

| Dynamic | Treatment |
| --- | --- |
| Confirmed inbound | Arrives on its expected date, enters the simulation from that day |
| Expected acquisitions | Only if supplied; otherwise see §5 below |
| Aging transitions | Days in inventory advances per draw; aging affects the hazard |
| Scheduled events | Event lift multiplier applied within the event window |
| Seasonality | Configured monthly multiplier |
| Capacity limits | Arrivals that would exceed physical slots are deferred, not dropped |
| Depreciation | Accrues per draw on unsold units |
| Wholesale disposition | Units exceeding `wholesale_age_threshold` exit at configured wholesale value |

Capacity is a **constraint on the simulation**, not a post-hoc check. Modeling arrivals that
physically cannot fit would produce an ending inventory above 100% utilization that could
never occur, and would understate the cost of being full — which is the entire point of the
promotion skill.

---

## 5. Run-off forecast

§14.6 requires a defined behavior when acquisition data is unavailable. Since no MCP tool
supplies planned acquisitions, **this is the MVP default path, not an edge case.**

The run-off forecast projects current inventory plus confirmed inbound only. No replacement
vehicles are assumed. It returns:

1. The current-inventory run-off forecast
2. `FUTURE_ACQUISITION_DATA_UNAVAILABLE`

Its interpretation must be stated wherever it is displayed: ending inventory and utilization
are **lower bounds**, because a real dealer will replace sold units. Revenue is a lower bound
for the same reason. A general manager reading a 90-day forecast showing utilization falling
to 40% would otherwise draw precisely the wrong conclusion.

---

## 6. Capacity position

```text
target_ending_inventory = total_physical_slots × target_utilization      (D6)
reserved_not_inbound    = reserved_slots − confirmed_inbound
physical_open_slots     = total_physical_slots − current_inventory
effective_open_slots    = physical_open_slots − reserved_not_inbound − confirmed_inbound
current_utilization     = current_inventory ÷ total_physical_slots
```

Utilization is always measured against physical slots, matching what the general manager means
and what their inventory tools display. `reserved_slots ⊇ confirmed_inbound` per the MCP contract, so the two
are never both deducted.

---

## 7. Risk ranking

### 7.1 Aging profile

Buckets per §14.4: 0–30, 31–60, 61–90, 91–120, 120+. Reported as unit count, cost basis, and
**projected** units per bucket at H — where projected aging comes from the draws, so
"how much of my inventory will be over 90 days in a month" is answerable rather than inferred.

### 7.2 Top risk vehicles

Ranked by expected economic damage, not by age alone:

```text
risk_score = w1 × P(projected_total_inventory_age > 90)
           + w2 × normalized(P90 depreciation_loss)
           + w3 × normalized(P90 cash_holding_cost)
           + w4 × P(net_economic_value < 0)
           + w5 × indicator(break_even > market_value)
           + w6 × normalized(cost_basis)
```

Cost basis is included so that a $45,000 unit at moderate risk outranks a $9,000 unit at high
risk. The list exists to direct attention, and attention should follow dollars at stake.

Weights are configured, not fitted (`open-questions.md` C2).

### 7.3 Recommended actions

Each vehicle receives exactly one action from §14.8, assigned by a deterministic decision
table evaluated in order — first match wins, so the mapping is auditable:

| Condition | Action |
| --- | --- |
| Break-even > market value, age > 90 | `LOSS_MINIMIZATION_REVIEW` |
| Age > `wholesale_age_threshold` | `WHOLESALE_DISPOSITION` |
| `BLOCKING` warning present | `MANAGER_REVIEW` |
| P(age > 90) high, headroom available | `VELOCITY_REPRICE` |
| Priced above market, deal rating poor | `BALANCED_REPRICE` |
| Priced below market, selling fast | `INCREASE_PRICE` |
| Event eligible, headroom available | `EVENT_PROMOTION` |
| otherwise | `RETAIN_PRICE` |

Thresholds live in `config/assumptions/portfolio.yaml`. The promotion skill consumes
`EVENT_PROMOTION` candidates from this ranking rather than re-deriving them (§28).
