"""Transport shared by every mocked MCP client.

Implements the envelope, freshness classification, and error model in
docs/mcp-contract.md §1. Two properties matter beyond plumbing:

* **The clock is injected** (D8). Nothing here reads the wall clock, so the §26.1 stale
  scenario is produced by advancing `as_of` rather than editing a fixture, and every
  other fixture stays permanently fresh.
* **A missing number is absent, never zero.** A defaulted acquisition cost would produce
  a floor of zero and defeat §4.5, so absent data raises or returns NOT_FOUND.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from pricing_agent.config import Config, load_config
from pricing_agent.config.loader import MOCKS_DIR


class ToolStatus(str, Enum):
    OK = "OK"
    NOT_FOUND = "NOT_FOUND"
    UNAUTHORIZED = "UNAUTHORIZED"
    UNAVAILABLE = "UNAVAILABLE"
    PARTIAL = "PARTIAL"


@dataclass(frozen=True)
class SourceMeta:
    source: str
    data_timestamp: datetime
    source_version: str
    confidence: str = "UNKNOWN"
    coverage: float = 1.0
    freshness_class: str = "DAILY"
    age_hours: float = 0.0
    is_stale: bool = False


@dataclass(frozen=True)
class ToolResponse:
    tool: str
    status: ToolStatus
    data: Any
    meta: SourceMeta | None = None

    @property
    def ok(self) -> bool:
        return self.status in (ToolStatus.OK, ToolStatus.PARTIAL)

    def require(self) -> Any:
        """Return data, or raise. For dependencies with no degradation path."""
        if not self.ok:
            raise ToolUnavailable(f"{self.tool} returned {self.status.value}")
        return self.data


class ToolUnavailable(RuntimeError):
    """Raised when a hard dependency cannot be satisfied.

    Cost basis is the only hard stop in the system: without it there is no floor, and a
    recommendation without a floor is the failure §4.5 exists to prevent.
    """


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --- fixture store --------------------------------------------------------------------

# Logical namespace -> (fixture file, path to the collection inside `data`)
_NAMESPACES: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "inventory": ("inventory/dealer-1001-inventory.json", ()),
    "capacity": ("inventory/capacity.json", ()),
    "inbound": ("inventory/inbound.json", ()),
    "cost_basis": ("dealer_costs/cost-basis.json", ("cost_basis",)),
    "policy": ("dealer_costs/pricing-policy.json", ()),
    "events": ("events/event-calendar.json", ()),
    "positions": ("market_data/market-position.json", ("positions",)),
    "recommendations": ("market_data/pricing-recommendation.json", ("recommendations",)),
    "comparables": ("market_data/comparables.json", ("comparables",)),
    "velocity": ("market_data/sales-velocity.json", ()),
    "engagement": ("market_data/shopper-engagement.json", ("engagement",)),
    "history": ("market_data/vehicle-history.json", ()),
    "sales_history": ("market_data/dealer-sales-history.json", ()),
}

_ID_FIELDS = ("vehicle_id", "event_id", "inbound_id", "listing_id")


@dataclass
class Mutation:
    """A scenario mutation from tests/scenarios/*.json.

    Applied to loaded fixture data, never to the files, so scenarios never depend on
    editing shared fixtures in place.
    """

    op: str  # set | remove | append | fail_tool
    path: str | None = None
    value: Any = None
    tool: str | None = None
    status: str = "UNAVAILABLE"


class FixtureStore:
    """Loads mock fixtures once, applies mutations, and serves namespaces."""

    def __init__(
        self,
        mocks_dir: Path | None = None,
        mutations: Iterable[Mutation] = (),
    ) -> None:
        self._dir = mocks_dir or MOCKS_DIR
        self._cache: dict[str, dict] = {}
        self.failures: dict[str, ToolStatus] = {}

        for mutation in mutations:
            self._apply(mutation)

    def _load_file(self, relpath: str) -> dict:
        if relpath not in self._cache:
            path = self._dir / relpath
            self._cache[relpath] = json.loads(path.read_text(encoding="utf-8"))
        return self._cache[relpath]

    def envelope(self, namespace: str) -> dict:
        relpath, _ = _NAMESPACES[namespace]
        return self._load_file(relpath)

    def collection(self, namespace: str) -> Any:
        relpath, inner = _NAMESPACES[namespace]
        node = self._load_file(relpath)["data"]
        for key in inner:
            node = node[key]
        return node

    # --- mutation ---------------------------------------------------------------------

    def _apply(self, mutation: Mutation) -> None:
        if mutation.op == "fail_tool":
            if mutation.tool:
                self.failures[mutation.tool] = ToolStatus(mutation.status)
            return

        if not mutation.path:
            raise ValueError(f"Mutation {mutation.op} requires a path")

        head, *rest = mutation.path.split(".")
        if head not in _NAMESPACES:
            # Paths like `request.proposed_price` address the caller, not a fixture.
            return

        node = self.collection(head)
        parent, key = self._resolve(node, rest)

        if mutation.op == "set":
            self._assign(parent, key, mutation.value)
        elif mutation.op == "remove":
            self._delete(parent, key)
        elif mutation.op == "append":
            target = self._read(parent, key) if key is not None else parent
            if not isinstance(target, list):
                raise TypeError(f"Cannot append to {mutation.path}")
            target.append(mutation.value)
        else:
            raise ValueError(f"Unknown mutation op: {mutation.op}")

    def _resolve(self, node: Any, segments: list[str]) -> tuple[Any, Any]:
        """Walk to the parent of the final segment, returning (parent, key)."""
        if not segments:
            return node, None
        for segment in segments[:-1]:
            node = self._read(node, segment)
        return node, segments[-1]

    @staticmethod
    def _read(node: Any, key: str) -> Any:
        if isinstance(node, dict):
            if key in node:
                return node[key]
            raise KeyError(key)
        if isinstance(node, list):
            for item in node:
                if isinstance(item, dict) and any(
                    item.get(f) == key for f in _ID_FIELDS
                ):
                    return item
            raise KeyError(key)
        raise TypeError(f"Cannot index {type(node).__name__} with {key!r}")

    def _assign(self, parent: Any, key: Any, value: Any) -> None:
        if key is None:
            raise ValueError("Cannot set a namespace root")
        if isinstance(parent, dict):
            parent[key] = value
            return
        if isinstance(parent, list):
            self._read(parent, key)  # raises if absent
            for item in parent:
                if isinstance(item, dict) and any(
                    item.get(f) == key for f in _ID_FIELDS
                ):
                    item.clear()
                    item.update(value)
                    return
        raise TypeError("Unsupported assignment target")

    def _delete(self, parent: Any, key: Any) -> None:
        if isinstance(parent, dict):
            parent.pop(key, None)
            return
        if isinstance(parent, list):
            for index, item in enumerate(parent):
                if isinstance(item, dict) and any(
                    item.get(f) == key for f in _ID_FIELDS
                ):
                    parent.pop(index)
                    return
            return
        raise TypeError("Unsupported deletion target")


# --- transport ------------------------------------------------------------------------


@dataclass
class CallRecord:
    """One entry of the §23 `mcp_tools_called` audit list."""

    tool: str
    called_at: datetime
    status: str
    source_version: str | None = None
    data_timestamp: datetime | None = None
    coverage: float | None = None

    def as_audit(self) -> dict:
        return {
            "tool": self.tool,
            "called_at": self.called_at.isoformat(),
            "status": self.status,
            **({"source_version": self.source_version} if self.source_version else {}),
            **(
                {"data_timestamp": self.data_timestamp.isoformat()}
                if self.data_timestamp
                else {}
            ),
            **({"coverage": self.coverage} if self.coverage is not None else {}),
        }


class MockTransport:
    """Serves fixture data with a proper envelope, freshness, and a call log."""

    def __init__(
        self,
        as_of: datetime,
        store: FixtureStore | None = None,
        config: Config | None = None,
    ) -> None:
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        self.as_of = as_of
        self.store = store or FixtureStore()
        self.config = config or load_config()
        self.calls: list[CallRecord] = []

    # --- freshness --------------------------------------------------------------------

    def _freshness(self, tool: str, data_timestamp: datetime) -> tuple[str, float, bool]:
        classes = self.config.freshness["classes"]
        tool_classes = self.config.freshness["tool_classes"]
        name = tool_classes.get(tool, "DAILY")
        max_age = classes.get(name, {}).get("max_age_hours")

        age_hours = (self.as_of - data_timestamp).total_seconds() / 3600.0
        is_stale = max_age is not None and age_hours > float(max_age)
        return name, age_hours, is_stale

    def blocks_publication_when_stale(self, freshness_class: str) -> bool:
        gate = self.config.freshness.get("publication_gate", {})
        return freshness_class in gate.get("block_on_stale_classes", [])

    # --- fetch ------------------------------------------------------------------------

    def fetch(self, tool: str, namespace: str) -> ToolResponse:
        forced = self.store.failures.get(tool)
        if forced is not None:
            self._record(CallRecord(tool, self.as_of, forced.value))
            return ToolResponse(tool=tool, status=forced, data=None)

        envelope = self.store.envelope(namespace)
        raw_meta = envelope["meta"]
        timestamp = parse_timestamp(raw_meta["data_timestamp"])
        freshness_class, age_hours, is_stale = self._freshness(tool, timestamp)

        meta = SourceMeta(
            source=raw_meta["source"],
            data_timestamp=timestamp,
            source_version=raw_meta["source_version"],
            confidence=raw_meta.get("confidence", "UNKNOWN"),
            coverage=float(raw_meta.get("coverage", 1.0)),
            freshness_class=freshness_class,
            age_hours=age_hours,
            is_stale=is_stale,
        )
        status = ToolStatus.PARTIAL if meta.coverage < 1.0 else ToolStatus.OK

        self._record(
            CallRecord(
                tool=tool,
                called_at=self.as_of,
                status=status.value,
                source_version=meta.source_version,
                data_timestamp=timestamp,
                coverage=meta.coverage,
            )
        )
        return ToolResponse(
            tool=tool, status=status, data=self.store.collection(namespace), meta=meta
        )

    def _record(self, record: CallRecord) -> None:
        self.calls.append(record)

    def audit_calls(self) -> list[dict]:
        return [c.as_audit() for c in self.calls]
