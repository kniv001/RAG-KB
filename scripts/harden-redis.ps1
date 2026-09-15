# 收紧 Redis 的入站暴露面。
#
# 背景（2026-09-15 实测）：
#   · netstat 显示 Redis 监听 0.0.0.0:6379 —— 所有网卡，不只是本机
#   · 配置文件里既没有 bind 也没有 requirepass，且 3.0.504 没有 protected-mode
#   · 防火墙存在一条名为 Redis 的入站规则：action=Allow，profile=Any
#   三者叠加 = 同一局域网内任何人都能无密码连上，读写 refresh token（等于盗取会话），
#   还能用 CONFIG SET dir/dbfilename + SAVE 写任意文件。
#
# 本脚本只收紧防火墙这一层 —— 把那条 Any 放行规则停掉。
# 不需要改 Redis 配置、不需要重启服务、不影响应用：
# 应用连的是 127.0.0.1，而回环流量不受 Windows 防火墙入站规则约束。
#
# 用法：以管理员身份运行 PowerShell，然后执行本脚本。
#   powershell -ExecutionPolicy Bypass -File scripts\harden-redis.ps1
#
# 想恢复：powershell -ExecutionPolicy Bypass -File scripts\harden-redis.ps1 -Restore

param(
    [switch]$Restore
)

$ErrorActionPreference = 'Stop'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "需要管理员权限。请以管理员身份运行 PowerShell 后重试。" -ForegroundColor Red
    exit 1
}

$rules = Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object {
    $_.Direction -eq 'Inbound' -and $_.DisplayName -like '*Redis*'
}

if (-not $rules) {
    Write-Host "没有找到名为 *Redis* 的入站规则。" -ForegroundColor Yellow
    Write-Host "可能已经被处理过，或规则名不同。请手动核对：" -ForegroundColor Yellow
    Write-Host "  Get-NetFirewallRule | ? {`$_.Direction -eq 'Inbound'} | % { `$a = `$_ | Get-NetFirewallApplicationFilter -EA 0; if (`$a.Program -like '*redis*') { `$_ } }"
    exit 0
}

if ($Restore) {
    foreach ($r in $rules) {
        Set-NetFirewallRule -Name $r.Name -Enabled True
        Write-Host "已恢复：$($r.DisplayName)" -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Host "警告：恢复后 Redis 又会暴露给整个局域网，且它没有密码。" -ForegroundColor Red
    exit 0
}

Write-Host "找到以下入站规则：" -ForegroundColor Cyan
foreach ($r in $rules) {
    $prof = $r.Profile
    Write-Host "  $($r.DisplayName)  enabled=$($r.Enabled)  action=$($r.Action)  profile=$prof"
}

Write-Host ""
foreach ($r in $rules) {
    Set-NetFirewallRule -Name $r.Name -Enabled False
    Write-Host "已停用：$($r.DisplayName)" -ForegroundColor Green
}

Write-Host ""
Write-Host "验证应用是否仍然正常（Redis 走回环，不应受影响）："
Write-Host "  curl http://127.0.0.1:8080/api/health"
Write-Host ""
Write-Host "更彻底的一层（需要改配置 + 重启服务，本脚本不做）：" -ForegroundColor Cyan
Write-Host "  在 C:\Program Files\Redis\redis.windows-service.conf 里加两行："
Write-Host "      bind 127.0.0.1"
Write-Host "      requirepass <一个强密码>"
Write-Host "  然后重启服务：Restart-Service Redis"
Write-Host "  注意：设了 requirepass 之后，应用必须带上 KB_REDIS_PASSWORD 环境变量，"
Write-Host "        否则登录与刷新令牌会全部失败（refresh token 存在 Redis 里）。"
