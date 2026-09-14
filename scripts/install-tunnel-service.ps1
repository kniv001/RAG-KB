# REQUIRES ADMIN —— 安装 cloudflared 为 Windows 服务（token 模式，无需 cert.pem）
#
# 为什么用 token 模式：本网络对 login.cloudflareaccess.org 的 IPv4 连接做 RST 封锁
# （实测 -4 失败 / -6 成功），而 `cloudflared tunnel login` 只会走 IPv4，
# 导致取证书那一步必然失败。token 模式完全不碰该主机。
#
# token 从 data/tunnel-token.txt 读取（该目录已 gitignore），不写进任何入库文件。

$ErrorActionPreference = 'Continue'
$repo = Split-Path $PSScriptRoot -Parent
$cf = "$repo\bin\cloudflared.exe"
$tokenFile = "$repo\data\tunnel-token.txt"
$log = "$repo\data\tunnel-service-install.log"

Start-Transcript -Path $log -Force | Out-Null

if (-not (Test-Path $tokenFile)) {
    Write-Host "ERROR: $tokenFile not found" -ForegroundColor Red
    Stop-Transcript | Out-Null
    exit 1
}
$token = (Get-Content $tokenFile -Raw).Trim()
Write-Host ("token loaded, length = {0}" -f $token.Length)

Write-Host "`n=== 1) stop legacy quick-tunnel process (if any) ===" -ForegroundColor Cyan
Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" | ForEach-Object {
    if ($_.CommandLine -match '--url') {
        Write-Host ("  stopping PID {0}" -f $_.ProcessId)
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
Start-Sleep -Seconds 2

Write-Host "=== 2) uninstall existing service (if any) ===" -ForegroundColor Cyan
& $cf service uninstall 2>&1 | Out-String | Write-Host
Start-Sleep -Seconds 2

Write-Host "=== 3) install service with token ===" -ForegroundColor Cyan
& $cf service install $token 2>&1 | Out-String | Write-Host
Start-Sleep -Seconds 5

Write-Host "=== 4) service status ===" -ForegroundColor Cyan
$svc = Get-Service cloudflared -ErrorAction SilentlyContinue
if ($svc) {
    $svc | Select-Object Name, Status, StartType | Format-Table -AutoSize
    if ($svc.Status -ne 'Running') {
        Write-Host "  starting..."
        Start-Service cloudflared -ErrorAction Continue
        Start-Sleep -Seconds 6
    }
    Get-Service cloudflared | Select-Object Name, Status, StartType | Format-Table -AutoSize
} else {
    Write-Host "  service not found" -ForegroundColor Red
}

Write-Host "=== 5) recent cloudflared log ===" -ForegroundColor Cyan
$logDir = "$env:ProgramData\Cloudflare"
if (Test-Path $logDir) {
    Get-ChildItem $logDir -Recurse -Filter '*.log' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 |
        ForEach-Object { Get-Content $_.FullName -Tail 20 }
} else {
    Write-Host "  (no log dir at $logDir)"
}

Write-Host "`n=== done ===" -ForegroundColor Green
Stop-Transcript | Out-Null
