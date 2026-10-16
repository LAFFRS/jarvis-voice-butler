<#
.SYNOPSIS
    Starts Jarvis: the voice agent, the web UI, and (in local mode) the local
    model services it needs.

.EXAMPLE
    .\jarvis.ps1                  # mode from jarvis_new\config\modes.toml
    .\jarvis.ps1 -Mode local      # force local mode for this run
    .\jarvis.ps1 -Console         # talk to Jarvis in this terminal, no web UI
    .\jarvis.ps1 -NoFrontend      # agent only
    .\jarvis.ps1 -Phone           # also let the phone app connect over Wi-Fi
#>
[CmdletBinding()]
param(
    [ValidateSet("cloud", "local")]
    [string]$Mode,
    [switch]$Console,
    [switch]$NoFrontend,
    [switch]$Phone
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$AgentDir = Join-Path $Root "jarvis_new"
$FrontendDir = Join-Path $AgentDir "frontend"

function Write-Step($Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Warn($Message) { Write-Host "    $Message" -ForegroundColor Yellow }

function Test-Command($Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-Url($Url) {
    try {
        Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3 | Out-Null
        return $true
    } catch {
        # Any HTTP response (even 404) means the server is up.
        return [bool]$_.Exception.Response
    }
}

function Get-LanAddress {
    # The adapter with the default route is the real Wi-Fi/Ethernet one, not a
    # WSL or Hyper-V virtual adapter.
    $route = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
        Sort-Object RouteMetric | Select-Object -First 1
    $ip = if ($route) {
        Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty IPAddress
    }
    if ($ip) { return $ip } else { return "127.0.0.1" }
}

function Get-PhoneKey($EnvFile) {
    $line = Get-Content $EnvFile | Where-Object { $_ -match "^JARVIS_PHONE_KEY=(.+)$" } | Select-Object -First 1
    if ($line) { return ($line -replace "^JARVIS_PHONE_KEY=", "").Trim() }
    # First use: create a random key and remember it in .env.local.
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $key = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    Add-Content -Path $EnvFile -Value "JARVIS_PHONE_KEY=$key"
    return $key
}

function Start-Window($Title, $WorkDir, $Command) {
    Start-Process powershell -WorkingDirectory $WorkDir -ArgumentList @(
        "-NoExit", "-Command", "`$Host.UI.RawUI.WindowTitle = '$Title'; $Command"
    ) | Out-Null
}

# --- Prerequisites ----------------------------------------------------------
foreach ($tool in @("uv")) {
    if (-not (Test-Command $tool)) {
        throw "$tool is not installed. See SETUP-WINDOWS.md."
    }
}
if (-not (Test-Path (Join-Path $AgentDir ".env.local"))) {
    throw "jarvis_new\.env.local is missing. Copy jarvis_new\.env.example and fill it in."
}

if ($Mode) { $env:JARVIS_MODE = $Mode }
Push-Location $AgentDir
try {
    $ActiveMode = (uv run --quiet python src/modes.py).Trim()
} finally {
    Pop-Location
}
Write-Step "Starting Jarvis in $ActiveMode mode"

# Lets the web UI dispatch the agent registered as "my-agent" in agent.py.
$env:AGENT_NAME = "my-agent"

# --- Local mode services ----------------------------------------------------
if ($ActiveMode -eq "local") {
    if (Test-Command "livekit-server") {
        if (-not (Test-Url "http://localhost:7880")) {
            Write-Step "Starting local LiveKit server"
            # The phone needs to reach it over Wi-Fi; otherwise keep it on this PC.
            $bind = if ($Phone) { "0.0.0.0" } else { "127.0.0.1" }
            Start-Window "LiveKit server" $Root "livekit-server --dev --bind $bind"
            Start-Sleep -Seconds 2
        }
        if ($Phone) { $env:JARVIS_PHONE_LIVEKIT_URL = "ws://$(Get-LanAddress):7880" }
        # The dev server's fixed credentials. Process env beats .env.local.
        $env:LIVEKIT_URL = "ws://localhost:7880"
        $env:LIVEKIT_API_KEY = "devkey"
        $env:LIVEKIT_API_SECRET = "secret"
    } else {
        Write-Warn "livekit-server not found: using LiveKit Cloud from .env.local for audio."
        Write-Warn "For fully offline use, install livekit-server (see SETUP-WINDOWS.md)."
    }

    if (-not (Test-Url "http://localhost:11434/api/tags")) {
        if (Test-Command "ollama") {
            Write-Step "Starting Ollama"
            Start-Window "Ollama" $Root "ollama serve"
            Start-Sleep -Seconds 3
        } else {
            Write-Warn "Ollama is not installed. Get it from https://ollama.com"
        }
    }
    if (-not (Test-Url "http://localhost:8000/v1/models")) {
        Write-Warn "Speech-to-text server not reachable on port 8000 (see SETUP-WINDOWS.md)."
    }
    if (-not (Test-Url "http://localhost:8880/v1/models")) {
        Write-Warn "Text-to-speech server not reachable on port 8880 (see SETUP-WINDOWS.md)."
    }
}

# --- Agent and web UI ---------------------------------------------------------
if ($Console) {
    Write-Step "Console mode: speak to Jarvis here. Ctrl+C to stop."
    Push-Location $AgentDir
    try { uv run src/agent.py console } finally { Pop-Location }
    return
}

Write-Step "Starting the Jarvis agent"
Start-Window "Jarvis agent ($ActiveMode)" $AgentDir "uv run src/agent.py dev"

if ($Phone) {
    $key = Get-PhoneKey (Join-Path $AgentDir ".env.local")
    $endpoint = "http://$(Get-LanAddress):8787/token"
    Write-Step "Starting the phone token server"
    Start-Window "Jarvis phone server" $AgentDir "uv run src/phone_server.py"
    Write-Host ""
    Write-Host "    Put these two lines in agent-starter-flutter\assets\.env:" -ForegroundColor Green
    Write-Host "      LIVEKIT_TOKEN_ENDPOINT=$endpoint"
    Write-Host "      JARVIS_PHONE_KEY=$key"
    Write-Host "    Phone and PC must be on the same Wi-Fi. Allow Python through" -ForegroundColor Green
    Write-Host "    Windows Firewall (private networks) when Windows asks." -ForegroundColor Green
    Write-Host ""
}

if (-not $NoFrontend) {
    if (-not (Test-Command "pnpm")) {
        throw "pnpm is not installed. Run: npm install -g pnpm"
    }
    if (-not (Test-Path (Join-Path $FrontendDir "node_modules"))) {
        Write-Step "Installing web UI dependencies (first run only)"
        Push-Location $FrontendDir
        try { pnpm install } finally { Pop-Location }
    }
    Write-Step "Starting the web UI"
    # Bound to this PC only: the web UI's token route has no password.
    Start-Window "Jarvis web UI" $FrontendDir "pnpm dev -H 127.0.0.1"
    Start-Sleep -Seconds 6
    Start-Process "http://127.0.0.1:3000"
}

Write-Step "Jarvis is up. Close the opened windows to stop it."
