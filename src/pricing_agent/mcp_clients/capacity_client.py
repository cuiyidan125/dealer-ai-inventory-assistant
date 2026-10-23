"""Dealer capacity and inbound inventory. docs/mcp-contract.md §3.2 and §3.4.

Returns primitives only. `physical_open_slots`, `effective_open_slots`, and the
utilization figures are derived in domain/portfolio.py — a returned derived field that
disagreed with the primitives would be unresolvable.
"""

from __future__ import annotations

from pricing_agent.mcp_clients.base import MockTransport, ToolResponse


class CapacityClient:
    def __init__(self, transport: MockTransport) -> None:
        self._t = transport

    def get_dealer_capacity(self, dealer_id: str = "DEALER-1001") -> ToolResponse:
        return self._t.fetch("get_dealer_capacity", "capacity")

    def get_inbound_inventory(self, dealer_id: str = "DEALER-1001") -> ToolResponse:
        """Inbound units.

        Only `committed_slot: true` units count as confirmed_inbound, which is what keeps
        this tool consistent with get_dealer_capacity. D6 defines
        `reserved_slots ⊇ confirmed_inbound`, so callers deduct one or the other in the
        capacity flow — never both.
        """
        response = self._t.fetch("get_inbound_inventory", "inbound")
        if not response.ok:
            return response
        return ToolResponse(
            response.tool, response.status, response.data.get("vehicles", []), response.meta
        )
