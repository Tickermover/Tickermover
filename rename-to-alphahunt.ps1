# ============================================================================
#  Rename PopDetector_v5 → AlphaHunt
# ----------------------------------------------------------------------------
#  Run this FROM A NEW POWERSHELL WINDOW after closing the current Claude
#  Code session. Do NOT run while any process inside the folder is open
#  (Claude Code, VS Code, the FastAPI server, Explorer windows pointing
#  into the folder).
#
#  What it does:
#    1. Verifies the target name 'AlphaHunt' is free
#    2. Renames the folder PopDetector_v5 → AlphaHunt
#    3. Removes the stale git worktree pointer (since the worktree path
#       changed) and re-registers it at the new location
#    4. Verifies git status still works in the renamed folder
#
#  After it finishes, your project lives at:
#     C:\Users\SOURA\Documents\Claude\Projects\USA Stock Market\AlphaHunt
# ============================================================================

$ErrorActionPreference = 'Stop'

$Parent  = 'C:\Users\SOURA\Documents\Claude\Projects\USA Stock Market'
$OldName = 'PopDetector_v5'
$NewName = 'AlphaHunt'
$OldPath = Join-Path $Parent $OldName
$NewPath = Join-Path $Parent $NewName

Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  Rename: $OldName -> $NewName"                                    -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan

# ── 1. Sanity checks ────────────────────────────────────────────────────────
if (-not (Test-Path $OldPath)) {
    Write-Host "ERROR: source path not found: $OldPath" -ForegroundColor Red
    exit 1
}
if (Test-Path $NewPath) {
    Write-Host "ERROR: target already exists: $NewPath" -ForegroundColor Red
    Write-Host "Delete or move it first, then re-run."  -ForegroundColor Yellow
    exit 1
}

# Detect locked files (Claude Code / VS Code / server still running)
Write-Host "Checking for processes that have the folder open..." -ForegroundColor Yellow
$locks = Get-Process | Where-Object {
    $_.Path -and $_.Path.StartsWith($OldPath, [StringComparison]::OrdinalIgnoreCase)
}
if ($locks) {
    Write-Host "Locking processes found:" -ForegroundColor Red
    $locks | Select-Object Name, Id, Path | Format-Table
    Write-Host "Close those windows / kill those processes first." -ForegroundColor Red
    exit 1
}

# ── 2. Do the rename ────────────────────────────────────────────────────────
Write-Host "`nRenaming folder..." -ForegroundColor Yellow
Rename-Item -Path $OldPath -NewName $NewName -Force
Write-Host "OK  $OldPath" -ForegroundColor Green
Write-Host "      -> $NewPath" -ForegroundColor Green

# ── 3. Repair git worktrees ─────────────────────────────────────────────────
# The .claude/worktrees folder contains git worktrees whose .git files
# point at the OLD absolute path. 'git worktree repair' rewrites those
# pointers to the new location.
$WorktreeRoot = Join-Path $NewPath '.claude\worktrees'
if (Test-Path $WorktreeRoot) {
    Write-Host "`nRepairing git worktrees at $WorktreeRoot..." -ForegroundColor Yellow
    Push-Location $NewPath
    try {
        # List existing worktrees
        & git worktree list 2>&1 | Write-Host
        # Repair them all in one shot
        & git worktree repair
        Write-Host "OK  worktrees re-pointed" -ForegroundColor Green
    } finally {
        Pop-Location
    }
}

# ── 4. Verify git status works in the renamed folder ───────────────────────
Push-Location $NewPath
try {
    Write-Host "`nVerifying git in the renamed folder..." -ForegroundColor Yellow
    $branch = (& git rev-parse --abbrev-ref HEAD).Trim()
    $remote = (& git remote get-url origin).Trim()
    Write-Host "OK  branch: $branch" -ForegroundColor Green
    Write-Host "OK  remote: $remote" -ForegroundColor Green
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  Done." -ForegroundColor Cyan
Write-Host "  Project now lives at:" -ForegroundColor Cyan
Write-Host "     $NewPath" -ForegroundColor White
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. Update any shortcuts / Railway settings that point at the"
Write-Host "     old PopDetector_v5 path."
Write-Host "  2. If you use VS Code: File > Open Folder > new AlphaHunt path."
Write-Host "  3. Start a fresh Claude Code session inside the new folder."
Write-Host ""
