# Internal Used Vehicle Pricing and Inventory Optimization Agent

## 1. Document Overview

### 1.1 Product name

**Internal Used Vehicle Pricing and Inventory Optimization Agent**

### 1.2 Product vision

Build an internal AI agent that helps used-vehicle dealers make explainable, financially responsible pricing and inventory decisions.

The agent converts natural-language requests into validated structured data, retrieves authorized market and inventory information through a market-data MCP integration, invokes deterministic valuation and forecasting services, and returns:

* single-vehicle valuation
* pricing recommendations
* promotional headroom
* P50 and P90 sales forecasts
* break-even analysis
* holding-cost and depreciation exposure
* portfolio valuation
* one-month and three-month sales forecasts
* event promotion plans
* inventory-capacity recommendations
* warning flags, approvals, and audit records

The agent must support decision-making but must never automatically publish a vehicle price.

---

# 2. Business Problem

Used-vehicle pricing decisions require dealers to balance several competing objectives:

* protect front-end gross
* price vehicles competitively
* reduce days in inventory
* avoid future depreciation
* control floorplan and holding costs
* maintain available inventory slots
* prepare for inbound vehicles
* create discount room for sale events
* avoid pricing below break-even
* maximize portfolio-level economic value

A dealer may be able to list a vehicle at a higher price and earn more nominal gross, but that price may:

* increase expected days to sale
* increase holding costs
* increase depreciation exposure
* consume limited inventory capacity
* create a poor-deal market position
* reduce shopper conversion
* prevent the dealer from acquiring more productive inventory

The product must therefore evaluate both:

1. **Vehicle-level economics**
2. **Portfolio-level inventory economics**

---

# 3. Primary Users

## 3.1 Used Vehicle Manager

Responsible for:

* vehicle pricing
* inventory aging
* gross protection
* turn rate
* appraisal and acquisition decisions
* promotion decisions

## 3.2 Pricing Analyst

Responsible for:

* monitoring market position
* reviewing comparables
* analyzing price-to-market
* identifying repricing opportunities
* validating pricing exceptions

## 3.3 General Manager

Responsible for:

* inventory investment
* portfolio profitability
* sales targets
* promotion approval
* exception approval
* capacity utilization

## 3.4 Marketing or Merchandising Manager

Responsible for:

* sale-event planning
* promotional pricing
* campaign eligibility
* event budget
* advertising coordination

---

# 4. Core Product Principles

## 4.1 LLM as orchestrator, not pricing engine

The LLM may:

* understand natural language
* extract structured data
* identify missing or ambiguous information
* select MCP tools
* coordinate workflows
* explain calculations
* summarize risks and trade-offs

The LLM must not independently generate:

* vehicle valuation
* recommended numerical price
* days-to-sale forecast
* depreciation forecast
* holding cost
* break-even value
* portfolio revenue forecast

All numerical outputs must come from deterministic services, configured simulation models, or validated predictive models.

## 4.2 Structured data before tool execution

No valuation or pricing tool may be called directly from unvalidated free text.
The agent must first produce a validated JSON request.

## 4.3 Human-in-the-loop

The agent may recommend a price or promotion plan, but it must not:

* publish a price
* activate a promotion
* commit inventory
* approve a below-floor sale

without explicit user approval and any required manager approval.

## 4.4 Explainability

Every recommendation must preserve:

* source data
* comparable vehicles
* calculation breakdown
* assumptions
* confidence
* model version
* data timestamps
* warning flags
* approval requirements

## 4.5 Financial safety

The system must protect configurable hard price floors.
Below-floor pricing is allowed only through a documented loss-minimization exception workflow.

## 4.6 Portfolio awareness

The product must not optimize one vehicle while ignoring:

* lot capacity
* inbound vehicles
* duplicate inventory
* replacement opportunity
* sale events
* promotion budget
* portfolio gross
* inventory aging concentration

---

# 5. Scope

## 5.1 MVP scope

The MVP will support:

* natural-language vehicle input
* strict JSON extraction and validation
* mocked market-data MCP
* synthetic dealer inventory
* single-vehicle valuation
* three pricing strategies
* P50 and P90 sales forecasts
* configurable holding-cost assumptions
* configurable depreciation assumptions
* break-even and price-floor analysis
* portfolio valuation
* 30-day and 90-day sales forecast
* promotion planning
* warning and approval rules
* Streamlit internal interface, organised by dealer workflow (§6.1)
* audit-ready structured output

## 5.2 Out of scope for MVP

* automatic retail price publishing
* production scraping of third-party valuation sites
* real consumer credit data
* autonomous purchasing or disposal
* trained production sales forecasting
* production-grade dealer authentication
* dealer management system writeback
* automated wholesale auction execution

---

# 6. System Architecture

```text
Internal User
    ↓
Pricing and Inventory Agent
    ↓
Natural-Language Intake and Intent Router
    ↓
Validated Structured JSON
    ↓
Skill Router
    ├── Single Vehicle Valuation Skill
    ├── Inventory Portfolio Forecast Skill
    └── Dealer Event Promotion Planner Skill
    ↓
MCP Orchestration Layer
    ├── Market-data MCP
    ├── Internal Cost MCP
    ├── Dealer Capacity MCP
    ├── Event Calendar MCP
    └── Approval and Audit MCP
    ↓
Deterministic and Predictive Services
    ├── Valuation Engine
    ├── Comparable Selection Engine
    ├── Sales Outcome Forecast
    ├── Depreciation Forecast
    ├── Holding Cost Engine
    ├── Break-Even Engine
    ├── Portfolio Simulation
    └── Promotion Optimizer
    ↓
Policy and Fail-Safe Engine
    ↓
Structured Result
    ↓
LLM Explanation
    ↓
User Review and Approval
```

## 6.1 Workflow layer

*Added after the skill-first prototype was built. The layers above are unchanged; this
names the layer between the user and the Skill Router.*

The product is presented as the dealer's jobs, not as the tool's capabilities. Five terms,
used consistently across the interface, the code, and this document:

| Term | Definition | Count |
| --- | --- | --- |
| **Agent** | Reads a request in the user's own words and directs it. Orchestrates; never computes (§4.1). | 1 |
| **Workflow** | A job the dealer has. Sequences skills and frames the result. What the navigation is made of. | 4 |
| **Skill** | A reusable capability owning one analysis end to end (§10). Never a navigation entry. | 3 |
| **MCP tool** | A typed adapter over a system of record (§8, §9). | — |
| **Dashboard** | A view rendering a finished result. Never calculates. | — |

The four workflows:

| Workflow | Question it answers | Skills used |
| --- | --- | --- |
| **Acquire Inventory** | What can the lot absorb before buying more? Capacity, gaps, open slots, aging and replacement pressure. | portfolio forecast |
| **Price Inventory** | What is this vehicle worth, what should it be listed at, and what floor constrains it? | single-vehicle valuation |
| **Merchandise Inventory** | Which vehicles to discount for an event, by how much, and is the target reachable? | promotion planner |
| **Improve Aging Inventory** | What should be done about the aged units? | all three |

**Acquire Inventory does not evaluate an external acquisition candidate.** It answers what
the lot can absorb, from the inventory the dealer already has. Appraising a specific
prospective purchase would require a valuation of a vehicle not in inventory and an
acquisition-cost source; neither is in MVP scope (§5.2).

**Improve Aging Inventory is a workflow, not a fourth skill.** It coordinates the three
existing skills against aged units and introduces no valuation, forecasting, or promotion
arithmetic of its own. §28's prohibition on reimplementing a calculation is the reason: a
fourth skill would have duplicated all three.

Navigation is declared as data in one registry module rather than implied by filenames, so
the vocabulary in this section and the vocabulary on screen cannot drift apart.

### Implementation status

The workflow layer is built out across phases; the interface states plainly what is and is
not connected.

* **Deterministic natural-language routing** (§7.1) is connected as of Phase 4 — the
  assistant classifies a request, resolves the vehicle, and runs one skill, with **no model**
  in the path. LLM-assisted routing will sit above this deterministic layer in a later phase.
* **Improve Aging orchestration** is connected as of Phase 5. It coordinates all three skills
  — portfolio forecast → candidate selection → single-vehicle valuation for the aged cohort →
  promotion plan when a real event is named → one consolidated action plan — and adds no
  arithmetic of its own. Figures from different simulations are shown side by side, never
  summed. See `docs/improve-aging-orchestration-results.md`.

---

# 7. Main Agent Responsibilities

Create a main agent named:

`used-vehicle-pricing-agent`

The main agent must:

1. Understand the user's natural-language request.
2. Identify the requested workflow.
3. Extract required entities and objectives.
4. Produce validated JSON.
5. Select the correct skill.
6. Call the required MCP tools.
7. Detect missing, ambiguous, or stale data.
8. Apply fail-safe policies.
9. Present results in user-friendly language.
10. Preserve structured output and audit metadata.
11. Require confirmation before any write action.

## 7.1 Intent routing

The main agent must route requests as follows:

### Single-vehicle analysis

Examples:

* "What is this vehicle worth?"
* "How much can I discount this RAV4?"
* "How long will this vehicle take to sell?"
* "What is my break-even price?"

Route to:
`single-vehicle-valuation`

### Portfolio analysis

Examples:

* "What is my total inventory worth?"
* "How much revenue will I make next month?"
* "How much should I expect to sell in 90 days?"
* "Which vehicles are creating the greatest aging risk?"

Route to:
`inventory-portfolio-forecast`

### Promotion planning

Examples:

* "Create a July 4th promotion plan."
* "I need to reduce inventory utilization to 70 percent."
* "Which cars should I discount for Christmas?"
* "How much promotion budget is required to clear 25 vehicles?"

Route to:
`dealer-event-promotion-planner`

---

# 8. Market-data MCP Integration

## 8.1 Integration assumption

The market-data MCP described in this specification is a proposed authorized integration layer.

It may be implemented through:

* provider-supported APIs
* the provider's internal services
* dealer-authorized data exports
* enterprise integration middleware
* a custom MCP adapter around authorized provider capabilities

The system must not assume that all listed tools currently exist as publicly available provider APIs.
For the prototype, use mocked market-data MCP responses.

## 8.2 Market-data MCP responsibilities

The market-data MCP should provide authorized access to:

* dealer vehicle inventory
* current list prices
* market position
* deal rating
* comparable vehicles
* inventory age
* price history
* market supply
* pricing recommendations
* sales velocity indicators
* shopper engagement, when available

The market-data MCP should not be responsible for:

* interpreting free-form user input
* calculating dealer-specific acquisition cost
* calculating dealer-specific break-even
* determining dealer policy floors
* approving pricing exceptions
* publishing prices without approval

---

# 9. Proposed Market-data MCP Tool Contracts

## 9.1 `get_dealer_inventory`

### Purpose

Retrieve the dealer's active used-vehicle inventory.

### Input

```json
{
  "dealer_id": "DEALER-1001",
  "status": ["ACTIVE"],
  "include_pending": false
}
```

### Output

```json
{
  "dealer_id": "DEALER-1001",
  "inventory_count": 184,
  "vehicles": [
    {
      "vehicle_id": "V-10028",
      "vin": "VALID_17_CHARACTER_VIN",
      "year": 2022,
      "make": "Toyota",
      "model": "RAV4",
      "trim": "XLE",
      "mileage": 42000,
      "current_list_price": 29900,
      "days_in_inventory": 83,
      "status": "ACTIVE"
    }
  ],
  "data_timestamp": "ISO-8601 timestamp",
  "source_version": "string"
}
```

## 9.2 `get_vehicle_market_position`

### Purpose

Return market positioning for one vehicle.

### Output

* market reference price
* price-to-market ratio
* market percentile
* deal rating
* good-deal threshold
* fair-deal threshold
* poor-deal threshold
* confidence
* effective date

## 9.3 `get_vehicle_comparables`

### Purpose

Return comparable market listings.

### Required output per comparable

* listing identifier
* year
* make
* model
* trim
* mileage
* condition, when available
* list price
* distance
* days on market
* similarity score
* data timestamp

## 9.4 `get_vehicle_pricing_recommendation`

### Purpose

Retrieve any available provider pricing recommendation or market-supported range.
The internal agent must treat this as one pricing evidence source rather than an unquestionable final answer.

Return:

* recommended price
* recommended range
* source methodology identifier
* market position
* confidence
* effective date
* service version

## 9.5 `get_vehicle_price_history`

Return:

* original list price
* current list price
* historical price changes
* dates
* cumulative markdown
* campaign participation

## 9.6 `get_vehicle_inventory_age`

Return:

* days in inventory
* acquisition date
* merchandising start date
* aging bucket
* aging status

## 9.7 `get_market_sales_velocity`

Return:

* local average days to sale
* median days to sale
* sales volume
* active supply
* supply-to-sales ratio
* seasonal indicators
* confidence

## 9.8 `get_shopper_engagement`

When authorized and available, return:

* listing views
* vehicle detail page views
* saved vehicles
* leads
* calls
* appointments
* conversion indicators

If unavailable, the agent must continue with reduced confidence.

## 9.9 `get_dealer_sales_history`

Return historical dealer-level sales information required for portfolio forecasting:

* vehicles sold
* transaction prices
* list price at sale
* days to sale
* gross
* segment
* event participation
* date
* baseline versus promoted sale

---

# 10. Internal MCP Tools

The following services may come from dealer systems rather than the market-data provider.

## 10.1 `get_vehicle_cost_basis`

Return:

* acquisition cost
* auction fee
* transportation cost
* reconditioning cost
* accrued holding cost
* financing amount
* selling costs

## 10.2 `get_dealer_capacity`

Return:

* total physical slots
* current inventory
* physical open slots
* reserved slots
* confirmed inbound
* expected exits
* effective open slots
* current utilization
* projected utilization
* target utilization

## 10.3 `get_sales_event_calendar`

Return:

* event name
* start date
* end date
* event type
* eligible inventory
* promotion budget
* dealer-funded incentives
* partner-funded incentives
* historical demand lift
* confidence

## 10.4 `get_inbound_inventory`

Return:

* inbound vehicle count
* expected arrival dates
* segment
* committed slots
* acquisition status

## 10.5 `request_manager_approval`

Create an approval request for:

* below-floor pricing
* emergency markdown
* loss-minimization sale
* budget exception
* aggressive promotion

## 10.6 `save_pricing_decision`

Preserve:

* system recommendation
* user-selected price
* override reason
* user
* manager approval
* timestamp
* data versions
* model versions

## 10.7 `publish_vehicle_price`

This must be a separate write tool.

It must require:

* explicit user confirmation
* valid approval status
* final price
* pricing decision ID
* idempotency key

The main agent must never call this tool automatically.

---

# 11. Shared Data Definitions

## 11.1 Current list price

The advertised vehicle price currently displayed by the dealer.

## 11.2 Transaction price

The expected final selling price after negotiation, promotion, incentive, or markdown.

## 11.3 Market value

A market-supported estimate based on valuation sources and comparable vehicles.

## 11.4 Cost basis

The dealer's invested cost:

```text
Acquisition
+ Transportation
+ Reconditioning
+ Other capitalized vehicle costs
```

## 11.5 Front-end gross

```text
Transaction price
- Acquisition cost
- Reconditioning cost
- Transportation cost
- Direct selling costs
```

## 11.6 Net economic value

```text
Front-end gross
- Cash holding cost
- Depreciation loss
- Promotion cost
- Slot opportunity cost
```

Cash holding cost and slot opportunity cost are two distinct fields defined in §17.
Slot opportunity cost is an imputed economic cost. It appears in net economic value and
in promotion candidate ranking only. It must never enter break-even, minimum safe
transaction price, or any §19.1 publication bar.

## 11.7 Current accounting break-even

The minimum transaction price required to recover costs already incurred or contractually committed as of today.

## 11.8 Projected break-even

The transaction price required to recover current costs plus expected future costs until sale.

## 11.9 Policy price floor

The minimum price permitted under dealer policy.

## 11.10 Minimum safe transaction price

The highest applicable minimum among:

* accounting break-even
* policy floor
* financing constraint
* configured risk floor

## 11.11 Minimum safe list price

The list price required to preserve the minimum safe transaction price after expected discounting.

## 11.12 Promotional headroom

```text
Current or recommended list price
- Minimum safe promotional list price
```

---

# 12. Forecast Percentile Definitions

## 12.1 P50 additional days to sale

There is a 50 percent estimated probability that the vehicle will sell within this number of additional days.

## 12.2 P90 additional days to sale

There is a 90 percent estimated probability that the vehicle will sell within this number of additional days.

Normally:

```text
P90 days to sale ≥ P50 days to sale
```

## 12.3 Projected total inventory age

```text
Current days in inventory
+ Predicted additional days to sale
```

## 12.4 Transaction-price percentiles

P50 transaction price represents the median modeled transaction price.
P90 transaction price means that 90 percent of modeled transaction prices are at or below that value.

For downside risk, P10 transaction price is normally more important than P90.

## 12.5 Joint-distribution warning

The system must not imply that:

* P90 transaction price
* P90 days to sale
* P90 holding cost

necessarily occur in the same simulated scenario.

Where possible, preserve joint simulation records.

---

# 13. Skill 1: Single Vehicle Valuation

Create:
`skills/single-vehicle-valuation/SKILL.md`

## 13.1 Purpose

Analyze one vehicle and return:

* valuation
* comparable-market evidence
* current pricing position
* recommended pricing scenarios
* promotional headroom
* P50 and P90 sales time
* expected transaction-price distribution
* break-even analysis
* holding cost
* depreciation
* financial outcomes
* warnings
* approval requirements

## 13.2 Supported request example

> Analyze this 2022 Toyota RAV4 XLE with 42,000 miles. We paid $23,500 and spent $1,200 on reconditioning. It has been in inventory for 37 days. Tell me what it is worth, how much discount room we have, and the expected P50 and P90 sales time.

## 13.3 Input extraction

Extract and validate:

### Vehicle

* VIN
* year
* make
* model
* trim
* mileage
* condition
* accident history
* title status
* drivetrain
* powertrain
* optional equipment
* certified status

### Dealer context

* dealer ID
* postal code
* acquisition cost
* reconditioning cost
* transportation cost
* current price
* days in inventory
* floorplan rate
* inventory cost allocation

Preserve:

* missing fields
* ambiguous fields
* estimated fields
* field confidence
* source

## 13.4 Valuation process

The skill must:

1. Normalize vehicle identity.
2. Retrieve market positioning through the market-data MCP.
3. Retrieve comparable vehicles.
4. Retrieve any available provider price recommendation.
5. Retrieve internal base value.
6. Compare valuation sources.
7. Calculate a normalized market-supported range.
8. Generate valuation-confidence warnings.

## 13.5 Pricing strategies

Generate:

1. `MAXIMIZE_GROSS`
2. `BALANCED`
3. `INCREASE_VELOCITY`

Each must include:

* proposed list price
* transaction-price distribution
* P50 and P90 days to sale
* probability sold within 30, 60, and 90 days
* projected total inventory age
* expected holding cost
* expected depreciation
* expected front-end gross
* expected net economic value
* deal rating
* promotional headroom
* warning flags

## 13.6 Sales forecast

Return:

### Additional days to sale

* P10
* P25
* P50
* P75
* P90
* mean

### Projected total inventory age

* P10
* P50
* P90
* probability over 60 days
* probability over 90 days
* probability over 120 days

### Sale probabilities

* within 7 days
* within 30 days
* within 60 days
* within 90 days

## 13.7 Depreciation analysis

Return:

* current market value
* monthly depreciation assumption
* P50 value at sale
* P90 value at sale
* P50 depreciation loss
* P90 depreciation loss
* data source
* confidence
* model version

## 13.8 Break-even analysis

Return:

* current accounting break-even
* P50 projected break-even
* P90 projected break-even
* hard price floor
* minimum safe transaction price
* minimum safe list price
* probability of loss
* market-value crossover risk

## 13.9 Promotional headroom

Return:

* negotiation reserve
* event promotion reserve
* emergency markdown reserve
* maximum safe discount
* economically sensible discount
* used headroom
* remaining headroom

## 13.10 Single-vehicle output

```json
{
  "vehicle": {},
  "valuation": {},
  "market_position": {},
  "comparables": [],
  "break_even_analysis": {},
  "promotional_headroom": {},
  "sales_outcome_distribution": {},
  "depreciation_forecast": {},
  "pricing_scenarios": [],
  "recommended_strategy": {},
  "warnings": [],
  "approvals_required": [],
  "audit": {}
}
```

---

# 14. Skill 2: Inventory Portfolio Forecast

Create:
`skills/inventory-portfolio-forecast/SKILL.md`

## 14.1 Purpose

Analyze the dealer's entire active inventory and return:

* portfolio market value
* total cost basis
* total list value
* expected transaction value
* expected one-month sales
* expected three-month sales
* expected one-month revenue
* expected three-month revenue
* expected gross
* expected net economic value
* ending inventory
* capacity utilization
* aging exposure
* depreciation exposure
* portfolio actions

## 14.2 Supported request example

> What is my current used-inventory value, how many vehicles and how much sales revenue should I expect in the next 30 and 90 days, and which vehicles create the most financial risk?

## 14.3 Portfolio valuation

Return:

* active inventory count
* total cost basis
* total current list value
* total internal base value
* total external market value
* total expected transaction value
* total liquidation value, if available
* total pricing variance
* total promotional headroom
* cash tied up in inventory

## 14.4 Inventory segmentation

Group by:

* make
* model
* segment
* deal rating
* confidence
* price-to-market
* age bucket

Required age buckets:

* 0–30 days
* 31–60 days
* 61–90 days
* 91–120 days
* more than 120 days

## 14.5 One-month forecast

Forecast the next 30 days.

Return distributions for:

### Unit sales

* P10
* P50
* P90
* mean

### Sales revenue

* P10
* P50
* P90
* mean

### Front-end gross

* P10
* P50
* P90
* mean

### Net economic value

* P10
* P50
* P90
* mean

Also return:

* ending inventory
* ending utilization
* open slots
* inbound inventory
* holding cost
* depreciation loss
* probability revenue falls below target
* probability capacity exceeds target

## 14.6 Three-month forecast

Forecast the next 90 days.

Account for:

* current inventory
* expected inbound vehicles
* expected acquisitions
* current sales velocity
* pricing strategies
* scheduled sale events
* event demand lift
* seasonality
* capacity limits
* aging transitions
* depreciation
* wholesale disposition

If acquisition data is unavailable, return:

1. Current-inventory run-off forecast
2. Incomplete-forecast warning

## 14.7 Portfolio simulation

Do not simply add individual P50 forecasts.

Use portfolio-level simulation that preserves consistency among:

* units sold
* revenue
* transaction price
* gross
* holding cost
* depreciation
* ending inventory

For the prototype, use configurable Monte Carlo simulation and preserve:

* random seed
* simulation count
* assumption version
* data coverage
* model version

## 14.8 Portfolio actions

Rank vehicles for:

* retain price
* increase price
* balanced reprice
* velocity reprice
* event promotion
* manager review
* wholesale disposition
* loss-minimization review

## 14.9 Portfolio output

```json
{
  "dealer_context": {},
  "data_coverage": {},
  "inventory_summary": {},
  "portfolio_valuation": {},
  "aging_profile": {},
  "capacity_position": {},
  "one_month_forecast": {},
  "three_month_forecast": {},
  "event_adjustments": [],
  "financial_risk": {},
  "top_contributors": [],
  "top_risk_vehicles": [],
  "recommended_actions": [],
  "warnings": [],
  "audit": {}
}
```

---

# 15. Skill 3: Dealer Event Promotion Planner

Create:
`skills/dealer-event-promotion-planner/SKILL.md`

## 15.1 Purpose

Create a portfolio-level promotion plan that identifies:

* how many vehicles must be sold
* whether the target is achievable
* which vehicles should be promoted
* which vehicles should protect price
* maximum safe discount per vehicle
* recommended event price
* expected event sales
* ending inventory
* target-achievement probability
* gross impact
* capacity impact

## 15.2 Supported request example

> We have a July 4th sales event starting in two days. I want inventory utilization to reach 70 percent by the end of the event. Determine which vehicles should be discounted, how much promotional room each has, and whether the plan will achieve the target.

The agent must resolve exact dates through the event calendar or explicit user input.

## 15.3 Promotion objective

Extract:

* event name
* event start date
* event end date
* dealer ID
* target inventory utilization
* target ending inventory
* optimization priority
* maximum discount budget
* minimum gross target
* excluded inventory
* approval policy

## 15.4 Inventory reduction requirement

Calculate:

```text
Target ending inventory
=
Total capacity × target utilization
```

```text
Projected inventory without promotion
=
Current inventory
+ Confirmed inbound
- Baseline expected sales
- Other expected exits
```

```text
Incremental promotional sales required
=
Projected inventory without promotion
- Target ending inventory
```

## 15.5 Promotion candidate evaluation

Score each vehicle based on:

* inventory age
* projected P50 and P90 inventory age
* depreciation risk
* holding cost
* price above market
* current deal rating
* shopper activity
* expected promotion response
* promotional headroom
* break-even floor
* duplicate inventory
* inbound replacement
* slot opportunity cost

## 15.6 Candidate exclusion

Normally exclude:

* recently acquired vehicles
* vehicles already priced as a strong deal
* high-demand scarce vehicles
* vehicles likely to sell before the event
* vehicles lacking safe headroom
* vehicles with insufficient data
* vehicles already assigned to another promotion

## 15.7 Discount calculation

For each candidate calculate:

* current list price
* minimum safe transaction price
* minimum safe list price
* maximum accounting discount
* maximum safe discount
* economically sensible discount
* recommended promotion discount
* remaining headroom

## 15.8 Promotion plans

Generate:

1. `MARGIN_PROTECT`
2. `BALANCED`
3. `CAPACITY_FIRST`

For each return:

* vehicles selected
* per-vehicle promotion price
* total discount
* dealer-funded discount
* partner-funded incentive
* incremental units sold
* ending inventory distribution
* ending utilization
* probability target achieved
* expected gross impact
* holding-cost savings
* depreciation savings
* slot days released
* warnings
* approvals

## 15.9 Feasibility analysis

Return:

* required incremental units
* maximum safe candidate pool
* P50 achievable incremental units
* conservative achievable units
* event duration
* historical event lift
* target-achievement probability
* feasibility status

If the target is unrealistic, recommend combinations of:

* longer campaign
* revised utilization target
* additional promotion budget
* wholesale disposition
* reduced inbound
* manager-approved loss minimization

## 15.10 Promotion output

```json
{
  "promotion_objective": {},
  "inventory_target_calculation": {},
  "feasibility": {},
  "candidate_ranking": [],
  "excluded_vehicles": [],
  "plans": [],
  "recommended_plan": {},
  "per_vehicle_actions": [],
  "projected_ending_inventory": {},
  "financial_impact": {},
  "warnings": [],
  "approvals_required": [],
  "audit": {}
}
```

---

# 16. Sales Outcome Model

## 16.1 Production direction

The production sales model may use:

* survival analysis
* gradient-boosted trees
* probabilistic regression
* time-to-event modeling
* calibrated ensemble methods

Survival analysis is appropriate because unsold vehicles create censored observations.

## 16.2 MVP approach

Use a configurable simulation model based on:

* base market days to sale
* price-to-market ratio
* vehicle age
* mileage
* condition
* local supply
* historical sales velocity
* shopper engagement
* dealer performance
* sale-event lift
* seasonality

The MVP must be labeled:
`CONFIGURABLE_PROTOTYPE_SIMULATION`

It must not be represented as a trained production prediction.

---

# 17. Holding Cost Model

Holding cost is reported as two separate quantities. They must not be summed into a
single field.

## 17.1 Cash holding cost

Out-of-pocket or accrued accounting cost.

```text
Daily cash holding cost
=
Floorplan interest
+ Lot allocation
+ Insurance allocation
+ Maintenance
+ Administrative cost
```

## 17.2 Slot opportunity cost

Imputed economic cost of occupying a finite inventory slot. Configurable as the
expected daily net economic value of a replacement vehicle in the same slot.

## 17.3 Usage rule

| Quantity | Cash holding cost | Slot opportunity cost |
| --- | --- | --- |
| Break-even (§11.7, §11.8) | included | **excluded** |
| Minimum safe transaction price (§11.10) | included | **excluded** |
| §19.1 publication bars | included | **excluded** |
| Net economic value (§11.6) | included | included |
| Promotion candidate ranking (§15.5) | included | included |

Including an imputed cost in a price floor would block sales that are profitable in
accounting terms. The separation is a financial-safety requirement, not a presentation
choice.

## 17.4 Return

* daily cash holding cost
* monthly cash holding cost
* cash holding cost through P50 sale date
* cash holding cost through P90 sale date
* daily slot opportunity cost
* slot opportunity cost through P50 sale date
* slot opportunity cost through P90 sale date
* cost breakdown by component

---

# 18. Depreciation Model

For the prototype:

```text
Future market value
=
Current market value
× (1 - monthly depreciation rate)^(days to sale / 30)
```

Rates must be configurable by:

* segment
* powertrain
* vehicle age
* mileage
* market trend

Return:

* P50 future value
* P90 future value
* P50 depreciation loss
* P90 depreciation loss

---

# 19. Break-Even and Price-Floor Policy

## 19.1 Normal rule

A price may not be published when:

* expected transaction price is below the hard floor
* expected P50 transaction price is below current accounting break-even
* promotion price exceeds available safe headroom
* discount is likely to create negative net value above the configured threshold

## 19.2 Loss-minimization exception

Allow only with:

* manager approval
* documented reason
* immediate loss
* expected future loss
* expected holding cost
* expected depreciation
* capacity opportunity cost
* complete audit trail

---

# 20. Warning Taxonomy

## 20.1 Severity

* `INFO`
* `LOW`
* `MEDIUM`
* `HIGH`
* `CRITICAL`
* `BLOCKING`

## 20.2 Single-vehicle warnings

* `INSUFFICIENT_VEHICLE_DATA`
* `LOW_VALUATION_CONFIDENCE`
* `EXTERNAL_PROVIDER_VARIANCE`
* `EXTERNAL_VALUATION_UNAVAILABLE`
* `CURRENT_PRICE_POOR_DEAL`
* `RECOMMENDED_PRICE_POOR_DEAL`
* `P50_PROJECTED_INVENTORY_AGE_OVER_90_DAYS`
* `P90_PROJECTED_INVENTORY_AGE_OVER_90_DAYS`
* `P50_PROJECTED_INVENTORY_AGE_OVER_120_DAYS`
* `P90_PROJECTED_INVENTORY_AGE_OVER_120_DAYS`
* `HIGH_DEPRECIATION_RISK`
* `PRICE_BELOW_CURRENT_BREAK_EVEN`
* `P50_TRANSACTION_PRICE_BELOW_BREAK_EVEN`
* `P10_TRANSACTION_PRICE_BELOW_BREAK_EVEN`
* `MINIMUM_SAFE_LIST_PRICE_VIOLATION`
* `BREAK_EVEN_EXCEEDS_MARKET_VALUE`
* `BREAK_EVEN_MARKET_CROSSOVER_RISK`
* `HIGH_PROBABILITY_OF_NEGATIVE_NET_VALUE`
* `DISCOUNT_EXCEEDS_SAFE_HEADROOM`
* `HOLDING_COST_EXCEEDS_INCREMENTAL_GROSS`

## 20.3 Portfolio warnings

* `INCOMPLETE_INVENTORY_DATA`
* `STALE_MARKET_DATA`
* `LOW_PORTFOLIO_FORECAST_CONFIDENCE`
* `HIGH_AGED_INVENTORY_CONCENTRATION`
* `HIGH_PROJECTED_DEPRECIATION`
* `PROJECTED_CAPACITY_OVER_TARGET`
* `PROJECTED_CAPACITY_OVER_100_PERCENT`
* `INBOUND_CAPACITY_CONFLICT`
* `ONE_MONTH_REVENUE_BELOW_TARGET`
* `THREE_MONTH_REVENUE_BELOW_TARGET`
* `HIGH_PORTFOLIO_HOLDING_COST`
* `HIGH_PERCENTAGE_BELOW_BREAK_EVEN`
* `FUTURE_ACQUISITION_DATA_UNAVAILABLE`

## 20.4 Promotion warnings

* `UNREALISTIC_INVENTORY_TARGET`
* `INSUFFICIENT_SAFE_PROMOTION_CANDIDATES`
* `PROMOTION_BUDGET_EXCEEDED`
* `PROMOTIONAL_PRICE_BELOW_BREAK_EVEN`
* `PROMOTION_EXCEEDS_SAFE_HEADROOM`
* `LOW_EXPECTED_EVENT_LIFT`
* `PROMOTION_COST_EXCEEDS_EXPECTED_SAVINGS`
* `VEHICLE_EXPECTED_TO_SELL_BEFORE_EVENT`
* `PRICE_CANNIBALIZATION_RISK`
* `VEHICLE_ALREADY_ASSIGNED_TO_CAMPAIGN`
* `CAPACITY_TARGET_UNLIKELY_TO_BE_ACHIEVED`
* `EMERGENCY_MARKDOWN_APPROVAL_REQUIRED`

---

# 21. Data Freshness Policy

Recommended configurable defaults:

| Data                  |              Maximum age |
| --------------------- | -----------------------: |
| Active inventory      |                   1 hour |
| Current price         |                   1 hour |
| Dealer capacity       |                  4 hours |
| Market comparables    |                 24 hours |
| Market valuation      |                 24 hours |
| Shopper engagement    |                 24 hours |
| Cost basis            |                 24 hours |
| Event calendar        |                 24 hours |
| Historical model data | model-version controlled |

Stale critical data must block price publication.

---

# 22. Approval Policy

## 22.1 No approval required

* read-only analysis
* safe price recommendation
* promotion within approved budget and price floor

## 22.2 Manager approval required

* emergency markdown reserve
* below projected break-even
* high probability of negative P10 value
* unusually aggressive price adjustment
* loss-minimization strategy
* capacity-first plan with material gross reduction
* promotion budget exception

## 22.3 Explicit user confirmation required

Before:

* saving final pricing decision
* sending promotion plan for approval
* publishing a vehicle price
* activating a promotion

---

# 23. Audit Requirements

Every result must preserve:

* request ID
* dealer ID
* user ID
* input text
* normalized JSON
* vehicle identifiers
* MCP tools called
* source timestamps
* source versions
* model versions
* configuration version
* system recommendation
* warning flags
* selected action
* override reason
* approving manager
* final price
* creation timestamp
* publication timestamp

---

# 24. Required Schemas

Create:

```text
schemas/
├── common-vehicle.schema.json
├── warning.schema.json
├── audit-metadata.schema.json
├── single-vehicle-request.schema.json
├── single-vehicle-result.schema.json
├── sales-outcome-distribution.schema.json
├── depreciation-forecast.schema.json
├── break-even-analysis.schema.json
├── promotional-headroom.schema.json
├── inventory-portfolio-request.schema.json
├── inventory-portfolio-valuation.schema.json
├── inventory-sales-forecast.schema.json
├── inventory-portfolio-result.schema.json
├── promotion-objective.schema.json
├── promotion-candidate.schema.json
├── promotion-plan.schema.json
└── promotion-plan-result.schema.json
```

---

# 25. Required Project Structure

```text
used-vehicle-pricing-agent/
├── CLAUDE.md
├── README.md
├── requirements.txt
├── app.py
├── skills/
│   ├── single-vehicle-valuation/
│   │   └── SKILL.md
│   ├── inventory-portfolio-forecast/
│   │   └── SKILL.md
│   └── dealer-event-promotion-planner/
│       └── SKILL.md
├── schemas/
├── docs/
│   ├── product-spec.md
│   ├── architecture.md
│   ├── forecast-definitions.md
│   ├── valuation-methodology.md
│   ├── portfolio-forecast-methodology.md
│   ├── promotion-optimization-methodology.md
│   ├── fail-safe-policy.md
│   ├── approval-policy.md
│   └── mcp-contract.md
├── src/
│   ├── agents/
│   ├── skills/
│   ├── mcp_clients/
│   │   ├── market_data_client.py
│   │   ├── cost_client.py
│   │   ├── capacity_client.py
│   │   └── event_client.py
│   ├── domain/
│   │   ├── vehicle.py
│   │   ├── valuation.py
│   │   ├── sales_forecast.py
│   │   ├── depreciation.py
│   │   ├── holding_cost.py
│   │   ├── break_even.py
│   │   ├── promotion.py
│   │   └── portfolio.py
│   ├── policy/
│   │   ├── warnings.py
│   │   ├── price_floor.py
│   │   ├── approvals.py
│   │   └── freshness.py
│   └── simulation/
├── mocks/
│   ├── market_data/
│   ├── dealer_costs/
│   ├── events/
│   └── inventory/
└── tests/
    ├── unit/
    ├── integration/
    ├── schema/
    └── scenarios/
```

---

# 26. Required Test Scenarios

## 26.1 Single vehicle

* high-confidence vehicle with strong comparables
* poor-deal current price
* P50 under 90 days and P90 over 90 days
* both P50 and P90 over 90 days
* break-even above market value
* promotional discount below floor
* large safe headroom
* insufficient comparables
* stale market data
* high depreciation EV
* loss-minimization exception

## 26.2 Portfolio

* healthy portfolio with open capacity
* inventory utilization over target
* projected utilization over 100 percent
* high aged-inventory concentration
* one-month revenue below target
* three-month forecast without acquisition data
* large inbound inventory conflict
* portfolio with significant below-break-even exposure

## 26.3 Promotion

* realistic target achieved by balanced plan
* target not achievable within event window
* insufficient safe candidates
* event with validated demand lift
* event without historical demand lift
* partner-funded incentive
* promotion budget exceeded
* price cannibalization among duplicate vehicles
* emergency markdown requiring approval
* promotion plan still above capacity target

---

# 27. MVP Definition of Done

The MVP is complete when:

1. Natural-language requests are converted to schema-valid JSON.
2. The main agent routes requests to the correct skill.
3. Mocked market-data MCP tools return deterministic test data.
4. Single-vehicle analysis returns valuation, P50/P90 time, financial analysis, and pricing headroom.
5. Portfolio analysis returns current valuation and 30-day/90-day forecasts.
6. Promotion planning returns three portfolio plans.
7. Break-even fail-safe rules are enforced.
8. Aging and depreciation warnings are generated.
9. No numerical price is generated directly by the LLM.
10. No price can be published without confirmation.
11. All recommendations include source, confidence, version, and timestamp.
12. All critical business rules have automated tests.
13. The UI clearly distinguishes prototype assumptions from production predictions.
14. A five-minute end-to-end demonstration can be completed using synthetic data.

---

# 28. Claude Code Implementation Instruction

Before writing application code:

1. Create this specification as `docs/product-spec.md`.
2. Create `docs/architecture.md`.
3. Create `docs/mcp-contract.md`.
4. Create the three Skill files.
5. Create all JSON schemas.
6. Create mocked market-data MCP tool contracts.
7. Create synthetic test scenarios.
8. Identify unresolved assumptions and missing data.
9. Run schema validation.
10. Do not build the UI until the skill boundaries, schemas, and MCP contracts are internally consistent.

The implementation must reuse shared vehicle-level calculation modules across all skills.

The portfolio and promotion skills must not independently reimplement:

* valuation
* break-even analysis
* depreciation
* holding cost
* sales-outcome prediction
* warning policies

All prototype numerical assumptions must be centralized in configurable files rather than distributed as hard-coded constants.
