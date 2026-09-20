# 「两段式回答」的双臂对照：**质量与速度一起量**。
#
# 由来：直接读思考原文看到，decode 的大头是**同一件事换措辞说十几遍**
# （最极端那题：思考 3598 字 / 正文 58 字 = 62 倍，里面是同一句定义的反复微调）。
# 逐字指标测不到它（每遍用词都不同），形状约束也拦不住（"最多 4 条"只压了 14%）。
# **语法约束是唯一能截断这个循环的东西** —— 这就是 plan/assess 验证过的机制。
#
#   A 现状（一段：思考自由发挥 + 接着写正文）
#   B 两段式（第一段 format 约束成固定 schema 的分析；第二段照分析直接写）
#
# 两把尺子都跑：只看 latency 就是拿质量赌。
#
# 用法：powershell -File tools\two-stage-ab.ps1

$ErrorActionPreference = 'Continue'
$repo = 'D:\vs\rag-kb-java'
$jar = "$repo\rag-kb-web\target\rag-kb.jar"
$env:KB_DB_PASSWORD = (Get-Content 'D:\vs\rag-kb\data\pgapp.txt' -Raw).Trim()

function Stop-App {
    Get-CimInstance Win32_Process -Filter "name='java.exe'" |
        Where-Object { $_.CommandLine -like '*rag-kb.jar*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep -Seconds 4
}

function Start-App($two) {
    $env:KB_TWO_STAGE = $two
    Start-Process -FilePath 'C:\Program Files\Java\jdk-21\bin\java.exe' `
        -ArgumentList '-jar', $jar -WorkingDirectory $repo -WindowStyle Hidden
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Seconds 2
        try {
            $h = Invoke-RestMethod 'http://127.0.0.1:8080/actuator/health' -TimeoutSec 3
            if ($h.status -eq 'UP' -and $h.components.db.status -eq 'UP') { return $true }
        } catch { }
    }
    return $false
}

foreach ($arm in @(@{n = 'A'; on = 'false' }, @{n = 'B'; on = 'true' })) {
    Write-Host "`n========== 臂 $($arm.n)  two-stage=$($arm.on) ==========" -ForegroundColor Cyan
    Stop-App
    if (-not (Start-App $arm.on)) { Write-Host "！应用没起来，跳过这臂"; continue }

    Push-Location $repo
    Write-Host "`n--- 质量 ---"
    python tools\eval.py run answer-quality --model qwen3:4b
    Write-Host "`n--- 速度 ---"
    node tools\latency-probe.mjs --n 5 --offset 3
    Pop-Location
}

Stop-App
$env:KB_TWO_STAGE = 'false'
Start-Process -FilePath 'C:\Program Files\Java\jdk-21\bin\java.exe' `
    -ArgumentList '-jar', $jar -WorkingDirectory $repo -WindowStyle Hidden
Start-Sleep -Seconds 25
Write-Host "`n（已按默认 two-stage=false 把应用起回来）"
