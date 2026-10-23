# Open Questions, Settled Decisions, and Missing Data

**Companion to** `docs/product-spec.md`. **Status:** live document.

Section A records decisions that were ambiguous or self-contradictory in the specification
and have since been settled. Section B records deliberate deviations from the specification.
Section C is the §28 step 8 deliverable: assumptions and missing data that remain unresolved
and would need a real answer before this prototype could inform a real pricing decision.

---

## A. Settled decisions

Each entry states what was ambiguous, what was decided, why, and what was rejected.
Reopening any of these requires re-deriving the schemas that depend on it.

---

### D1 — Percentile direction convention

**Ambiguity.** §13.7 required both "P90 value at sale" and "P90 depreciation loss". These
are inversely related: the 90th percentile of loss is the 10th percentile of value. A
literal implementation would compute `loss_p90 = current_value − value_p90` and return a
figure with the wrong sign of risk.

**Decision.** Every percentile is taken on its own quantity's distribution. Both value and
loss are emitted as full percentile sets, correctly labeled. Nothing is renamed to encode
"good" or "bad".

Risk direction is declared once, per quantity, in `docs/forecast-definitions.md`, and warning
rules reference the risk-relevant tail explicitly. Audit metadata carries
`percentile_convention: "OWN_DISTRIBUTION_V1"`.

**Rejected.** Emitting only the risk-relevant tail under a `downside_*` name. Safer against
misuse, but discards the upside figure and makes the schemas asymmetric.

**Residual risk.** A consumer — particularly the LLM explanation layer — can still quote the
optimistic tail of a loss-bearing quantity. Mitigated by the risk-direction table and by
constraining the explanation layer to fields the result object marks as narratable.

**Touches.** §13.7, §13.8, all forecast schemas.

---

### D2 — Draw-level simulation

**Ambiguity.** §12.5 forbids implying that P90 price, P90 days, and P90 holding cost occur in
one scenario. §13.5 nonetheless requires `expected_net_economic_value` per pricing scenario,
which is a joint function of all three. Computing it from marginals violates §12.5.

**Decision.** The sales-outcome service returns a **draw matrix**, not summary statistics.
Every derived financial quantity is computed per draw and only then summarized. One seeded
simulation serves all three skills:

* single vehicle — summarize its own draws
* portfolio — sum across vehicles **within** each draw, then summarize
* promotion — re-run the same seeded draws with modified prices, so plans are comparable to
  each other and to the no-promotion baseline

Every distribution object in every schema carries a `simulation_id`. Two distributions may be
combined only when their `simulation_id` matches. This is the enforceable form of §12.5.

Defaults: 2000 draws, fixed configured seed, both recorded in audit.

**Rejected.** Services returning summary statistics that skills recombine. Simpler interfaces,
but makes §14.7 unimplementable and violates §12.5 wherever net economic value appears.

**Cost.** Domain services take and return arrays rather than scalars — a more invasive
interface than the specification's prose implies. Roughly 9 MB per portfolio run at 184
vehicles × 2000 draws.

**Touches.** §12.5, §13.5, §14.5, §14.7, §15.8, all forecast schemas, `src/simulation/`.

---

### D3 — Cash holding cost separated from slot opportunity cost

**Ambiguity.** §11.6 subtracted slot opportunity cost from net economic value, and §17
included it inside daily holding cost, which §11.6 also subtracted. Every net value figure
was too low by one slot-cost term.

**Decision.** Split into two reported quantities that are never summed into one field:

| | Cash holding cost | Slot opportunity cost |
| --- | --- | --- |
| Nature | out-of-pocket or accrued | imputed |
| Break-even, minimum safe price, §19.1 bars | included | **excluded** |
| Net economic value, promotion ranking | included | included |

**Rationale.** Deduplication alone would have been satisfied by deleting either copy. The
split is chosen because an imputed cost inside a price floor is a financial-safety defect: it
inflates break-even and blocks sales that are profitable in accounting terms, and §19.1 makes
that floor a publication bar. Break-even must remain defensible in accounting terms.

**Rejected.** A single combined holding cost. One less field, but break-even inherits an
imputed number.

**Touches.** §11.6, §17 (restructured into §17.1–§17.4), break-even and holding-cost schemas.

---

### D4 — Expected discount rate

**Ambiguity.** §11.11 requires backing out "expected discounting" to derive minimum safe list
price, and §13.9 outputs a negotiation reserve, but no input in §13.3 or §10.1 supplies a
discount rate. Minimum safe list price, promotional headroom, and the entire promotion skill
had no defined floor to compute against.

**Decision.** A configured `expected_discount_rate` by segment × price band, in
`config/assumptions/discounting.yaml`, overridable per dealer.

```text
minimum_safe_list_price = minimum_safe_transaction_price ÷ (1 − expected_discount_rate)
negotiation_reserve     = list_price × expected_discount_rate
```

**Blocked calibration.** §9.9 returned transaction prices but not list price at time of sale,
so the rate could not be derived from dealer history. `list_price_at_sale` was **added to the
§9.9 contract** so the assumption is calibratable in a real integration. The MVP still uses
the configured default.

**Rejected.** Per-analysis user input. More honest about uncertainty, but the manager will not
reliably know it and it cannot be defaulted sensibly.

**Residual risk.** An invented constant sits underneath every floor and headroom figure in the
system. It must appear in the UI assumptions panel, not be buried in config.

**Touches.** §9.9, §11.11, §11.12, §13.9, §15.7.

---

### D5 — Valuation source reconciliation

**Ambiguity.** §13.4 steps 6–7 ("compare valuation sources", "calculate a normalized
market-supported range") define the number every other figure depends on, but state no rule.
§9.4 gives a principle — the market-data provider is "one evidence source" — not an algorithm. An undefined rule
here is precisely the gap where an LLM would end up selecting a number, which §4.1 exists to
prevent.

**Decision — external market data primary, internal engine as check.**

1. The provider's `market_reference_price` and `recommended_range` anchor the market-supported range.
2. The internal comparable-based valuation is computed **independently and always**. It is
   never skipped when the provider responds.
3. `divergence = |internal − external| ÷ external`
   * `> 0.05` → `EXTERNAL_PROVIDER_VARIANCE` at MEDIUM, valuation confidence drops one level
   * `> 0.10` → `EXTERNAL_PROVIDER_VARIANCE` at HIGH, and the published range widens to
     contain both estimates
4. Provider unavailable, or stale beyond the §21 threshold → fall back to the internal valuation,
   emit `EXTERNAL_VALUATION_UNAVAILABLE`, reduce confidence.

Thresholds live in `config/assumptions/valuation.yaml`.

**Rejected.** Confidence-weighted blending of the two sources, and internal-primary. Blending
produces a number neither source would defend and is hard to explain to a GM; internal-primary
diverges from the dealer's existing workflow.

**Accepted tradeoff.** The headline number originates in a mocked external service, so a
reviewer of this prototype sees a figure the repository did not compute. The mandatory
independent internal valuation running alongside is what keeps the result defensible, which is
why step 2 is not optional.

**Touches.** §9.2, §9.4, §13.4, §20.2, valuation schemas.

---

### D6 — Capacity basis for the promotion target

**Ambiguity.** §15.4 computes `total capacity × target utilization`, but §10.2 returns
`total_physical_slots`, `reserved_slots`, and `effective_open_slots` as distinct quantities,
and never states whether reserved slots and confirmed inbound describe the same units. If they
do and both are counted, every inbound vehicle is double-counted and the promotional sales
target is inflated.

**Decision.**

```text
target_ending_inventory = total_physical_slots × target_utilization
```

Utilization is measured against physical capacity, because a general manager saying "70%
utilization" means 70% of the lot.

The contract now defines **`reserved_slots ⊇ confirmed_inbound`**. The §15.4 flow equation
uses `confirmed_inbound`; any reserved capacity in excess of confirmed inbound is deducted
separately as `reserved_not_inbound`.

**Rejected.** Targeting against effective capacity. Defensible, but "70% utilization" would
then mean something other than what the general manager said and would not match the figure
they see in their inventory tools.

**Touches.** §10.2, §15.4, `docs/mcp-contract.md`, promotion schemas.

---

### D7–D9 — Settled without objection

* **D7 — Warning severity mapping.** §20.1 defines six severities but never maps codes to
  them. A full mapping lives in `config/assumptions/warnings.yaml`. `BLOCKING` is reserved
  exclusively for the §19.1 publication bars, so that "blocking" always means "publication is
  refused" and never merely "serious".
* **D8 — Injectable clock.** §21's freshness thresholds would mark every synthetic fixture
  stale on every run. All MCP mocks take an injected `as_of` timestamp, and fixtures declare
  their age relative to it. This also makes §26.1's "stale market data" scenario deliberate
  rather than an artifact of wall-clock drift.
* **D9 — Repository root.** §25 roots the tree at `used-vehicle-pricing-agent/`. Built at the
  repository root instead, to avoid a redundant nesting level inside `Pricing_demo/`.

---

## B. Deviations from the specification

| # | Deviation | Reason |
| --- | --- | --- |
| B1 | An 18th schema, `common-types.schema.json`, beyond the 17 listed in §24 | Percentile sets, money, confidence, and simulation references are shared by most schemas. The alternative — hosting them inside `sales-outcome-distribution.schema.json` — would force the depreciation and break-even schemas to reference the sales-outcome schema for a numeric type they do not otherwise use. |
| B2 | §17 restructured into §17.1–§17.4 | Required by D3. |
| B3 | `list_price_at_sale` added to §9.9 | Required by D4. |
| B4 | `EXTERNAL_VALUATION_UNAVAILABLE` added to §20.2 | Required by D5 branch 4. |
| B5 | Project tree at repository root | D9. |

---

## C. Unresolved assumptions and missing data

The §28 step 8 deliverable. These are **not** blocking the prototype; they are the honest
list of what a production version would have to resolve. Ordered by consequence.

### C1 — Data sources the specification requires but no tool contract provides

| Missing input | Required by | Consequence | Proposed resolution |
| --- | --- | --- | --- |
| **Policy price floor** and **configured risk floor** | §11.9, §11.10 | Two of the four inputs to minimum safe transaction price — the central financial-safety control — have no MCP source | Add an internal MCP tool `get_dealer_pricing_policy` returning policy floor rule, risk floor, minimum gross policy, and approval thresholds. Prototype reads from `config/assumptions/pricing.yaml`. |
| **Financing constraint** | §11.10 | §10.1 returns `financing_amount` but no rule converts it into a price constraint | Prototype treats the constraint as `financing_amount` (payoff must be covered). Real rule is lender-specific. |
| **Other expected exits** | §15.4 flow equation | Directly changes the promotional unit target | Prototype assumes zero and emits a data-coverage note. Real source is wholesale and auction disposition schedules. |
| **Liquidation value** | §14.3 | Marked "if available" and no tool returns it | Omitted from MVP output; field present in schema, nullable. |
| **Wholesale disposition value** | §14.8 | Wholesale is a recommended action with no value attached to it | Prototype uses a configured percentage of market value. Real source is auction data. |

### C2 — Invented constants

Every figure below is a prototype assumption in `config/assumptions/`, not a calibrated value.
The UI must present them as assumptions, per §27 item 13.

* `expected_discount_rate` — see D4
* `slot_opportunity_cost_per_day` — **circular by nature**: the economic value of a slot
  depends on the portfolio that would occupy it. The prototype uses a configured constant
  rather than solving the fixed point. A production version needs either a marginal-vehicle
  model or an explicitly assumed hurdle rate.
* monthly depreciation rates by segment and powertrain
* seasonality factors
* event demand-lift multipliers where no history exists
* dealer performance factor
* base daily sale hazard and the price-to-market elasticity that scales it
* internal deal-rating thresholds, where the provider does not supply its own

### C3 — Uncalibrated model relationships

* **Shopper engagement to sale probability.** §9.8 supplies engagement metrics and §16.2 lists
  them as a model input, but no mapping is specified. The prototype applies a bounded
  multiplier and flags low confidence when engagement is absent.
* **Price-to-market elasticity.** The single most consequential relationship in the system —
  it drives every velocity-versus-gross tradeoff — and is entirely assumed.
* **Event lift transferability.** §26.3 requires both an event with validated lift and one
  without. No rule states how much a past event's lift generalizes to a different event type,
  season, or inventory mix.

### C4 — Undefined business rules

* **Duplicate inventory and cannibalization.** §15.5 scores duplicate inventory and §20.4
  defines `PRICE_CANNIBALIZATION_RISK`, but "duplicate" is never defined. The prototype uses
  same year ± 1, same model, same trim group. The threshold at which discounting one unit
  harms another is assumed.
* **"Vehicles likely to sell before the event"** (§15.6). Threshold not specified. Prototype
  excludes vehicles whose P50 days to sale falls before the event start.
* **"Unusually aggressive price adjustment"** (§22.2). Approval trigger with no defined
  magnitude. Prototype uses a configured percentage change from current list price.
* **"Material gross reduction"** (§22.2, capacity-first plans). Same problem; configured
  threshold.

### C5 — Integration risk

§8.1 already states that the market-data tools may not exist as public APIs. Everything in
`docs/mcp-contract.md` is a **proposed** contract shaped to the specification's stated
needs, not an observed one. The nine market-data tools carry differing degrees of plausibility, and
`get_shopper_engagement` in particular is the least likely to be available in the assumed form.
Contract drift is the largest single integration risk in this design.
