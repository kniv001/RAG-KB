# 恢复把应用暴露到公网的 Cloudflare 隧道（服务名 Cloudflared）。
# 与 tunnel-off.ps1 配对，写法与理由见那边的注释（别把命令塞进 -ArgumentList）。
#
# 用法：
#   1) 管理员 PowerShell：  powershell -NoProfile -ExecutionPolicy Bypass -File tools\tunnel-on.ps1
#   2) 普通窗口提权：      Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','D:\vs\rag-kb-java\tools\tunnel-on.ps1'

$ErrorActionPreference = 'Continue'
try {
    $before = (Get-Service Cloudflared).Status
    Start-Service Cloudflared -ErrorAction Stop
    Start-Sleep -Seconds 2
    $after = (Get-Service Cloudflared).Status
    Write-Host "Cloudflared: $before -> $after"
    if ($after -ne 'Running') { exit 1 }
} catch {
    Write-Host "失败：$($_.Exception.Message)"
    Write-Host "（多半是没提权：起服务需要管理员）"
    exit 1
}
