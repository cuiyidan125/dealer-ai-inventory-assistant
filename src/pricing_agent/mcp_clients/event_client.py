"""Sale event calendar. docs/mcp-contract.md §3.3.

`historical_demand_lift` is nullable by design. §26.3 requires both an event with
validated lift and one without; a null forces the configured default and raises
LOW_EXPECTED_EVENT_LIFT.
"""

from __future__ import annotations

from pricing_agent.mcp_clients.base import MockTransport, ToolResponse, ToolStatus


class EventClient:
    def __init__(self, transport: MockTransport) -> None:
        self._t = transport

    def get_sales_event_calendar(self, dealer_id: str = "DEALER-1001") -> ToolResponse:
        response = self._t.fetch("get_sales_event_calendar", "events")
        if not response.ok:
            return response
        return ToolResponse(
            response.tool, response.status, response.data.get("events", []), response.meta
        )

    def get_event(self, event_id: str) -> ToolResponse:
        response = self.get_sales_event_calendar()
        if not response.ok:
            return response
        for event in response.data:
            if event.get("event_id") == event_id:
                return ToolResponse(response.tool, response.status, event, response.meta)
        return ToolResponse(response.tool, ToolStatus.NOT_FOUND, None, response.meta)
