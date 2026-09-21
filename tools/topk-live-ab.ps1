# 「加宽召回」的**真身**复验：`top-k-per-query` 8 vs 16，走真实链路。
#
# 为什么要重做：上一次真身复验（+1 题，据此退回 8）是在**重建前的语料**上跑的，
# 而重建之后复刻尺子给的是 36 → 56 → 68（k=8/12/16）。**两把尺子不能互相顶替**，
# 但两条数据来自**不同的语料状态**，所以现在这条对比是不成立的。
#
# 今天还顺手修了一件事让这次 A/B 更可信：**缓存键里加了"开关/参数指纹"** ——
# 上一轮那次 A/B 有一半是白跑的（第二臂全命中第一臂的缓存）。
# 这次 `top-k` 一变，`contextHash` 就变，键自然不同。
#
# 方法：每臂跑全 25 题（agent 真实链路），按**进提示词的那一批**（`done.sources`）判
# **全中率**。每臂 1 次 —— 真身是有噪声的，所以两臂都重跑，且用**同一批题**配对比。
#
# 用法：powershell -File tools\topk-live-ab.ps1

param([int[]]$Ks = @(8, 16))

$ErrorActionPreference = 'Continue'
$repo = 'D:\vs\rag-kb-java'
$jar = Join-Path $repo 'rag-kb-web\target\rag-kb.jar'
$java = 'C:\Program Files\Java\jdk-21\bin\java.exe'
$env:KB_DB_PASSWORD = (Get-Content 'D:\vs\rag-kb\data\pgapp.txt' -Raw).Trim()

function Stop-App {
    Get-CimInstance Win32_Process -Filter "name='java.exe'" |
        Where-Object { $_.CommandLine -like '*rag-kb.jar*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep -Seconds 4
}

function Start-App($topk) {
    $env:KB_TOP_K = "$topk"
    Start-Process -FilePath $java -ArgumentList '-jar', $jar `
        -WorkingDirectory $repo -WindowStyle Hidden
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Seconds 2
        try {
            $h = Invoke-RestMethod 'http://127.0.0.1:8080/actuator/health' -TimeoutSec 3
            if ($h.status -eq 'UP' -and $h.components.db.status -eq 'UP') { return $true }
        } catch { }
    }
    return $false
}

function Reset-Prod {
    Stop-App
    # **显式回到默认** —— 中途失败也不让实验参数留在生产上（09-21 踩过一次）
    $env:KB_TOP_K = '8'
    $env:KB_TWO_STAGE = 'false'
    $env:KB_CTX_IN_PROMPT = 'false'
    $env:KB_SHAPE_THINKING = 'false'
    Start-Process -FilePath $java -ArgumentList '-jar', $jar `
        -WorkingDirectory $repo -WindowStyle Hidden
    Start-Sleep -Seconds 25
    Write-Host "`n（finally：已按默认 top-k=8、三开关全关 把应用起回来）"
}

try {
    foreach ($k in $Ks) {
        Write-Host "`n========== 臂 top-k=$k ==========" -ForegroundColor Cyan
        Stop-App
        if (-not (Start-App $k)) { Write-Host "！应用没起来，跳过"; continue }
        Push-Location $repo
        node tools\multihop-live-probe.mjs
        # 每臂的原始记录要留下来 —— 探针写的是固定文件名
        $dst = "tools/_live-topk$k.json"
        Copy-Item tools\_multihop-live.json $dst -Force
        Write-Host "`n--- 判分（top-k=$k）---"
        python tools\multihop-live-score.py
        Copy-Item tools\_multihop-live.json "tools/_live-topk$k.json" -Force
        Pop-Location
    }
}
finally {
    Reset-Prod
}
