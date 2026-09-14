# 一键启动：本地服务 + Cloudflare 隧道（后台），并打印公网地址
$repo = Split-Path $PSScriptRoot -Parent

Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
$p = (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue).OwningProcess
if ($p) { Stop-Process -Id $p -Force }
Start-Sleep -Seconds 2

Start-Process -FilePath "$repo\.venv\Scripts\python.exe" `
    -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000' `
    -WorkingDirectory $repo -WindowStyle Hidden `
    -RedirectStandardOutput "$repo\data\app.out.log" -RedirectStandardError "$repo\data\app.err.log"

Start-Process -FilePath "$repo\bin\cloudflared.exe" `
    -ArgumentList 'tunnel', '--url', 'http://127.0.0.1:8000', '--no-autoupdate' `
    -WindowStyle Hidden `
    -RedirectStandardOutput "$repo\data\tunnel.out.log" -RedirectStandardError "$repo\data\tunnel.err.log"

Write-Host "waiting for tunnel ..." -ForegroundColor Cyan
$url = $null
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    $m = Select-String -Path "$repo\data\tunnel.err.log" -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -ErrorAction SilentlyContinue |
        Where-Object { $_.Line -notmatch 'api\.trycloudflare' } | Select-Object -First 1
    if ($m) { $url = $m.Matches[0].Value; break }
}

if ($url) {
    $url | Set-Content "$repo\.tunnel-url"
    Write-Host "`n  public url : $url" -ForegroundColor Green
    $cred = Get-Content "$repo\data\auth.json" -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
    if ($cred) { Write-Host ("  login      : " + $cred.user + " / " + $cred.password) }
} else {
    Write-Host "`n  tunnel failed to start - check data\tunnel.err.log" -ForegroundColor Red
}
