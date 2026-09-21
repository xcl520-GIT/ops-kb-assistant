# Stop the Local Ops Assistant server.
#
# Two passes, on purpose:
#   1. kill whatever is LISTENING on the port
#   2. sweep orphan python/pythonw processes that are running app.py
#
# Pass 2 exists because on Windows SO_REUSEADDR lets two processes bind
# the same port. If a previous stop attempt failed, the old instance keeps
# running side by side with the new one and requests get routed to either,
# which looks like "I changed the config but nothing happened".
#
# ASCII only (no BOM needed).
#
# Exit codes: 0 = something was stopped, 1 = nothing found, 2 = port still busy.

param(
    [int]$Port = 8765
)

$ErrorActionPreference = 'SilentlyContinue'
$found = $false

function Stop-One([int]$procId, [string]$why) {
    if (-not $procId -or $procId -eq 0) { return $false }
    Write-Host "[i] Killing PID $procId ($why) ..."
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    return $true
}

# ---------------- pass 1: listeners on the port ----------------
$listeners = @()
try { $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop } catch { $listeners = @() }

foreach ($c in $listeners) {
    if (Stop-One ([int]$c.OwningProcess) "listening on port $Port") { $found = $true }
}

if ($listeners.Count -eq 0) {
    # fallback for hosts without the NetTCPIP module
    $rows = netstat -ano | Select-String -Pattern ":$Port\s" | Select-String -Pattern "LISTENING"
    foreach ($r in $rows) {
        $parts = ($r.ToString().Trim() -split '\s+')
        $cand = 0
        if ([int]::TryParse($parts[-1], [ref]$cand)) {
            if (Stop-One $cand "listening on port $Port (netstat)") { $found = $true }
        }
    }
}

# ---------------- pass 2: orphan app.py processes ----------------
try {
    $orphans = Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and
        $_.CommandLine -like '*app.py*'
    }
    foreach ($p in $orphans) {
        if (Stop-One ([int]$p.ProcessId) "orphan app.py") { $found = $true }
    }
} catch {
    Write-Host "[!] Orphan sweep skipped: $($_.Exception.Message)"
}

Start-Sleep -Milliseconds 400

# ---------------- verify ----------------
$still = @()
try { $still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop } catch { $still = @() }

if ($still.Count -gt 0) {
    $ids = ($still | ForEach-Object { $_.OwningProcess } | Sort-Object -Unique) -join ','
    Write-Host "[!] Port $Port is STILL listening (PID $ids)."
    exit 2
}

if ($found) {
    Write-Host "[i] Server stopped."
    exit 0
}

Write-Host "[i] Nothing to stop on port $Port."
exit 1
