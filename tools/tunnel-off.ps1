# 停掉把应用暴露到公网的 Cloudflare 隧道（服务名 Cloudflared）。
#
# 为什么要写成文件：把命令塞进 `Start-Process -ArgumentList ... -Command "<一长串>"` 时，
# 内层双引号会在拼接时提前闭合外层引号，提权窗口收到的是残缺命令、报错退出
# （2026-09-20 踩过：停服务成功了，但后面写结果文件那步崩了，还留下"脚本崩了"的假象）。
# **路径不带引号嵌套**才是稳的。
#
# 用法（任选）：
#   1) 管理员 PowerShell 里直接跑：  powershell -NoProfile -ExecutionPolicy Bypass -File tools\tunnel-off.ps1
#   2) 从普通窗口提权跑：            Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','D:\vs\rag-kb-java\tools\tunnel-off.ps1'
# 恢复用 tools\tunnel-on.ps1

$ErrorActionPreference = 'Continue'
try {
    $before = (Get-Service Cloudflared).Status
    Stop-Service Cloudflared -Force -ErrorAction Stop
    Start-Sleep -Seconds 1
    $after = (Get-Service Cloudflared).Status
    $procs = (Get-Process cloudflared -ErrorAction SilentlyContinue | Measure-Object).Count
    Write-Host "Cloudflared: $before -> $after　残留进程 $procs"
    if ($after -ne 'Stopped') { exit 1 }
} catch {
    Write-Host "失败：$($_.Exception.Message)"
    Write-Host "（多半是没提权：停服务需要管理员）"
    exit 1
}
