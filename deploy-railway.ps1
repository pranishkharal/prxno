# Deploy KICK Clipper Bot to Railway
# Run: .\deploy-railway.ps1

$ErrorActionPreference = "Stop"

Write-Host "=== KICK Clipper Bot - Railway Deploy ===" -ForegroundColor Cyan

# Check if railway CLI is installed
if (-not (Get-Command railway -ErrorAction SilentlyContinue)) {
    Write-Host "Installing railway CLI..." -ForegroundColor Yellow
    npm install -g @railway/cli
}

# Login if not already logged in
$railwayWhoami = railway whoami 2>$null
if (-not $railwayWhoami) {
    Write-Host "Please login to Railway..." -ForegroundColor Yellow
    railway login
}

# Deploy
Write-Host "Deploying to Railway..." -ForegroundColor Green
cd "D:\Auto Clips for Kick"
railway deploy

Write-Host "`n=== Deployment Complete! ===" -ForegroundColor Green
Write-Host "Configure these in Railway Dashboard > Variables:"
Write-Host "  DISCORD_BOT_TOKEN=***"
Write-Host "  CLIP_CHANNEL_ID=1547317591796748288"
Write-Host "`nThen run: railway open"