<#
.SYNOPSIS
    Structural validation of the schema set and fixtures, with no Python or Node required.

.DESCRIPTION
    A subset of scripts/validate_schemas.py, for environments without a JSON Schema engine.

    COVERS:
      - every schema and fixture parses as JSON
      - $id matches filename
      - every cross-file $ref resolves, including $defs targets
      - fixtures carry a well-formed meta envelope
      - fixture cross-references are consistent (vehicle coverage, capacity invariants)
      - scenarios reference real vehicles and real warning codes

    DOES NOT COVER:
      - whether each schema compiles under JSON Schema draft 2020-12
      - whether any instance document validates against its schema
    Both require a real validator. Run scripts/validate_schemas.py once Python is available.
#>

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$errors = New-Object System.Collections.ArrayList
$checks = 0

function Add-Failure($msg) { [void]$errors.Add($msg) }
function Add-Pass($msg) { $script:checks++; Write-Host "  ok  $msg" }

function Read-JsonFile($path) {
    try { return (Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json) }
    catch { Add-Failure "$($path.Replace($root,'').TrimStart('\')): invalid JSON - $($_.Exception.Message)"; return $null }
}

function Get-Refs($node, $path) {
    $out = @()
    if ($node -is [System.Management.Automation.PSCustomObject]) {
        foreach ($p in $node.PSObject.Properties) {
            if ($p.Name -eq '$ref' -and $p.Value -is [string]) {
                $out += [pscustomobject]@{ Ref = $p.Value; Where = $path }
            } else {
                $out += Get-Refs $p.Value "$path.$($p.Name)"
            }
        }
    } elseif ($node -is [System.Collections.IEnumerable] -and $node -isnot [string]) {
        $i = 0
        foreach ($item in $node) { $out += Get-Refs $item "$path[$i]"; $i++ }
    }
    return $out
}

# --- schemas ---------------------------------------------------------------
Write-Host "`nSchemas"
$baseUri = 'https://pricing-demo.local/schemas/'
$schemas = @{}
foreach ($f in Get-ChildItem "$root\schemas\*.schema.json" | Sort-Object Name) {
    $doc = Read-JsonFile $f.FullName
    if ($null -eq $doc) { continue }
    $schemas[$f.Name] = $doc
    $expected = "$baseUri$($f.Name)"
    if ($doc.'$id' -ne $expected) { Add-Failure "$($f.Name): `$id is '$($doc.'$id')', expected '$expected'" }
    else { Add-Pass "$($f.Name) parses, `$id correct" }
}

# --- refs ------------------------------------------------------------------
Write-Host "`nReferences"
foreach ($name in ($schemas.Keys | Sort-Object)) {
    $bad = 0
    foreach ($r in (Get-Refs $schemas[$name] '$')) {
        if ($r.Ref.StartsWith('#')) { continue }
        $parts = $r.Ref -split '#', 2
        $target = $parts[0]
        $fragment = if ($parts.Count -gt 1) { $parts[1] } else { '' }
        if (-not $schemas.ContainsKey($target)) {
            Add-Failure "$name at $($r.Where): `$ref to unknown schema '$target'"; $bad++; continue
        }
        if ($fragment -like '/$defs/*') {
            $defName = ($fragment -split '/')[-1]
            $defs = $schemas[$target].'$defs'
            if ($null -eq $defs -or $null -eq $defs.PSObject.Properties[$defName]) {
                Add-Failure "$name at $($r.Where): '$target' has no `$defs/$defName"; $bad++
            }
        }
    }
    if ($bad -eq 0) { Add-Pass "$name references resolve" }
}

# --- fixtures --------------------------------------------------------------
Write-Host "`nFixtures"
$required = @('source', 'data_timestamp', 'source_version')
$fixtures = @{}
foreach ($f in Get-ChildItem "$root\mocks" -Recurse -Filter *.json | Sort-Object FullName) {
    $doc = Read-JsonFile $f.FullName
    if ($null -eq $doc) { continue }
    $rel = $f.FullName.Substring("$root\mocks\".Length).Replace('\', '/')
    $fixtures[$rel] = $doc
    if ($rel.StartsWith('llm/')) {
        # Recorded model responses, not MCP tool responses: no provenance envelope.
        Add-Pass "mocks/$rel parses (recorded model response)"
        continue
    }
    if ($null -eq $doc.meta) { Add-Failure "mocks/${rel}: missing meta envelope"; continue }
    $missing = $required | Where-Object { $null -eq $doc.meta.PSObject.Properties[$_] }
    if ($missing) { Add-Failure "mocks/${rel}: meta missing $($missing -join ', ')" }
    else { Add-Pass "mocks/$rel envelope" }
}

# --- cross-consistency -----------------------------------------------------
Write-Host "`nFixture consistency"
$inv = $fixtures['inventory/dealer-1001-inventory.json'].data
$vehicleIds = @($inv.vehicles | ForEach-Object { $_.vehicle_id })

if ($inv.inventory_count -ne $vehicleIds.Count) {
    Add-Failure "inventory_count $($inv.inventory_count) != $($vehicleIds.Count) vehicles listed"
} else { Add-Pass "inventory_count matches vehicle list ($($vehicleIds.Count))" }

$coverageTargets = @(
    @{ File = 'dealer_costs/cost-basis.json';        Key = 'cost_basis' },
    @{ File = 'market_data/market-position.json';          Key = 'positions' },
    @{ File = 'market_data/pricing-recommendation.json';   Key = 'recommendations' },
    @{ File = 'market_data/comparables.json';              Key = 'comparables' }
)
foreach ($t in $coverageTargets) {
    $node = $fixtures[$t.File].data.($t.Key)
    if ($null -eq $node) { Add-Failure "mocks/$($t.File): missing data.$($t.Key)"; continue }
    $covered = @($node.PSObject.Properties.Name | Where-Object { -not $_.StartsWith('_') })
    $missing = @($vehicleIds | Where-Object { $covered -notcontains $_ })
    $unknown = @($covered | Where-Object { $vehicleIds -notcontains $_ })
    if ($missing) { Add-Failure "mocks/$($t.File): no entry for $($missing -join ', ')" }
    if ($unknown) { Add-Failure "mocks/$($t.File): entries for unknown vehicles $($unknown -join ', ')" }
    if (-not $missing -and -not $unknown) { Add-Pass "mocks/$($t.File) covers all $($vehicleIds.Count) vehicles" }
}

$cap = $fixtures['inventory/capacity.json'].data
if ($cap.reserved_slots -lt $cap.confirmed_inbound) {
    Add-Failure "capacity: reserved_slots ($($cap.reserved_slots)) < confirmed_inbound ($($cap.confirmed_inbound)); D6 requires a superset"
} else { Add-Pass "capacity: reserved_slots >= confirmed_inbound (D6)" }

if ($cap.current_inventory -gt $cap.total_physical_slots) {
    Add-Failure "capacity: current_inventory exceeds total_physical_slots"
} else { Add-Pass "capacity: inventory fits physical slots" }

$committed = @($fixtures['inventory/inbound.json'].data.vehicles | Where-Object { $_.committed_slot }).Count
if ($committed -ne $cap.confirmed_inbound) {
    Add-Failure "inbound: $committed committed_slot units but capacity.confirmed_inbound is $($cap.confirmed_inbound)"
} else { Add-Pass "inbound committed units match capacity.confirmed_inbound" }

# --- scenarios -------------------------------------------------------------
Write-Host "`nScenarios"
$codes = @($schemas['warning.schema.json'].properties.code.enum)
foreach ($f in Get-ChildItem "$root\tests\scenarios\*.json" | Sort-Object Name) {
    $doc = Read-JsonFile $f.FullName
    if ($null -eq $doc) { continue }
    $bad = 0
    foreach ($sc in $doc.scenarios) {
        if ($sc.vehicle_id -and $vehicleIds -notcontains $sc.vehicle_id) {
            Add-Failure "$($f.Name) $($sc.id): unknown vehicle_id $($sc.vehicle_id)"; $bad++
        }
        foreach ($bucket in @('warnings_must_include', 'warnings_must_not_include')) {
            foreach ($c in $sc.expect.$bucket) {
                if ($codes -notcontains $c) { Add-Failure "$($f.Name) $($sc.id): unknown warning code $c"; $bad++ }
            }
        }
    }
    if ($bad -eq 0) { Add-Pass "$($f.Name): $($doc.scenarios.Count) scenarios" }
}

# --- report ----------------------------------------------------------------
Write-Host ""
if ($errors.Count -gt 0) {
    Write-Host "FAILED  $($errors.Count) problem(s), $checks check(s) passed`n"
    foreach ($e in $errors) { Write-Host "  - $e" }
    exit 1
}
Write-Host "PASSED  $checks structural checks"
Write-Host "NOTE    draft 2020-12 compilation and instance validation still require scripts/validate_schemas.py"
exit 0
