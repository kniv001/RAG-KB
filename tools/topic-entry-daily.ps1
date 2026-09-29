# **入口曲线每天采一行** —— 由计划任务 `RAGKB-EntryCurve` 调用，也可以手动跑。
#
# 为什么包一层 ps1：计划任务**从 shell 起 .ps1 必须带 Bypass**（本项目记过：否则执行策略
# 报错，而那个错**长得像脚本自己的问题**）。另外把输出**追加**到一份日志里 —— 计划任务
# 跑的时候没人在看控制台，出错必须留痕。
#
# ⚠️ 它**只读**：只 SELECT，不碰库、不调模型、不需要应用在跑（只要 psql 连得上）。
# 手动跑：powershell -NoProfile -ExecutionPolicy Bypass -File tools\topic-entry-daily.ps1
# 移除：  Unregister-ScheduledTask -TaskName RAGKB-EntryCurve -Confirm:$false
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$repo = 'D:\vs\rag-kb-java'
Set-Location $repo
$log = Join-Path $repo 'data\_entry_daily.log'

# **每次一个新进程的环境**：计划任务里没有 .bashrc 那类东西，PATH 里必须能找到 python
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = 'python' }

$out = & $py 'tools\topic-entry-daily.py' 2>&1 | Out-String
$stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
Add-Content -Path $log -Value "[$stamp]`n$out" -Encoding UTF8

# 计划任务里**退出码没人在看**，所以把失败也写进日志（上面那段已经写了全部输出）
if ($LASTEXITCODE -ne 0) {
    Add-Content -Path $log -Value "[$stamp] ！退出码 $LASTEXITCODE" -Encoding UTF8
}
