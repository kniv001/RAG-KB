# 一键启动本地服务：PostgreSQL + 应用
#
# 隧道不在这里管 —— 它已经是 Windows 服务（cloudflared，Automatic），开机自启。
# 早期版本会在这里杀 cloudflared 进程并另起快速隧道，那会跟 named tunnel 冲突，已移除。

$repo = Split-Path $PSScriptRoot -Parent

Write-Host "--- 1) PostgreSQL ---" -ForegroundColor Cyan
& "$PSScriptRoot\pg.ps1" start

Write-Host "--- 2) 应用 ---" -ForegroundColor Cyan
$p = (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue).OwningProcess
if ($p) { Stop-Process -Id $p -Force; Start-Sleep -Seconds 2 }
Start-Process -FilePath "$repo\.venv\Scripts\python.exe" `
    -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000' `
    -WorkingDirectory $repo -WindowStyle Hidden `
    -RedirectStandardOutput "$repo\data\app.out.log" -RedirectStandardError "$repo\data\app.err.log"

Write-Host "--- 3) 隧道（Windows 服务） ---" -ForegroundColor Cyan
$svc = Get-Service cloudflared -ErrorAction SilentlyContinue
if ($svc) {
    Write-Host ("  cloudflared: {0} / {1}" -f $svc.Status, $svc.StartType)
    if ($svc.Status -ne 'Running') {
        Start-Service cloudflared -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
        Write-Host ("  -> {0}" -f (Get-Service cloudflared).Status)
    }
} else {
    Write-Host "  未安装。用 scripts\install-tunnel-service.ps1 安装（需管理员）" -ForegroundColor Yellow
}

Write-Host "--- 4) 等待应用就绪 ---" -ForegroundColor Cyan
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $null = Invoke-WebRequest 'http://127.0.0.1:8000/api/whoami' -UseBasicParsing -TimeoutSec 3
        $ok = $true; break
    } catch { }
}

if ($ok) {
    Write-Host "`n  应用就绪" -ForegroundColor Green
    $cred = Get-Content "$repo\data\auth.json" -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
    if ($cred) { Write-Host ("  登录: {0} / {1}" -f $cred.user, $cred.password) }
    # 访问地址不写死在这里 —— 仓库是公开的。取当前隧道地址打印，没有就只提示本地。
    $host_ = "$repo\data\tunnel-hostname.txt"
    if (Test-Path $host_) {
        Write-Host "`n  https://$((Get-Content $host_ -Raw).Trim())" -ForegroundColor Green
    } else {
        Write-Host "`n  （外网地址见 cloudflared 控制台；本机 http://127.0.0.1:8000）" -ForegroundColor DarkGray
    }
} else {
    Write-Host "`n  应用未就绪 —— 看 data\app.err.log" -ForegroundColor Red
}
