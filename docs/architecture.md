# Architecture

**Companion to** `docs/product-spec.md` §6, §7, §28.
Decisions referenced as **D1**–**D9** are recorded in `docs/open-questions.md`.

---

## 1. What this document fixes

The specification describes a layered system but does not say which layer owns which
calculation, what may cross each boundary, or how §4.1 — the LLM must not generate numbers —
is enforced rather than merely asserted. This document fixes those, because they determine the
schema shapes and the module graph.

Three rules carry most of the weight:

1. **Numbers originate in one place.** Every figure the user sees is traceable to a single
   deterministic or simulated service. No layer above `domain/` performs arithmetic.
2. **Distributions travel as draws, not summaries** (D2). Marginal percentiles are computed
   once, at the end, by a shared summarizer.
3. **The write path is physically separate from the read path.** No analysis code can reach
   `publish_vehicle_price`.

---

## 2. Layer model

```text
┌──────────────────────────────────────────────────────────────────────┐
│ app.py          Entry point. Builds navigation from the registry.    │
│ src/workflows/  The dealer workflows, declared as data. Binds each   │
│                 to a view. No Streamlit import, no arithmetic.       │
│ src/views/      Streamlit render functions. Render. Never calculate. │
├──────────────────────────────────────────────────────────────────────┤
│ src/agents/     Intent routing, entity extraction, clarification.    │
│                 LLM lives here. Emits validated JSON only.           │
├──────────────────────────────────────────────────────────────────────┤
│ src/skills/     Skill orchestration. Sequences MCP calls and         │
│                 domain calls. Assembles result objects.              │
├──────────────────────────────────────────────────────────────────────┤
│ src/mcp_clients/   Typed adapters over MCP tools. Return DTOs with   │
│                    source, timestamp, version. No business logic.    │
├──────────────────────────────────────────────────────────────────────┤
│ src/domain/     All financial calculation. Pure functions.           │
│ src/simulation/ Seeded Monte Carlo. Returns draw matrices.           │
├──────────────────────────────────────────────────────────────────────┤
│ src/policy/     Warnings, floors, approvals, freshness. Runs last,   │
│                 over assembled results. Can force BLOCKING.          │
├──────────────────────────────────────────────────────────────────────┤
│ src/llm/        Explanation only. Consumes finished results.         │
└──────────────────────────────────────────────────────────────────────┘
```

### Dependency rule

```text
app.py → workflows → views → agents → skills → { mcp_clients, domain, simulation, policy }
domain, simulation → config only
llm → finished result objects only
```

`domain/` and `simulation/` import nothing from `agents/`, `skills/`, `mcp_clients/`, `llm/`,
`views/`, or `workflows/`. They never perform I/O and never call an MCP tool. Skills fetch
data and pass it in. This is what makes the calculation layer testable without mocks and
reusable across all three skills, as §28 requires.

The `workflows → views` edge runs one way only. The registry imports views to bind a render
callable; a view that imported the registry back would close a cycle, so the assistant home
receives its workflow cards as an argument. `tests/unit/test_views.py` asserts this.

---

## 2.1 Agent, workflow, skill

Three words that are easy to blur, kept distinct because the distinction is the product.

| | Owns | Count | In the navigation? |
| --- | --- | --- | --- |
| **Agent** | Reading a request in the dealer's words and choosing where it goes | 1 | Yes — the default entry point |
| **Workflow** | A job the dealer has. Sequences skills and frames the result | 4 | Yes — this is what the sidebar is made of |
| **Skill** | A reusable capability. Owns one analysis end to end | 3 | **No** — always reached through a workflow |

A workflow may use one skill or several; a skill may serve several workflows. **Improve
Aging Inventory is a workflow, not a fourth skill** — it coordinates all three against aged
units and adds no valuation, forecasting, or promotion arithmetic of its own. Making it a
skill would have meant duplicating logic that already exists three times over. Its
orchestration is implemented in Phase 5 (§3.6).

Navigation is declared in `src/pricing_agent/workflows/registry.py` as frozen dataclasses,
so what the product offers is one list rather than a set of filenames. The registry holds no
Streamlit import and no arithmetic; it binds a `WorkflowContext` into each view with
`functools.partial`, which is what lets one view serve more than one workflow without being
copied.

One capability the shell does not yet have, stated on screen rather than implied: Improve
Aging orchestration. Natural-language routing **is** connected as of Phase 4 — but
deterministically, with no model (§3.5).

---

## 3. LLM containment

§4.1 lists what the LLM may and may not produce. Four mechanisms enforce it.

### 3.1 Structural — no arithmetic above `domain/`

The agent and skill layers may compare, sort, filter, and select. They may not compute a
price, a duration, a cost, or a probability. Any new calculation belongs in `domain/`.

### 3.2 Import guard

An architecture test walks the AST of every module under `src/domain/` and `src/simulation/`
and asserts no import of `anthropic`, `openai`, or `src.llm`. This is the executable form of
§4.1: the calculation layer cannot call a model even by accident.

### 3.3 The extraction boundary

§4.2 requires validated JSON before any tool call. The agent's only numeric responsibility is
transcription — reading "$23,500" out of a sentence into `acquisition_cost`. Every extracted
field carries `source: USER_STATED | MCP | CONFIG | ESTIMATED` and a confidence. A field the
agent inferred rather than read is marked `ESTIMATED` and raises
`INSUFFICIENT_VEHICLE_DATA` when it feeds a floor calculation.

The agent may never emit a value for a field the domain layer computes. Request schemas
enforce this by omitting those fields entirely — there is no `recommended_price` property on
`single-vehicle-request.schema.json` for a model to populate.

### 3.4 The narration allow-list

Every result object carries an `explanation_inputs` block enumerating the computed values the
explanation layer is permitted to reference, each with its label, value, and units. The
explanation prompt receives that block and the warning list — not the raw result tree.

A post-generation check extracts every currency figure and duration from the narrative and
asserts each appears in `explanation_inputs`. A figure that does not match fails the response
rather than reaching the user. This closes the gap D1 leaves open, where a model might quote
the optimistic tail of a loss-bearing quantity.

### 3.5 Deterministic routing (Phase 4)

The assistant entry point routes natural language to a workflow and executes one skill
**without any model**. `src/pricing_agent/agents/` holds three deterministic modules:

* `router.py` — classifies intent by keyword precedence (aging cohort → promotion →
  forecast → pricing) and parses the vehicle a pricing request names, with per-field
  confidence. It produces **no number**: a `year` or `mileage` in its output was typed by
  the user and copied through, and `test_router.py` asserts the extracted entities appear
  verbatim in the input.
* `resolver.py` — matches the parsed vehicle against real inventory by documented priority
  (id → VIN → year+make+model+trim → year+make+model → make+model, each requiring a unique
  match). Ambiguous returns candidates; unmatched returns NONE — it never fabricates a
  vehicle.
* `assistant.py` — the orchestrator. Runs at most one skill, copies every summary figure
  from the schema-valid skill result, and returns one of six states.

This is a second, independent enforcement of §4.1 at the routing layer: the LLM
containment guarantee does not depend on the LLM being absent, but the deterministic router
happens not to use one at all. `test_assistant.py` makes a model call explode and asserts
the pricing path still executes. When an LLM-based router is added later, it will sit
*above* this layer — choosing a workflow, never producing a figure.

### 3.6 Improve Aging orchestration (Phase 5)

`src/pricing_agent/workflows/improve_aging.py` runs the three skills in dependency order and
consolidates their results. It is the strongest test of the "orchestrate, don't compute"
boundary, because it is where the temptation to combine figures is greatest.

```
1 PORTFOLIO_FORECAST        inventory_portfolio.analyze   sim_P    once
2 CANDIDATE_SELECTION        candidate_selection            —       ranks/filters sim_P + facts
3 SINGLE_VEHICLE_VALUATION   single_vehicle.analyze × k    sim_V1…k selected candidates only
4 PROMOTION_PLAN             promotion_planner.plan_event  sim_M    only if a real event resolves
5 CONSOLIDATE                improve_aging                  —       group actions, side-by-side figures
```

Three invariants, each tested:

* **No recalculation.** `candidate_selection` and `improve_aging` import neither `domain` nor
  `simulation`, and contain no `percentile`/`np.`/`simulate(` call. Selection reads the
  portfolio's `top_risk_vehicles` and `recommended_actions` and static inventory facts;
  consolidation reads whole skill-result objects.
* **Simulations stay separate.** The portfolio, each single vehicle, and the promotion plan
  are different probability spaces (`sim_P`, `sim_Vi`, `sim_M`). The engine never sums a
  percentile across them. Portfolio-level joint figures — ending inventory, target
  probability, joint gross/holding/depreciation impact — come from the recommended promotion
  plan's simulation alone, and are marked unavailable when no event ran. Per-vehicle evidence
  is shown side by side, each stamped with its own `request_id` and `simulation_id`.
* **The workflow's protection is authoritative.** The promotion skill has its own eligibility
  rules; if its raw plan selects a vehicle the workflow protected (recently acquired, high
  demand), the workflow holds it back (`held_from_promotion`) rather than promoting it.

Failure is a state, not a crash: if a single-vehicle or promotion call raises, completed
results are preserved, the gap is named, and the workflow returns `PARTIAL_RESULT`. The six
states are `ROUTED_AND_EXECUTED`, `NEEDS_CLARIFICATION`, `PARTIAL_RESULT`,
`TARGET_NOT_ACHIEVABLE`, `NO_SAFE_ACTIONS`, `EXECUTION_ERROR`. An event named but not on the
calendar ("July 4th") returns `NEEDS_CLARIFICATION` and is never substituted.

---

## 4. Request lifecycle

```text
 1. Intake            free text + dealer context
 2. Intent routing    → single-vehicle | portfolio | promotion        (§7.1)
 3. Extraction        → request JSON, schema-validated                (§4.2)
 4. Gap detection     missing / ambiguous / estimated fields
 5. Clarification     agent asks the user, or proceeds with flags
 6. Data retrieval    mcp_clients, in parallel where independent
 7. Freshness check   policy/freshness.py, per §21                    (D8)
 8. Calculation       domain + simulation, seeded                     (D2)
 9. Policy pass       warnings, floors, approvals                     (§19, §20, §22)
10. Result assembly   schema-validated result object + audit          (§23)
11. Explanation       llm/, constrained to explanation_inputs
12. Presentation      user reviews; any write requires confirmation   (§4.3)
```

Steps 8 and 9 are strictly ordered. Policy runs **over** finished numbers, never inside the
calculation — so a floor violation is always detectable after the fact and is never silently
absorbed by clamping a value mid-calculation.

---

## 5. Calculation ownership

Single source for each calculation. §28 forbids the portfolio and promotion skills from
reimplementing any of these.

| Calculation | Owner | Consumed by |
| --- | --- | --- |
| Vehicle identity normalization | `domain/vehicle.py` | all skills |
| Comparable selection and normalization | `domain/valuation.py` | single-vehicle, portfolio |
| Market-supported range, source reconciliation (D5) | `domain/valuation.py` | all skills |
| Sale-hazard draws, transaction-price draws | `simulation/` | all skills |
| Days-to-sale and sale-probability summaries | `domain/sales_forecast.py` | all skills |
| Depreciation per draw | `domain/depreciation.py` | all skills |
| Cash holding cost, slot opportunity cost (D3) | `domain/holding_cost.py` | all skills |
| Break-even, minimum safe prices | `domain/break_even.py` | all skills |
| Promotional headroom | `domain/promotion.py` | single-vehicle, promotion |
| Candidate scoring, plan construction | `domain/promotion.py` | promotion |
| Portfolio aggregation within draws | `domain/portfolio.py` | portfolio, promotion |
| Warning emission | `policy/warnings.py` | all skills |
| Floors and publication bars | `policy/price_floor.py` | all skills |

The promotion skill computes headroom by calling `domain/promotion.py`, which calls
`domain/break_even.py`, which calls `domain/holding_cost.py`. It does not re-derive a floor
from its own arithmetic. Portfolio-level figures are aggregations of vehicle-level draws, not
separate models.

---

## 6. The draw-matrix contract

The load-bearing interface of the system (D2).

```python
@dataclass(frozen=True)
class DrawMatrix:
    simulation_id: str          # identity for joint-combination checks
    seed: int
    draw_count: int
    model_label: str            # "CONFIGURABLE_PROTOTYPE_SIMULATION"  (§16.2)
    model_version: str
    assumption_version: str
    vehicle_ids: tuple[str, ...]
    # shape (draw_count, n_vehicles) each:
    days_to_sale: ndarray
    sold_within_horizon: ndarray
    transaction_price: ndarray
    cash_holding_cost: ndarray
    slot_opportunity_cost: ndarray
    depreciation_loss: ndarray
    front_end_gross: ndarray
    net_economic_value: ndarray
```

**Rules.**

* A single simulation call covers every vehicle in scope. Per-vehicle simulations are never
  stitched together, because that discards the shared market conditions that make portfolio
  outcomes correlated.
* Percentiles are produced only by `domain/summarize.py`, which stamps every distribution
  object with the originating `simulation_id`.
* Two distributions may be combined arithmetically only if their `simulation_id` values match.
  Combining across simulations raises rather than silently producing a §12.5 violation.
* Promotion plans re-simulate with modified prices under the **same seed**, so differences
  between plans reflect the price change and not sampling noise.
* Scenario comparisons within a single vehicle (§13.5's three strategies) likewise share a
  seed.

### Reproducibility

A result is reproducible from: `seed`, `draw_count`, `assumption_version`, `model_version`,
`config_version`, and the source data timestamps — all of which §23 already requires in audit.
Given the same inputs and the same versions, output is byte-identical.

---

## 7. Policy engine

`src/policy/` runs after assembly and may only add — warnings, approval requirements, and
publication bars. It never alters a computed number. If a recommended price violates a floor,
the price is reported unchanged alongside a `BLOCKING` warning; it is not quietly raised to
the floor.

This ordering is what makes §19.1 auditable: the record shows what the model recommended and
what policy did about it, as two separate facts.

`BLOCKING` is reserved exclusively for the §19.1 publication bars (D7). Every other severity,
including `CRITICAL`, permits the user to proceed with documentation.

---

## 8. Write-path isolation

§4.3 and §10.7 require that no price is published automatically.

* Read tools and write tools are separate client classes. Skills receive a read-only client.
* `publish_vehicle_price` and `save_pricing_decision` are reachable only from an explicit UI
  confirmation handler, never from a skill, agent, or explanation path.
* Publication requires: a `pricing_decision_id` from a prior save, a satisfied approval state,
  no unresolved `BLOCKING` warning, non-stale critical data (§21), and an idempotency key.
* The idempotency key is derived from `pricing_decision_id + final_price`, so a retry cannot
  double-publish and a changed price cannot reuse an approval.

---

## 9. Degradation model

Each dependency has a defined failure behavior. The system degrades with a warning rather than
failing closed, except where financial safety requires otherwise.

| Unavailable | Behavior | Warning |
| --- | --- | --- |
| External market position / recommendation | Fall back to internal valuation (D5 branch 4) | `EXTERNAL_VALUATION_UNAVAILABLE` |
| External comparables | Valuation proceeds on external reference price alone; confidence drops to LOW | `LOW_VALUATION_CONFIDENCE` |
| Shopper engagement (§9.8) | Drop the engagement term from the hazard model | reduced confidence, per §9.8 |
| Cost basis | **Hard stop.** No break-even, no floor, no recommendation | `INSUFFICIENT_VEHICLE_DATA` (BLOCKING) |
| Dealer capacity | Portfolio and promotion skills unavailable; single-vehicle proceeds | `INCOMPLETE_INVENTORY_DATA` |
| Event calendar | Promotion skill requires explicit user-supplied dates (§15.2) | — |
| Any critical source stale per §21 | Analysis proceeds, publication barred | `STALE_MARKET_DATA` (BLOCKING) |

Cost basis is the one hard stop: without it there is no floor, and a recommendation without a
floor is the specific failure §4.5 exists to prevent.

---

## 10. Clock injection

Every MCP client and every policy check takes an injected `as_of` timestamp (D8). Nothing in
`domain/`, `simulation/`, or `policy/` reads the wall clock. Fixtures declare their age
relative to `as_of`, which makes the §26.1 stale-data scenario deterministic and keeps the
other fixtures permanently fresh.

---

## 11. Module map

```text
src/
├── workflows/         registry (the four dealer workflows + the assistant), WorkflowContext,
│                      pages (url_path → live st.Page), and the Improve Aging orchestration
│                      (candidate_selection + improve_aging) — coordinates the three skills
├── views/             dashboard, vehicle_detail, promotion, assistant_home,
│                      improve_aging, workflow_copy, page_config — render only
├── agents/            deterministic router + resolver + assistant orchestrator (Phase 4),
│                      extraction, clarification, narration guard
├── skills/            one module per skill; orchestration only
├── mcp_clients/       market_data_client, cost_client, capacity_client, event_client
│                      (read-only) + write_client (isolated, §8)
├── domain/            vehicle, valuation, sales_forecast, depreciation,
│                      holding_cost, break_even, promotion, portfolio, summarize
├── policy/            warnings, price_floor, approvals, freshness
├── simulation/        hazard model, draw matrix, seeding
└── config/            loaders over config/assumptions/*.yaml
```

Two additions to the §25 tree: `domain/summarize.py`, which owns percentile production so the
`simulation_id` stamp cannot be bypassed, and `mcp_clients/write_client.py`, which enforces
§8. Comparable selection lives inside `domain/valuation.py` rather than in a separate module,
to stay closer to the specified tree.

`workflows/` and `views/` replaced the earlier Streamlit `pages/` directory. Filename-ordered
pages could not express that Improve Aging Inventory is a workflow over three skills, and
they made the navigation a property of the filesystem rather than a declaration.
`docs/workflow-navigation-results.md` records that migration.
