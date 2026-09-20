# 「语境行进提示词」的双臂对照：**速度与质量一起量**。
#
# 由来：`latency-probe` 把 decode 拆开之后看到，思考的大头是**逐条扫描召回块**
# （思考原文：「[1] 提到了…但没有…」× 15 段 ≈ 700 token）。
# 而每块的语境行**本来就在库里**，只差没给模型 —— 带上它就省掉那段扫描，
# 代价从 decode（~75 token/秒）挪到 prefill（~3800 token/秒），差约 50 倍。
#
#   A 现状（语境行只进索引）
#   B 语境行随资料一起给（「[n] 来源：X（第 k 块）｜本段可回答：…」）
#
# 两把尺子都要跑：只看 latency 就是在拿质量赌（答案侧尺子正是为此而建）。
#
# 用法：powershell -File tools\ctx-prompt-ab.ps1

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

function Start-App($ctx) {
    $env:KB_CTX_IN_PROMPT = $ctx
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
    Write-Host "`n========== 臂 $($arm.n)  ctx-in-prompt=$($arm.on) ==========" -ForegroundColor Cyan
    Stop-App
    if (-not (Start-App $arm.on)) { Write-Host "！应用没起来，跳过这臂"; continue }

    Push-Location $repo
    Write-Host "`n--- 质量 ---"
    python tools\eval.py run answer-quality --model qwen3:4b
    Write-Host "`n--- 速度 ---"
    node tools\latency-probe.mjs --n 5 --offset 20
    Pop-Location
}

Stop-App
$env:KB_CTX_IN_PROMPT = 'false'
Start-Process -FilePath 'C:\Program Files\Java\jdk-21\bin\java.exe' `
    -ArgumentList '-jar', $jar -WorkingDirectory $repo -WindowStyle Hidden
Start-Sleep -Seconds 25
Write-Host "`n（已按默认 ctx-in-prompt=false 把应用起回来）"
