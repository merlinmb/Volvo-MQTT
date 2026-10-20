# Deploy volvo-mqtt to hal from Windows PowerShell.
#   .\deploy.ps1                       # default pi@hal.local
#   .\deploy.ps1 -Target pi@192.168.1.55
#   .\deploy.ps1 -KeepServerEnv        # don't upload the local .env
#
# If a .env exists next to this script it is uploaded to the server and
# replaces the server copy (previous one kept as .env.bak-<timestamp>).
# Tokens in data/ are never touched.
param(
    [string]$Target = "pi@hal.local",
    [string]$AppDir = "/home/pi/volvo-mqtt",
    [string]$ConfigDir = "/home/pi/portainer_data/volvo-mqtt",
    [int]$Port = 8084,
    [switch]$KeepServerEnv
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$tarball = Join-Path $env:TEMP "volvo-mqtt.tgz"
Write-Host "Packaging project..." -ForegroundColor Cyan
tar -czf $tarball --exclude=__pycache__ --exclude=data --exclude=.env --exclude=tests `
    volvo_mqtt requirements.txt Dockerfile docker-compose.yml .dockerignore .env.example homebridge-mqttthing.json README.md

Write-Host "Uploading to $Target..." -ForegroundColor Cyan
scp $tarball "${Target}:/tmp/volvo-mqtt.tgz"

$uploadEnv = (Test-Path (Join-Path $PSScriptRoot ".env")) -and -not $KeepServerEnv
if ($uploadEnv) {
    Write-Host "Uploading local .env (server copy will be backed up)..." -ForegroundColor Cyan
    scp (Join-Path $PSScriptRoot ".env") "${Target}:/tmp/volvo-mqtt.env"
} elseif (-not $KeepServerEnv) {
    Write-Host "No local .env found - keeping the server's .env." -ForegroundColor Yellow
}

$applyEnv = if ($uploadEnv) { "1" } else { "0" }
$remote = @"
set -e
mkdir -p $AppDir $ConfigDir/data
tar -xzf /tmp/volvo-mqtt.tgz -C $AppDir && rm /tmp/volvo-mqtt.tgz
if [ "$applyEnv" = "1" ] && [ -f /tmp/volvo-mqtt.env ]; then
  if [ -f $ConfigDir/.env ]; then
    cp -p $ConfigDir/.env $ConfigDir/.env.bak-`$(date +%Y%m%d-%H%M%S)
    ls -1t $ConfigDir/.env.bak-* 2>/dev/null | tail -n +6 | xargs -r rm --
  fi
  mv /tmp/volvo-mqtt.env $ConfigDir/.env
  sed -i 's/\r`$//' $ConfigDir/.env
  chmod 600 $ConfigDir/.env
  echo '>>> Server .env replaced with your local copy (backup kept).'
fi
if [ ! -f $ConfigDir/.env ]; then
  cp $AppDir/.env.example $ConfigDir/.env && chmod 600 $ConfigDir/.env
  sed -i 's/^WEB_PORT=.*/WEB_PORT=$Port/' $ConfigDir/.env
  echo '>>> Created $ConfigDir/.env - edit it, then run this deploy again.'
  exit 0
fi
# Keep the dashboard on the chosen port (80 and 8080 are taken on hal)
if grep -q '^WEB_PORT=' $ConfigDir/.env; then
  sed -i 's/^WEB_PORT=.*/WEB_PORT=$Port/' $ConfigDir/.env
else
  echo 'WEB_PORT=$Port' >> $ConfigDir/.env
fi
if ! grep -q '^WEB_PASSWORD_HASH=.\+\|^WEB_PASSWORD=.\+\|^WEB_AUTH_ENABLED=false' $ConfigDir/.env; then
  echo '>>> WARNING: no dashboard password set. Sign-in will fail until WEB_PASSWORD_HASH is set.'
fi
cd $AppDir
docker compose up -d --build --force-recreate
sleep 3
docker compose ps
docker logs --tail 20 volvo-mqtt
"@
ssh $Target ($remote -replace "`r", "")
Remove-Item $tarball -ErrorAction SilentlyContinue
$hostOnly = $Target.Split("@")[-1]
Write-Host "Dashboard: http://${hostOnly}:$Port" -ForegroundColor Green
