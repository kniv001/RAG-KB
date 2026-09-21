# 双臂对照的**通用**脚本：质量与速度一起量。
#
# 为什么合并成一个：原先三个开关各有一份脚本（two-stage / ctx-prompt / shape-think），
# 内容九成相同、各自维护。而 2026-09-21 踩到一个更重要的坑 ——
#
#   **一个中途失败的实验脚本不该改变生产的行为。**
#   two-stage 那次 A/B 中途退出（exit 255），收尾那步没执行，于是**生产上一直跑着
#   `KB_TWO_STAGE=true`**，而之后一次"基线"采集采到的全是两段式的答案（短、无引用），
#   与任何历史数都对不上 —— 查了半天才发现是配置残留。
#
# 所以本脚本：① 收尾一律放 `finally`；② 收尾时**显式把三个开关全设成 false**；
# ③ 中途 `continue` 也走 finally。
#
# 用法：powershell -File tools\ab.ps1 -Switch KB_TWO_STAGE [-Offset 3] [-Quality 0]

param(
    [Parameter(Mandatory = $true)][string]$Switch,
    [int]$Offset = 3,          # 速度那段的题目起点（错开，避开答案缓存）
    [int]$SpeedN = 5,
    [switch]$SkipQuality
)

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

function Start-App($on) {
    Set-Item -Path "Env:$Switch" -Value $on
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
    # **三个开关显式全关** —— 不靠"没设就是默认"，那正是残留能溜进来的地方
    $env:KB_TWO_STAGE = 'false'
    $env:KB_CTX_IN_PROMPT = 'false'
    $env:KB_SHAPE_THINKING = 'false'
    Start-Process -FilePath $java -ArgumentList '-jar', $jar `
        -WorkingDirectory $repo -WindowStyle Hidden
    Start-Sleep -Seconds 25
    Write-Host "`n（已按**三个开关全关**把应用起回来）"
}

try {
    foreach ($arm in @(@{ n = 'A'; on = 'false' }, @{ n = 'B'; on = 'true' })) {
        Write-Host "`n========== 臂 $($arm.n)　$Switch=$($arm.on) ==========" -ForegroundColor Cyan
        Stop-App
        if (-not (Start-App $arm.on)) { Write-Host "！应用没起来，跳过这臂"; continue }

        Push-Location $repo
        if (-not $SkipQuality) {
            Write-Host "`n--- 质量 ---"
            python tools\eval.py run answer-quality --model qwen3:4b
        }
        Write-Host "`n--- 速度 ---"
        node tools\latency-probe.mjs --n $SpeedN --offset $Offset
        Pop-Location
    }
}
finally {
    Reset-Prod
}
