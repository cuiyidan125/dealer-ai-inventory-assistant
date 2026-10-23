"""Market-data MCP adapter. Read-only, no business logic.

Contracts in docs/mcp-contract.md §2. Per §8.1 these are a *proposed* integration
surface, not an observed API; everything here is served from mocks/.

Comparable normalization deliberately does NOT happen in this client — it belongs to
domain/valuation.py, so the same rules apply regardless of where comparables came from.
"""

from __future__ import annotations

from pricing_agent.mcp_clients.base import MockTransport, ToolResponse, ToolStatus


class MarketDataClient:
    def __init__(self, transport: MockTransport) -> None:
        self._t = transport

    # --- inventory --------------------------------------------------------------------

    def get_dealer_inventory(self, dealer_id: str = "DEALER-1001") -> ToolResponse:
        response = self._t.fetch("get_dealer_inventory", "inventory")
        if not response.ok:
            return response
        return ToolResponse(
            tool=response.tool,
            status=response.status,
            data=response.data["vehicles"],
            meta=response.meta,
        )

    def get_vehicle_inventory_age(self, vehicle_id: str) -> ToolResponse:
        return self._keyed("get_vehicle_inventory_age", "history", vehicle_id, "inventory_age")

    def get_vehicle_price_history(self, vehicle_id: str) -> ToolResponse:
        return self._keyed("get_vehicle_price_history", "history", vehicle_id, "price_history")

    # --- market -----------------------------------------------------------------------

    def get_vehicle_market_position(self, vehicle_id: str) -> ToolResponse:
        return self._keyed("get_vehicle_market_position", "positions", vehicle_id)

    def get_vehicle_pricing_recommendation(self, vehicle_id: str) -> ToolResponse:
        return self._keyed("get_vehicle_pricing_recommendation", "recommendations", vehicle_id)

    def get_vehicle_comparables(self, vehicle_id: str) -> ToolResponse:
        response = self._keyed("get_vehicle_comparables", "comparables", vehicle_id)
        if response.ok and response.data is None:
            return ToolResponse(response.tool, ToolStatus.NOT_FOUND, [], response.meta)
        return response

    def get_market_sales_velocity(self, segment: str) -> ToolResponse:
        response = self._t.fetch("get_market_sales_velocity", "velocity")
        if not response.ok:
            return response
        by_segment = response.data.get("by_segment", {})
        entry = by_segment.get(segment)
        if entry is None:
            return ToolResponse(response.tool, ToolStatus.NOT_FOUND, None, response.meta)
        enriched = dict(entry)
        enriched["seasonal_indicators"] = response.data.get("seasonal_indicators", {})
        enriched["market_radius_miles"] = response.data.get("market_radius_miles")
        return ToolResponse(response.tool, response.status, enriched, response.meta)

    def get_shopper_engagement(self, vehicle_id: str) -> ToolResponse:
        """Optional per §9.8. Absence is normal and must not fail a calculation.

        Several fixture vehicles have no entry on purpose: the hazard model must drop
        the engagement term rather than substitute a default multiplier.
        """
        response = self._t.fetch("get_shopper_engagement", "engagement")
        if not response.ok:
            return response
        entry = response.data.get(vehicle_id)
        if entry is None:
            return ToolResponse(response.tool, ToolStatus.NOT_FOUND, None, response.meta)
        enriched = dict(entry)
        envelope = self._t.store.envelope("engagement")["data"]
        enriched["observation_window_days"] = envelope.get("observation_window_days", 30)
        return ToolResponse(response.tool, response.status, enriched, response.meta)

    def get_dealer_sales_history(self, dealer_id: str = "DEALER-1001") -> ToolResponse:
        response = self._t.fetch("get_dealer_sales_history", "sales_history")
        if not response.ok:
            return response
        return ToolResponse(
            response.tool, response.status, response.data.get("sales", []), response.meta
        )

    # --- helpers ----------------------------------------------------------------------

    def _keyed(
        self, tool: str, namespace: str, key: str, inner: str | None = None
    ) -> ToolResponse:
        response = self._t.fetch(tool, namespace)
        if not response.ok:
            return response
        node = response.data
        if inner is not None:
            node = node.get(inner, {})
        entry = node.get(key) if isinstance(node, dict) else None
        if entry is None:
            return ToolResponse(response.tool, ToolStatus.NOT_FOUND, None, response.meta)
        return ToolResponse(response.tool, response.status, entry, response.meta)
