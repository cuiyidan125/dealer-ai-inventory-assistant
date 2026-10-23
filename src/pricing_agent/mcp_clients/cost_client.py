"""Internal dealer-systems adapters: cost basis and pricing policy.

docs/mcp-contract.md §3.1 and §3.5.

`get_dealer_pricing_policy` is a proposed addition, not in product-spec.md §10. §11.9 and
§11.10 make the policy floor and risk floor two of the four inputs to minimum safe
transaction price — the central financial-safety control — and no specified tool returns
them (docs/open-questions.md C1). Both clients live in one module because both are
internal dealer systems rather than the market-data provider.
"""

from __future__ import annotations

from pricing_agent.mcp_clients.base import MockTransport, ToolResponse, ToolStatus


class CostClient:
    """Cost basis. The one hard dependency in the system."""

    def __init__(self, transport: MockTransport) -> None:
        self._t = transport

    def get_vehicle_cost_basis(self, vehicle_id: str) -> ToolResponse:
        response = self._t.fetch("get_vehicle_cost_basis", "cost_basis")
        if not response.ok:
            return response
        entry = response.data.get(vehicle_id)
        if entry is None:
            # Absent, never defaulted to zero: a zero acquisition cost would produce a
            # floor of zero and defeat §4.5.
            return ToolResponse(response.tool, ToolStatus.NOT_FOUND, None, response.meta)
        return ToolResponse(response.tool, response.status, entry, response.meta)


class PolicyClient:
    def __init__(self, transport: MockTransport) -> None:
        self._t = transport

    def get_dealer_pricing_policy(self, dealer_id: str = "DEALER-1001") -> ToolResponse:
        return self._t.fetch("get_dealer_pricing_policy", "policy")
