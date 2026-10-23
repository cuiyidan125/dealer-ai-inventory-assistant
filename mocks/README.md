# Mock MCP Data

Deterministic fixtures backing the mocked MCP clients. No network, no randomness.

## The clock

Every fixture declares its `data_timestamp` **relative to the scenario's injected `as_of`**,
not to the wall clock (D8). The anchor is:

```
as_of = 2026-07-29T14:00:00Z
```

Fixtures use absolute timestamps computed from that anchor. A scenario that needs stale data
(`SV-09`) overrides `as_of` forward rather than editing fixtures, so staleness is deliberate
and reproducible instead of an artifact of when the suite happens to run.

The whole clock was translated forward from `2026-07-21` to `2026-07-29` when the Summer
Clearance demo window moved to `2026-08-17`–`08-21`; every fixture `data_timestamp` moved by
the same +8 days, so freshness relationships are unchanged.

## Dataset shape

A deliberately small dealer — **12 active units, 14 physical slots** — rather than the 184
units in the §9.1 example. Small enough to reason about by hand, sized so that utilization
sits just above target and the capacity scenarios are reachable.

Every vehicle exists to make at least one §26 scenario reachable:

| Vehicle | Purpose |
| --- | --- |
| `V-10001` 2022 RAV4 XLE | High-confidence baseline, strong comparables |
| `V-10002` 2021 Accord EX | Poor-deal current price, overpriced |
| `V-10003` 2020 F-150 XLT | P50 under 90 days, P90 over 90 |
| `V-10004` 2019 Altima SV | Both P50 and P90 over 90 days; aged |
| `V-10005` 2018 BMW 540i | Break-even above market value — underwater |
| `V-10006` 2023 Bolt EV | High depreciation, EV |
| `V-10007` 2022 RAV4 XLE | Duplicate of `V-10001` — cannibalization |
| `V-10008` 2021 Outback | Large safe headroom |
| `V-10009` 2017 Ram 1500 | Insufficient comparables |
| `V-10010` 2022 Telluride | Stale market-data target |
| `V-10011` 2020 Camry LE | Promotional discount would breach the floor |
| `V-10012` 2019 Wrangler | 130 days — wholesale and loss-minimization candidate |

## Coverage note

`market-position`, `cost-basis`, `inventory-age`, and `price-history` cover all 12 vehicles.
`comparables` covers all 12, with deliberately thin sets for `V-10009` (2 listings, triggers
`INSUFFICIENT_COMPARABLES`) and moderate sets elsewhere. `shopper-engagement` deliberately
**omits** several vehicles, exercising the §9.8 degradation path.

Nothing here is real market data.
