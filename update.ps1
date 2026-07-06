# ============================================================
#  Meridian - safe updater (Windows PowerShell)
#  Preserves your CV, settings, API key, database, outputs.
#  Handles both meridian_v*.zip and jobcopilot_v*.zip names.
#  Usage: drop any release zip into Downloads, then run this.
# ============================================================

$Proj = "C:\Users\GenAI\Documents\Gen AI Projects\jobcopilot"

# Search for both naming patterns, pick the newest of either
$Zips = @(
    Get-ChildItem "$env:USERPROFILE\Downloads\meridian_v*.zip" -ErrorAction SilentlyContinue
    Get-ChildItem "$env:USERPROFILE\Downloads\jobcopilot_v*.zip" -ErrorAction SilentlyContinue
) | Sort-Object LastWriteTime -Descending

$Zip = $Zips | Select-Object -First 1

if (-not $Zip) {
    Write-Host "No meridian_v*.zip or jobcopilot_v*.zip found in Downloads." -ForegroundColor Red
    Write-Host "Download a release zip to your Downloads folder and try again." -ForegroundColor Yellow
    return
}
Write-Host "Updating from: $($Zip.Name)" -ForegroundColor Cyan

# Safety: copying jobcopilot.db while the app is running can capture a
# mid-write, inconsistent database. Warn and let the user bail out.
$Running = Get-Process -Name python, pythonw, streamlit -ErrorAction SilentlyContinue
if ($Running) {
    Write-Host "WARNING: A Python/Streamlit process is running. If Meridian is open," -ForegroundColor Yellow
    Write-Host "close it first so the database backup is consistent." -ForegroundColor Yellow
    $resp = Read-Host "Continue anyway? (y/N)"
    if ($resp -ne "y") { Write-Host "Update cancelled." -ForegroundColor Red; return }
}

# Backup personal files
$Backup = Join-Path $Proj ("_backup_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
New-Item -ItemType Directory -Force -Path $Backup | Out-Null
foreach ($f in @("resume_master.yaml","config.yaml","secrets.yaml","jobcopilot.db")) {
    if (Test-Path (Join-Path $Proj $f)) { Copy-Item (Join-Path $Proj $f) $Backup -Force }
}
foreach ($d in @("outputs","cv_versions")) {
    if (Test-Path (Join-Path $Proj $d)) {
        Copy-Item (Join-Path $Proj $d) $Backup -Recurse -Force
    }
}

# Extract + copy new code
$Tmp = Join-Path $env:TEMP "meridian_update"
if (Test-Path $Tmp) { Remove-Item $Tmp -Recurse -Force }
Expand-Archive -Path $Zip.FullName -DestinationPath $Tmp -Force
$Src = Join-Path $Tmp "jobcopilot"
Copy-Item (Join-Path $Src "*") $Proj -Recurse -Force

# Restore personal files
foreach ($f in @("resume_master.yaml","config.yaml","secrets.yaml","jobcopilot.db")) {
    if (Test-Path (Join-Path $Backup $f)) { Copy-Item (Join-Path $Backup $f) $Proj -Force }
}
# Restore personal folders (CV snapshots + generated resumes/prompts)
foreach ($d in @("outputs","cv_versions")) {
    $src = Join-Path (Join-Path $Backup $d) "*"
    if (Test-Path (Join-Path $Backup $d)) {
        New-Item -ItemType Directory -Force -Path (Join-Path $Proj $d) | Out-Null
        Copy-Item $src (Join-Path $Proj $d) -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$v = (Get-Content (Join-Path $Proj "VERSION") -ErrorAction SilentlyContinue)
Write-Host ""
Write-Host "Updated to version $v. Backup saved at: $Backup" -ForegroundColor Green
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  venv\Scripts\Activate.ps1" -ForegroundColor White
Write-Host "  pip install -r requirements.txt" -ForegroundColor White
Write-Host "  streamlit run app.py" -ForegroundColor White
