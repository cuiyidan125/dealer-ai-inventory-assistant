# Demo Script

Seven beats, about six minutes. Written for a used-vehicle manager or general manager —
lead with dollars and days, not architecture.

**Before you start:** `streamlit run app.py`, open http://localhost:8501, and click
through once so Streamlit's caches are warm. A cold first load runs the whole lot and
takes a few seconds.

The sidebar is organised by the dealer's job, not by ours: **Ask the Dealer AI Assistant**
on top, then **Acquire / Price / Merchandise / Improve Aging Inventory**. If someone asks
where the three skills are, that is the right question — they sit underneath the
workflows, and a workflow can use more than one.

The language is dealer-first: labels lead with the business meaning and put the statistic in
parentheses ("Expected days to sale (P50)"). If an interviewer asks what P50/P90 mean, open the
**How to read these estimates** expander on any page — expected (P50), conservative (P90),
downside (P10), stated plainly.

---

## Beat 0 — Ask the assistant (45s)

The app lands on **Ask the Dealer AI Assistant**. Type — or click the suggested prompt —
**"What should I price 2020 Ford F-150 XLT?"** and hit **Analyze**.

> "A question in the manager's own words. It figured out this is a pricing job, matched the
> truck to the one in inventory, and ran the valuation — recommended price, days to sell,
> break-even, all of it."

Then the honest part, which is the point:

> "No AI wrote those numbers. The routing is deterministic — rules, not a model — and every
> figure comes straight from the pricing engine. The assistant chooses *which* analysis to
> run; it never does the arithmetic."

Open the **How this was routed** expander to show the detected workflow, the confidence,
and the reason codes. Then click **Open the full Price Inventory analysis** — the workspace
opens on that exact truck.

If asked what it can't do yet: name a vehicle that isn't on the lot ("a Tesla Model 3") and
it says so plainly rather than inventing one; ask about aging inventory and it routes but
tells you the orchestration isn't built. **Don't mime a conversation it can't have.**

---

## Beat 1 — Open on the lot (45s)

Sidebar → **Acquire Inventory**.

> "This is a twelve-car lot on a Tuesday morning. Eighty-six percent full, two open
> slots, three units past ninety days, and three advertised below what they cost."

Point at the two banners:

- `HIGH_PERCENTAGE_BELOW_BREAK_EVEN` — three cars book a loss on sale
- `INBOUND_CAPACITY_CONFLICT` — committed inbound exceeds available slots by one

> "It hasn't recommended anything yet. It's told me what I'd have found out on Friday."

The **Lot** tab is sorted by risk, and risk weights cost basis — so a $45,000 problem
outranks a $9,000 one. That ordering is the point; say so.

---

## Beat 2 — Price a car (75s)

Sidebar → **Price Inventory** → `V-10001 · 2022 Toyota RAV4`.

Open **Ask in plain English** and hit **Extract**.

> "I typed what I'd say out loud. It came back as validated JSON, and look at what
> isn't in it — no price, no valuation, no days-to-sale. Those fields don't exist in
> the extraction schema. The model transcribes; it doesn't decide."

Point at the provenance table: every field marked `USER_STATED` or `MISSING`. Nothing
guessed.

Then the recommendation: **$29,195**, market value $28,400, P50 gross $2,597, 30 days to
sell.

Scroll to **Gross against turn**:

> "Three strategies, one simulation, same seed — so the difference between them is the
> price change, not noise. Maximize Gross wins here: giving up $2,200 of gross to sell
> eleven days sooner doesn't pay on a car this fresh."

---

## Beat 3 — The one that matters (90s)

Still in **Price Inventory**, switch the vehicle to `V-10005 · 2018 BMW 540i · 108d`.

Let the red banner land before saying anything.

> "Break-even is $28,963. The market is $24,900. This car cannot be sold at a profit
> today, and the system won't publish a price for it."

Walk the three `BLOCKING` warnings. Note that each carries the observed value **and** the
threshold — the margin by which the rule was missed, not just that it was missed.

> "Note what it didn't do. It didn't quietly raise the price to the floor to make the
> arithmetic work. It shows what the model recommended and what policy did about it, as
> two separate facts. A car priced into a vacuum above the market doesn't sell — it just
> ages while the loss grows."

Point at **Maximum safe discount: $0** and the required `Loss Minimization` approval.

> "The system quantifies both sides — the loss now against the modelled cost of holding.
> It doesn't decide. That's the manager's call, and it needs a signature."

**This is the beat the demo is for.** A tool that only ever says "price it here" is a
calculator.

---

## Beat 4 — Work the whole lot (75s)

Sidebar → **Merchandise Inventory**. Event: **Summer Clearance** (2026-08-17 → 08-21).
Target: **70%**.

> "I want the lot at seventy percent by the end of the August event."

Verdict first: **At Risk** — it hits the target about **43%** of the time.

> "It needs two incremental sales, and the most aggressive safe plan gets there just under
> half the time. It's not a no, but it's a coin-flip — and it tells me exactly how to
> improve the odds."

Show the quantified alternatives: extend the campaign, revise the target, or wholesale a
couple of units.

Then the **Excluded** tab — this is the sharpest observation in the demo:

> "Several cars are excluded for `NO_SAFE_HEADROOM`, and they're the aged ones. The cars I
> most want to move are exactly the ones I can't legally discount, because they're
> already at or below their floor. That tension is real, and most tools hide it."

Drag the target to **85%** to flip the verdict to **Not Achievable** and watch the quantified
alternatives appear — the strongest honesty moment in the demo.

---

## Beat 4.5 — Let the assistant coordinate all three (75s)

Back to **Ask the Dealer AI Assistant**. Type **"Reduce my inventory utilization to 70%
during the Summer Clearance event."** and **Analyze**, then open the **Improve Aging
Inventory** workspace.

> "This is one question that needed three skills. The assistant diagnosed the lot, picked
> the aging cars worth acting on, priced each one, and built the event plan — in that order,
> and it shows the order."

**The assistant answers in the conversation first.** Before you ever open the workspace, the
reply names the actual vehicles — which need immediate action, with a one-line reason each, and
which are only sale-event candidates — and states plainly when no event is selected that
"promotion eligibility is not finalized." Review is shown as a **vehicle count** ("5 vehicles
require review"); the raw per-condition record count lives under **View approval details**, so
no one reads it as five-plus separate approvals. The workspace remains the place for the full
evidence, plan comparison, and audit trail.

**Then keep talking to it.** The reply is the start of a thread, not a dead end. Ask
*"why is the BMW recommended for wholesale?"* — it answers from the existing result (days on lot,
break-even, the price floor), no re-run. Ask *"show only vehicles over 90 days"* — it filters the
list in place. Say *"use Summer Clearance"* — it **re-runs the deterministic workflow** with the
event, shows which vehicles become promotion candidates and the target likelihood, and keeps the
previous answer until the new one succeeds. Every turn stays in view, and *"open the updated
workspace"* carries the conversation's context across. If you ask for something the prototype
doesn't have — shopper views, lead conversion — it says so rather than inventing a number, and it
never publishes a price.

**And it knows when you've changed the subject.** Ask *"what should I price the 2021 Honda Accord
EX?"* mid-conversation and it **switches to Single Vehicle Valuation** — a visible "Switching from
Improve Aging Inventory to Single Vehicle Valuation" banner, then the normal valuation — because
the *action* you asked for changed, not just the vehicle you named. The aging conversation stays
in the thread above, so nothing is lost. By contrast *"why does the Accord require manager
review?"* stays an aging follow-up: the vehicle name alone never triggers a switch, the business
action does.

**Open on the executive summary — read it out in ten seconds:**

> "Eighty-six percent full, target seventy, two cars to release, seven candidates — and an
> honest headline: the target is *at risk*, about forty-three percent likely. The recommended
> plan is Capacity First, and every number traces to a simulation, not a guess."

Then **"What should I do next?"** — a prioritized list a GM could act on this morning:
review the two manager-approval cars, run a loss-minimization review on the three deep-aged
units, approve the promotion for the two with safe headroom, and hold price on the protected
three.

> "If I tighten the target — say to sixty percent — the same workspace flips to *not
> achievable* and shows the gap and exactly what would close it: extend the window, revise
> the target, or wholesale a couple of units."

Then walk the rest top to bottom:

- **Candidate ranking** — seven aging cars selected, each with *why*: over 90 days, high
  depreciation, duplicate inventory. Five excluded, each with *why not* — and two are
  **protected**: recently acquired, held back from any discount even though the promotion
  skill would have been willing to promote them.
- **Per-vehicle evidence** — "Every row is a separate simulation. Look at the `sim_id`
  column: they're all different. The workflow shows them side by side. It never adds a P50
  from one simulation to a P50 from another — that would be inventing a number."
- **Execution trace** — "Every step, every skill, every request and simulation id, in order.
  If someone asks *how did it decide*, this is the answer."

> "It's still honest about the target — seventy percent isn't reachable safely, and it says
> so. But now it's coordinated the whole response, not just answered one question."

**This is the beat that shows the architecture.** Improve Aging isn't a fourth skill — it's
the three you already saw, run in order and consolidated.

---

## Beat 5 — Show the receipts (30s)

Back to **Price Inventory** → **Assumptions and audit trail**.

> "Every MCP call with its timestamp, the simulation seed, the assumption version, the
> percentile convention."

Then the last table:

> "And this is the only thing the explanation layer is allowed to quote from. If the model
> writes a number that isn't in this list, the response is thrown away rather than shown
> to you."

---

## Questions you should expect

**"Are these numbers real?"**
No. Synthetic dealer, synthetic market. The forecast is a configured simulation labelled
as one, not a trained model. The architecture is the deliverable; the numbers demonstrate
it.

**"Where does the market value come from?"**
External market data anchors it. An independent comparable-based estimate runs alongside every time, and
disagreement widens the range and lowers confidence — it never silently averages the two.

**"What's the least trustworthy part?"**
Price elasticity. It alone decides every velocity-versus-gross tradeoff and it isn't
calibrated. It's one file, and it's labelled.

**"What does Improve Aging Inventory do?"**
It coordinates all three skills against the aged cohort — forecast, then per-vehicle
pricing, then an event plan — and consolidates them into one action list with an execution
trace. It is a *workflow over the three skills*, not a fourth skill: it adds no arithmetic,
and it keeps each skill's simulation separate rather than blending percentiles. Beat 4.5
walks it.

**"Is the assistant using ChatGPT / an LLM?"**
No. Phase 4 routing is deterministic — keyword rules for intent, pattern matching for the
vehicle, a lookup against inventory. It runs one skill and copies the skill's numbers.
There is a test that makes any model call throw and proves the pricing path still works.
An LLM-based router is a later phase, and it will only ever choose the workflow, never
produce a figure.

**"Could the AI just make up a price?"**
Two independent guards, both tested. The calculation layer can't import a model, and any
figure in generated prose that the engine didn't produce gets the response discarded. Show
the guard test if they want it.
