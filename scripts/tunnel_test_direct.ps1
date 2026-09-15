# REQUIRES ADMIN. 需要管理员权限运行。
#
# 目的：判定 cloudflared 连不上 Cloudflare 边缘，责任在「本机代理」还是「校园网」
#   停用 EternalTunnel(Meta Tunnel) -> 让 cloudflared 走校园网原生出口 -> 看能否建连
#   结束时会等回车，自动恢复 EternalTunnel
#
# 结果判读：
#   出现 "Registered tunnel connection"  -> 校园网放行 7844，问题在代理
#   仍然 ERR / precheck fail             -> 校园网也封 7844，转 Tailscale
#
# 控制台输出刻意使用 ASCII，避免 Windows PowerShell 5.1 读取无 BOM 的 UTF-8 脚本时中文乱码。

$ErrorActionPreference = 'Continue'
$repo = Split-Path $PSScriptRoot -Parent
$adapter = 'EternalTunnel'
$log = Join-Path $repo 'data\tunnel_test.log'

Start-Transcript -Path $log -Force | Out-Null

function Step($n, $msg) { Write-Host "`n=== $n. $msg ===" -ForegroundColor Cyan }

Step 1 "Stop existing cloudflared"
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

Step 2 "Disable adapter: $adapter"
Get-NetAdapter -Name $adapter -ErrorAction SilentlyContinue | Select-Object Name, Status | Format-Table -AutoSize
Disable-NetAdapter -Name $adapter -Confirm:$false -ErrorAction Continue
Start-Sleep -Seconds 3
Get-NetAdapter -Name $adapter -ErrorAction SilentlyContinue | Select-Object Name, Status | Format-Table -AutoSize

Step 3 "Flush DNS cache"
ipconfig /flushdns | Out-Null

Step 4 "Verify DNS is no longer fake-ip"
$dnsOk = $false
try {
    $ips = (Resolve-DnsName region1.v2.argotunnel.com -Type A -ErrorAction Stop).IPAddress
    Write-Host ("  region1.v2.argotunnel.com -> " + ($ips -join ', '))
    if ($ips -match '^198\.18\.') {
        Write-Host "  !! STILL FAKE-IP - DNS hijack not lifted" -ForegroundColor Red
    } else {
        Write-Host "  OK - real address" -ForegroundColor Green
        $dnsOk = $true
    }
} catch { Write-Host ("  resolve failed: " + $_.Exception.Message) -ForegroundColor Yellow }

Step 5 "Start cloudflared (bypassing proxy)"
Start-Process -FilePath "$repo\bin\cloudflared.exe" `
    -ArgumentList 'tunnel', '--url', 'http://127.0.0.1:8000', '--no-autoupdate' `
    -WindowStyle Hidden `
    -RedirectStandardOutput "$repo\data\tunnel3.out.log" -RedirectStandardError "$repo\data\tunnel3.err.log"

Step 6 "Waiting 45s ..."
Start-Sleep -Seconds 45

Write-Host "`n--- cloudflared key log lines ---"
$key = Get-Content "$repo\data\tunnel3.err.log" -ErrorAction SilentlyContinue |
    Select-String 'Registered tunnel connection|trycloudflare|precheck complete|ERR|Failed to dial|Unable to establish'
$key | Select-Object -Last 14 | ForEach-Object { Write-Host $_.Line }

$registered = [bool]($key | Where-Object { $_.Line -match 'Registered tunnel connection' })
$urlMatch = Select-String -Path "$repo\data\tunnel3.err.log" -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -ErrorAction SilentlyContinue | Select-Object -First 1
$url = if ($urlMatch) { $urlMatch.Matches[0].Value } else { '(none)' }

Write-Host ("`n  tunnel url : " + $url)
if ($registered) {
    Write-Host "  VERDICT: CAMPUS ALLOWS 7844 -> problem was the local proxy -> cloudflared is usable" -ForegroundColor Green
} else {
    Write-Host "  VERDICT: CAMPUS BLOCKS 7844 -> CF Tunnel unusable here -> switch to Tailscale" -ForegroundColor Red
}
Write-Host ("  dns_ok=" + $dnsOk + "  registered=" + $registered)

Write-Host "`n================================================================" -ForegroundColor Yellow
Read-Host "Press ENTER to restore $adapter"

Enable-NetAdapter -Name $adapter -Confirm:$false -ErrorAction Continue
Start-Sleep -Seconds 3
Get-NetAdapter -Name $adapter -ErrorAction SilentlyContinue | Select-Object Name, Status | Format-Table -AutoSize
Write-Host "restored." -ForegroundColor Green

Stop-Transcript | Out-Null
