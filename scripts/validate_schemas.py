#!/usr/bin/env python3
"""Validate every JSON Schema and every fixture. Implements product-spec.md §28 step 9.

Run:
    python scripts/validate_schemas.py

Checks, in order:
  1. Every file in schemas/ is syntactically valid JSON.
  2. Every schema compiles under JSON Schema draft 2020-12.
  3. Every $ref resolves within the local schema set.
  4. Every mock fixture is valid JSON and carries a well-formed `meta` envelope.
  5. Fixture cross-references are consistent (vehicle ids, capacity relationships).
  6. Scenario files reference vehicles and warning codes that actually exist.

Exit code 0 means the schema set and fixtures are internally consistent, which is the
§28 step 10 precondition for starting UI work.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
except ImportError:
    sys.exit("Missing dependencies. Run: pip install -r requirements.txt")

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schemas"
MOCK_DIR = ROOT / "mocks"
SCENARIO_DIR = ROOT / "tests" / "scenarios"

BASE_URI = "https://pricing-demo.local/schemas/"

errors: list[str] = []
checks = 0


def fail(msg: str) -> None:
    errors.append(msg)


def ok(msg: str) -> None:
    global checks
    checks += 1
    print(f"  ok  {msg}")


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"{path.relative_to(ROOT)}: invalid JSON at line {exc.lineno}: {exc.msg}")
        return None


# --- 1 & 2: schemas parse and compile -------------------------------------------------

print("\nSchemas")
schemas: dict[str, dict] = {}
for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
    doc = load_json(path)
    if doc is None:
        continue
    schemas[path.name] = doc
    if doc.get("$id") != f"{BASE_URI}{path.name}":
        fail(f"{path.name}: $id should be {BASE_URI}{path.name}, got {doc.get('$id')!r}")

registry = Registry().with_resources(
    [(f"{BASE_URI}{name}", Resource.from_contents(doc)) for name, doc in schemas.items()]
)

for name, doc in schemas.items():
    try:
        Draft202012Validator.check_schema(doc)
    except Exception as exc:  # noqa: BLE001
        fail(f"{name}: does not compile: {exc}")
        continue
    ok(f"{name} compiles")


# --- 3: refs resolve ------------------------------------------------------------------

print("\nReferences")


def iter_refs(node, path="$"):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value, path
            else:
                yield from iter_refs(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from iter_refs(item, f"{path}[{i}]")


for name, doc in schemas.items():
    for ref, where in iter_refs(doc):
        if ref.startswith("#"):
            continue
        target, _, fragment = ref.partition("#")
        if target not in schemas:
            fail(f"{name} at {where}: $ref to unknown schema {target!r}")
            continue
        if fragment.startswith("/$defs/"):
            defname = fragment.split("/")[-1]
            if defname not in schemas[target].get("$defs", {}):
                fail(f"{name} at {where}: {target} has no $defs/{defname}")
    ok(f"{name} references resolve")


# --- 4: fixtures ----------------------------------------------------------------------

print("\nFixtures")
REQUIRED_META = {"source", "data_timestamp", "source_version"}
fixtures: dict[str, dict] = {}

for path in sorted(MOCK_DIR.rglob("*.json")):
    doc = load_json(path)
    if doc is None:
        continue
    rel = str(path.relative_to(MOCK_DIR)).replace("\\", "/")
    fixtures[rel] = doc
    if rel.startswith("llm/"):
        # Recorded model responses, not MCP tool responses: no provenance envelope.
        ok(f"mocks/{rel} parses (recorded model response)")
        continue
    meta = doc.get("meta")
    if not isinstance(meta, dict):
        fail(f"mocks/{rel}: missing `meta` envelope")
        continue
    missing = REQUIRED_META - meta.keys()
    if missing:
        fail(f"mocks/{rel}: meta missing {sorted(missing)}")
        continue
    ok(f"mocks/{rel} envelope")


# --- 5: fixture cross-consistency -----------------------------------------------------

print("\nFixture consistency")
inv = fixtures.get("inventory/dealer-1001-inventory.json", {}).get("data", {})
vehicle_ids = {v["vehicle_id"] for v in inv.get("vehicles", [])}

if inv.get("inventory_count") != len(vehicle_ids):
    fail(
        f"inventory_count {inv.get('inventory_count')} != {len(vehicle_ids)} vehicles listed"
    )
else:
    ok("inventory_count matches vehicle list")

for rel, key in [
    ("dealer_costs/cost-basis.json", "cost_basis"),
    ("market_data/market-position.json", "positions"),
    ("market_data/pricing-recommendation.json", "recommendations"),
    ("market_data/comparables.json", "comparables"),
]:
    data = fixtures.get(rel, {}).get("data", {}).get(key, {})
    covered = {k for k in data if not k.startswith("_")}
    missing = vehicle_ids - covered
    unknown = covered - vehicle_ids
    if missing:
        fail(f"mocks/{rel}: no entry for {sorted(missing)}")
    if unknown:
        fail(f"mocks/{rel}: entries for unknown vehicles {sorted(unknown)}")
    if not missing and not unknown:
        ok(f"mocks/{rel} covers all {len(vehicle_ids)} vehicles")

# D6: reserved_slots must be a superset of confirmed_inbound.
cap = fixtures.get("inventory/capacity.json", {}).get("data", {})
if cap:
    if cap["reserved_slots"] < cap["confirmed_inbound"]:
        fail(
            f"capacity: reserved_slots ({cap['reserved_slots']}) < confirmed_inbound "
            f"({cap['confirmed_inbound']}); D6 requires reserved_slots to be a superset"
        )
    else:
        ok("capacity: reserved_slots >= confirmed_inbound (D6)")

    if cap["current_inventory"] > cap["total_physical_slots"]:
        fail("capacity: current_inventory exceeds total_physical_slots")
    else:
        ok("capacity: inventory fits physical slots")

# committed inbound in inbound.json must equal confirmed_inbound in capacity.json
inbound = fixtures.get("inventory/inbound.json", {}).get("data", {})
if inbound and cap:
    committed = sum(1 for v in inbound.get("vehicles", []) if v.get("committed_slot"))
    if committed != cap["confirmed_inbound"]:
        fail(
            f"inbound: {committed} committed_slot units but capacity.confirmed_inbound "
            f"is {cap['confirmed_inbound']}"
        )
    else:
        ok("inbound committed units match capacity.confirmed_inbound")


# --- 6: scenarios ---------------------------------------------------------------------

print("\nScenarios")
warning_codes = set(schemas.get("warning.schema.json", {}).get("properties", {}).get("code", {}).get("enum", []))

for path in sorted(SCENARIO_DIR.glob("*.json")):
    doc = load_json(path)
    if doc is None:
        continue
    for sc in doc.get("scenarios", []):
        sid = sc.get("id", "?")
        vid = sc.get("vehicle_id")
        if vid and vid not in vehicle_ids:
            fail(f"{path.name} {sid}: unknown vehicle_id {vid}")
        for bucket in ("warnings_must_include", "warnings_must_not_include"):
            for code in sc.get("expect", {}).get(bucket, []):
                if code not in warning_codes:
                    fail(f"{path.name} {sid}: unknown warning code {code}")
    ok(f"{path.name}: {len(doc.get('scenarios', []))} scenarios")


# --- 7: warning codes are mapped to severities (D7) -----------------------------------

print("\nWarning severity mapping")
try:
    import yaml

    mapping = yaml.safe_load(
        (ROOT / "config" / "assumptions" / "warnings.yaml").read_text(encoding="utf-8")
    )
    mapped: set[str] = set()
    for group in ("single_vehicle", "portfolio", "promotion"):
        mapped |= set(mapping.get(group, {}) or {})

    unmapped = warning_codes - mapped
    orphaned = mapped - warning_codes

    if unmapped:
        # A YAML key written as `CODE:{ severity: X }` -- no space after the colon --
        # is not parsed as a mapping and the code silently disappears. This check exists
        # because exactly that happened.
        fail(f"warnings.yaml does not map: {sorted(unmapped)}")
    if orphaned:
        fail(f"warnings.yaml maps codes absent from warning.schema.json: {sorted(orphaned)}")
    if not unmapped and not orphaned:
        ok(f"all {len(warning_codes)} warning codes have a declared severity")

    for group in ("single_vehicle", "portfolio", "promotion"):
        for code, rule in (mapping.get(group, {}) or {}).items():
            if not isinstance(rule, dict) or "severity" not in rule:
                fail(f"warnings.yaml {group}.{code}: no severity (check YAML spacing)")
except ImportError:  # pragma: no cover
    fail("PyYAML not installed; cannot check the severity mapping")


# --- report ---------------------------------------------------------------------------

print()
if errors:
    print(f"FAILED  {len(errors)} problem(s), {checks} check(s) passed\n")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)

print(f"PASSED  {checks} checks\n")
