# REQUIRES ADMIN —— 让笔记本合盖后继续运行（不睡眠）
#
# 背景：这台 ASUS 笔记本的 4 个电源方案里，SUB_BUTTONS 子组下的
#       LIDACTION（合盖动作）被厂商隐藏了，所以用户态下 `powercfg /q` 看不到它，
#       `setacvalueindex` 也会"假成功"（返回 0 但设置项不存在）。
#       必须先以管理员取消隐藏属性，再写入数值。
#
# 数值含义：0 = 不采取任何操作 / 1 = 睡眠 / 2 = 休眠 / 3 = 关机
# 本脚本把「交流(插电)」和「直流(电池)」都设为 0。
#   注意：电池供电时也设为 0 会持续耗电，笔记本会在几小时内耗尽 —— 长跑请插电。

$ErrorActionPreference = 'Continue'
$SUB = '4f971e89-eebd-4455-a8de-9e59040e7347'   # SUB_BUTTONS 电源按钮和盖子
$LID = '5ca83367-6e45-459f-a27b-476b1d01c936'   # LIDACTION 合盖动作
$log = Join-Path (Split-Path $PSScriptRoot -Parent) 'data\lid-awake.log'

Start-Transcript -Path $log -Force | Out-Null

Write-Host "=== 0) 改前状态 ===" -ForegroundColor Cyan
& powercfg /q SCHEME_CURRENT $SUB $LID 2>&1 | Out-String | Write-Host

Write-Host "=== 1) 取消隐藏 LIDACTION（这一步需要管理员） ===" -ForegroundColor Cyan
& powercfg -attributes SUB_BUTTONS $LID -ATTRIB_HIDE 2>&1 | Out-String | Write-Host
Write-Host ("  exit = {0}" -f $LASTEXITCODE)

Write-Host "=== 2) 设为「不采取任何操作」(0) ===" -ForegroundColor Cyan
& powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS $LID 0 2>&1 | Out-String | Write-Host
Write-Host ("  交流 exit = {0}" -f $LASTEXITCODE)
& powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS $LID 0 2>&1 | Out-String | Write-Host
Write-Host ("  直流 exit = {0}" -f $LASTEXITCODE)

Write-Host "=== 3) 应用方案 ===" -ForegroundColor Cyan
& powercfg /setactive SCHEME_CURRENT 2>&1 | Out-String | Write-Host

Write-Host "=== 4) 改后确认（应看到 LIDACTION 与 当前交流/直流 = 0x0） ===" -ForegroundColor Cyan
& powercfg /q SCHEME_CURRENT $SUB $LID 2>&1 | Out-String | Write-Host

Write-Host "=== 5) 顺带把插电时的空闲睡眠/显示器关闭也确认为「从不」 ===" -ForegroundColor Cyan
& powercfg /change standby-timeout-ac 0
& powercfg /change monitor-timeout-ac 0
& powercfg /change hibernate-timeout-ac 0
Write-Host "  done"

Write-Host "`n=== 完成。现在合盖不会睡眠了。 ===" -ForegroundColor Green
Write-Host "如需还原：把 LIDACTION 的值改回 1（睡眠）即可，" -ForegroundColor Yellow
Write-Host "命令：powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS $LID 1" -ForegroundColor Yellow

Stop-Transcript | Out-Null
