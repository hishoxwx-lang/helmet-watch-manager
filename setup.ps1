# ============================================================
# Helmet Watch Manager - Windows Setup Script (PowerShell 5.1)
# All-ASCII on purpose: PowerShell 5.1 garbles non-ASCII output.
# ============================================================

param(
    [string]$WebhookUrl = ""
)

# --- TLS 1.2 (needed for GitHub / python.org downloads on old .NET) ---
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {}

# --- Strict error handling for downloads/service ops, but keep going ---
$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host ""; Write-Host "===> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "     [OK] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "     [!!] $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "     [XX] $msg" -ForegroundColor Red }

# ------------------------------------------------------------
# 0) Admin check
# ------------------------------------------------------------
Write-Step "Checking administrator privileges..."
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Warn2 "This script is NOT running as Administrator."
    Write-Warn2 "Firewall rule, NSSM service and scheduled tasks will FAIL."
    Write-Warn2 "Re-run from an elevated PowerShell: powershell -ExecutionPolicy Bypass -File setup.ps1"
    $ans = Read-Host "Continue anyway? (y/N)"
    if ($ans -ne "y") { exit 1 }
} else {
    Write-Ok "Running as Administrator."
}

# ------------------------------------------------------------
# Config / paths
# ------------------------------------------------------------
$AppDir       = Join-Path $env:USERPROFILE "helmet-manager"
$TemplatesDir = Join-Path $AppDir "templates"
$RepoOwner    = "hishoxwx-lang"
$RepoName     = "helmet-watch-manager"
$Branch       = "main"
$RawBase      = "https://raw.githubusercontent.com/$RepoOwner/$RepoName/$Branch/"
$Port         = 8080
$IntervalMin  = 5
$Files = @(
    "app.py",
    "checker.py",
    "requirements.txt",
    "products.json",
    "config.json"
)
$TemplateFiles = @(
    "index.html",
    "settings.html",
    "login.html",
    "setup.html"
)

# ------------------------------------------------------------
# 1) Python check / install
# ------------------------------------------------------------
Write-Step "Checking Python..."
$python = $null
foreach ($cmd in @("python", "py")) {
    try {
        $v = & $cmd --version 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -match "Python (\d+)\.(\d+)") {
            $maj = [int]$Matches[1]; $min = [int]$Matches[2]
            if (($maj -gt 3) -or ($maj -eq 3 -and $min -ge 8)) {
                if ($cmd -eq "py") { $python = (Get-Command py).Source }
                else               { $python = (Get-Command python).Source }
                Write-Ok "Python found: $v ($python)"
                break
            }
        }
    } catch {}
}

if (-not $python) {
    Write-Warn2 "Python 3.8+ not found. Trying winget..."
    $wingetCmd = Get-Command winget -ErrorAction SilentlyContinue
    if ($wingetCmd) {
        Write-Host "     Installing Python 3.12 via winget..."
        & winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
        # Refresh PATH for this session
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
        $pyExe = Join-Path $env:ProgramFiles "Python312\python.exe"
        if (-not (Test-Path $pyExe)) { $pyExe = Join-Path ${env:LOCALAPPDATA} "Programs\Python\Python312\python.exe" }
        if (Test-Path $pyExe) {
            $python = $pyExe
            Write-Ok "Python installed: $python"
        } else {
            Write-Warn2 "winget finished but python.exe not found. You may need to REBOOT then re-run this script."
        }
    } else {
        Write-Err "winget is not available. Install Python 3.12 manually from https://www.python.org/downloads/ then re-run."
    }
}

if (-not $python) {
    Write-Err "Cannot continue without Python. Aborting."
    exit 2
}

# ------------------------------------------------------------
# 2) App directory + download files from GitHub raw
# ------------------------------------------------------------
Write-Step "Creating app directory: $AppDir"
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
New-Item -ItemType Directory -Force -Path $TemplatesDir | Out-Null
Write-Ok "Directory ready."

Write-Step "Downloading latest files from GitHub..."
$wc = New-Object System.Net.WebClient
foreach ($f in $Files) {
    $url  = $RawBase + $f
    $dest = Join-Path $AppDir $f
    Write-Host "     - $f"
    try {
        $wc.DownloadFile($url, $dest)
    } catch {
        Write-Err "Failed to download $url : $($_.Exception.Message)"
        exit 3
    }
}
foreach ($f in $TemplateFiles) {
    $url  = $RawBase + "templates/" + $f
    $dest = Join-Path $TemplatesDir $f
    Write-Host "     - templates/$f"
    try {
        $wc.DownloadFile($url, $dest)
    } catch {
        Write-Err "Failed to download $url : $($_.Exception.Message)"
        exit 3
    }
}
Write-Ok "All files downloaded."

# ------------------------------------------------------------
# 3) pip install
# ------------------------------------------------------------
Write-Step "Installing Python dependencies..."
& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $AppDir "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Write-Err "pip install failed."
    exit 4
}
Write-Ok "Dependencies installed."

# ------------------------------------------------------------
# 4) Discord Webhook URL (arg or prompt)
# ------------------------------------------------------------
Write-Step "Discord Webhook URL"
if (-not $WebhookUrl) {
    $WebhookUrl = Read-Host "Paste your Discord Webhook URL (Enter to skip)"
}
if ($WebhookUrl) { Write-Ok "Webhook URL received." }
else             { Write-Warn2 "No webhook set. You can add it later in the web UI (/settings)." }

# ------------------------------------------------------------
# 5) Admin password (prompt twice) -> hash via Python
# ------------------------------------------------------------
Write-Step "Admin password"
do {
    $sec1 = Read-Host "New admin password" -AsSecureString
    $sec2 = Read-Host "Confirm password"   -AsSecureString
    $bstr1 = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec1)
    $bstr2 = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec2)
    $p1 = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr1)
    $p2 = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr2)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr1)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr2)
    if ($p1 -ne $p2) { Write-Warn2 "Passwords do not match. Try again."; $p1 = "" }
    if ($p1.Length -lt 4) { Write-Warn2 "Password too short (min 4 chars)."; $p1 = "" }
} while (-not $p1)

Write-Host "     Generating password hash..."
$env:HELMET_PW = $p1
$tmpHashScript = Join-Path $env:TEMP "helmet_gen_hash.py"
$pycode = @'
import os
from werkzeug.security import generate_password_hash
print(generate_password_hash(os.environ["HELMET_PW"]))
'@
Set-Content -Path $tmpHashScript -Value $pycode -Encoding UTF8
$pwHash = (& $python $tmpHashScript)
Remove-Item $tmpHashScript -ErrorAction SilentlyContinue
Remove-Item Env:\HELMET_PW -ErrorAction SilentlyContinue
if (-not $pwHash) {
    Write-Err "Password hash generation failed."
    exit 5
}
# Clear plaintext from memory
$p1 = $null; $p2 = $null
Write-Ok "Password hash generated."

# ------------------------------------------------------------
# 6) Write config.json (BOM-less UTF-8)
# ------------------------------------------------------------
Write-Step "Writing config.json..."
$configFile = Join-Path $AppDir "config.json"
$config = Get-Content $configFile -Raw | ConvertFrom-Json
$config.password_hash       = $pwHash
$config.discord_webhook_url = $WebhookUrl
$config.port                = $Port
$config.interval_minutes    = $IntervalMin
$json = $config | ConvertTo-Json -Depth 6
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($configFile, $json, $utf8NoBom)
Write-Ok "config.json updated."

# ------------------------------------------------------------
# 7) Web service: NSSM (fallback: scheduled task @ startup)
# ------------------------------------------------------------
Write-Step "Installing web service (NSSM)..."
$nssmDir  = Join-Path $AppDir "nssm"
$nssmExe  = Join-Path $nssmDir "nssm.exe"
$svcName  = "HelmetManager"
$serviceInstalled = $false

if (-not (Test-Path $nssmExe)) {
    Write-Host "     Downloading NSSM 2.24..."
    $zipUrl = "https://nssm.cc/release/nssm-2.24.zip"
    $zipFile = Join-Path $env:TEMP "nssm-2.24.zip"
    try {
        $wc.DownloadFile($zipUrl, $zipFile)
        $tmpExtract = Join-Path $env:TEMP "nssm-extract"
        if (Test-Path $tmpExtract) { Remove-Item $tmpExtract -Recurse -Force }
        Expand-Archive -Path $zipFile -DestinationPath $tmpExtract -Force
        $cand = Get-ChildItem -Path $tmpExtract -Recurse -Filter "nssm.exe" | Where-Object { $_.DirectoryName -like "*win64*" } | Select-Object -First 1
        if (-not $cand) { $cand = Get-ChildItem -Path $tmpExtract -Recurse -Filter "nssm.exe" | Select-Object -First 1 }
        New-Item -ItemType Directory -Force -Path $nssmDir | Out-Null
        Copy-Item $cand.FullName $nssmExe -Force
        Write-Ok "NSSM extracted."
    } catch {
        Write-Warn2 "NSSM download/extract failed: $($_.Exception.Message)"
    }
}

if (Test-Path $nssmExe) {
    try {
        # Remove old service if exists
        & $nssmExe stop $svcName 2>$null | Out-Null
        & $nssmExe remove $svcName confirm 2>$null | Out-Null
        & $nssmExe install $svcName $python (Join-Path $AppDir "app.py") 2>&1 | Out-Null
        & $nssmExe set   $svcName AppDirectory $AppDir 2>&1 | Out-Null
        & $nssmExe set   $svcName AppStdout (Join-Path $AppDir "app.log") 2>&1 | Out-Null
        & $nssmExe set   $svcName AppStderr (Join-Path $AppDir "app.log") 2>&1 | Out-Null
        & $nssmExe set   $svcName Start SERVICE_AUTO_START 2>&1 | Out-Null
        & $nssmExe start $svcName 2>&1 | Out-Null
        Start-Sleep -Seconds 2
        $svc = Get-Service -Name $svcName -ErrorAction SilentlyContinue
        if ($svc -and $svc.Status -eq "Running") {
            Write-Ok "Service '$svcName' installed and started."
            $serviceInstalled = $true
        } else {
            Write-Warn2 "Service registered but not running. Check: nssm start $svcName"
        }
    } catch {
        Write-Warn2 "NSSM service install failed: $($_.Exception.Message)"
    }
} else {
    Write-Warn2 "NSSM not available. Falling back to Task Scheduler (start at boot)."
}

# Fallback: scheduled task that starts app.py at system startup (pythonw, hidden)
if (-not $serviceInstalled) {
    Write-Step "Registering startup task (fallback)..."
    $pyw = $python -replace "\.exe$", "w.exe"
    if (-not (Test-Path $pyw)) { $pyw = $python }
    $stAction   = New-ScheduledTaskAction -Execute $pyw -Argument "`"$AppDir\app.py`"" -WorkingDirectory $AppDir
    $stTrigger  = New-ScheduledTaskTrigger -AtStartup
    $stSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 0)
    $stPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    try {
        Unregister-ScheduledTask -TaskName $svcName -Confirm:$false -ErrorAction SilentlyContinue
        Register-ScheduledTask -TaskName $svcName -Action $stAction -Trigger $stTrigger -Settings $stSettings -Principal $stPrincipal -Description "Helmet Watch Manager web service (startup)" -Force | Out-Null
        Start-ScheduledTask -TaskName $svcName
        Write-Ok "Startup task '$svcName' registered and started."
    } catch {
        Write-Warn2 "Startup task registration failed: $($_.Exception.Message)"
        Write-Warn2 "You can start the web app manually:  pythonw $AppDir\app.py"
    }
}

# ------------------------------------------------------------
# 8) Watcher task: run checker.py every N minutes
# ------------------------------------------------------------
Write-Step "Registering watcher task (every $IntervalMin min)..."
$watcherName = "HelmetWatcher"
$wAction   = New-ScheduledTaskAction -Execute $python -Argument "`"$AppDir\checker.py`"" -WorkingDirectory $AppDir
# Once + repetition (duration ~ forever). On Server 2016+ RepetitionDuration is supported.
$wTrigger  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)
$wTrigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMin) -RepetitionDuration (New-TimeSpan -Days 3650)).Repetition
$wSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$wPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
try {
    Unregister-ScheduledTask -TaskName $watcherName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $watcherName -Action $wAction -Trigger $wTrigger -Settings $wSettings -Principal $wPrincipal -Description "Helmet Watch Manager: check stock every $IntervalMin min" -Force | Out-Null
    Write-Ok "Watcher task '$watcherName' registered."
} catch {
    Write-Warn2 "Watcher task registration failed: $($_.Exception.Message)"
}

# ------------------------------------------------------------
# 9) Firewall rule: allow inbound TCP 8080
# ------------------------------------------------------------
Write-Step "Configuring firewall (TCP $Port inbound)..."
try {
    Get-NetFirewallRule -DisplayName "HelmetManager" -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName "HelmetManager" -Direction Inbound -LocalPort $Port -Protocol TCP -Action Allow | Out-Null
    Write-Ok "Firewall rule added (TCP $Port inbound)."
} catch {
    Write-Warn2 "Firewall rule failed: $($_.Exception.Message)"
}

# ------------------------------------------------------------
# 10) Public IP
# ------------------------------------------------------------
Write-Step "Detecting public IP..."
$publicIp = $null
try {
    $publicIp = (Invoke-WebRequest -UseBasicParsing -Uri "https://api.ipify.org" -TimeoutSec 10).Content.Trim()
    Write-Ok "Public IP: $publicIp"
} catch {
    Write-Warn2 "Could not detect public IP."
}

# ------------------------------------------------------------
# Done
# ------------------------------------------------------------
$accessUrl = if ($publicIp) { "http://${publicIp}:$Port/" } else { "http://<SERVER_IP>:$Port/" }
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "=== Setup complete ===" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host "App dir     : $AppDir"
Write-Host "Python      : $python"
Write-Host "Access      : $accessUrl"
Write-Host "Password    : (the one you entered)"
if ($WebhookUrl) { Write-Host "Discord     : $WebhookUrl" } else { Write-Host "Discord     : (not set - add via /settings)" }
Write-Host "Web service : $svcName (auto-start on boot)"
Write-Host "Watch task  : $watcherName (every $IntervalMin min)"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1) Open the Access URL in a browser and log in."
Write-Host "  2) Add products (name / URL / size pattern / stock keyword)."
Write-Host "  3) If Discord not set, configure it in /settings."
Write-Host ""
Write-Host "Manage:"
Write-Host "  Service : nssm start $svcName  | nssm stop $svcName  | nssm remove $svcName confirm"
Write-Host "  Watcher : Start-ScheduledTask $watcherName ; Unregister-ScheduledTask $watcherName"
Write-Host "  Logs    : $AppDir\app.log"
Write-Host "============================================================"
