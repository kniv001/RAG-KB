# 「思考形状」的双臂对照：**速度与质量一起量**。
#
# 为什么要脚本：两臂各要重启一次应用（开关是启动期读的环境变量），
# 而每臂要跑**两把尺子** —— 只看 latency 就是在拿质量赌（答案侧尺子正是为此而建）。
#
#   A 现状（思考自由发挥）
#   B 思考条目化：最多 4 条短清单，写完立刻进正文
#
# 判据：decode token 数 / 思考占比 / 总耗时（速度） + answer-quality 通过率（质量）
#
# 用法：powershell -File tools\shape-think-ab.ps1

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

function Start-App($shape) {
    $env:KB_SHAPE_THINKING = $shape
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
    Write-Host "`n========== 臂 $($arm.n)  shape-thinking=$($arm.on) ==========" -ForegroundColor Cyan
    Stop-App
    if (-not (Start-App $arm.on)) { Write-Host "！应用没起来，跳过这臂"; continue }

    Push-Location $repo
    Write-Host "`n--- 速度 ---"
    node tools\latency-probe.mjs --n 5 --offset 0
    Write-Host "`n--- 质量 ---"
    python tools\eval.py run answer-quality --model qwen3:4b
    Pop-Location
}

Stop-App
$env:KB_SHAPE_THINKING = 'false'
Start-Process -FilePath 'C:\Program Files\Java\jdk-21\bin\java.exe' `
    -ArgumentList '-jar', $jar -WorkingDirectory $repo -WindowStyle Hidden
Start-Sleep -Seconds 25
Write-Host "`n（已按默认 shape-thinking=false 把应用起回来）"
