param(
    # 每一臂写成  "名字|KB_X=1;KB_Y=2"，臂之间用 **@@** 分隔（没有开关就写 "名字|"）。
    #
    # ⚠️ **不用 `[string[]]` + 逗号**（2026-09-29 踩过）：从 bash 用
    # `powershell -File … -Arms 'a','b','c'` 传进来时，三个值会被**当成一个字符串**
    # 绑到一个 `[string[]]` 元素上 ⇒ 四臂并成一臂、臂名是 `off　,k5|KB_SENT_CHUNK_K=5,…`，
    # 而它**照样跑完、照样出数**（那次它环境变量名非法，等于跑了个基线）——
    # 正是本项目最熟的一族：**仪器坏了不报错**。分隔符由脚本自己切，别再指望调用方的引号。
    [Parameter(Mandatory = $true)][string]$Arms,
    [string]$Bench = 'answer-quality'
)
# **材料臂的编排** —— 与 ab.ps1 同一条纪律，但采的是**材料**不是答案。
#
# 为什么不复用 ab.ps1：那个跑的是 `eval.py run`（21 题 × ~25s，八成时间是生成），
# 而材料在**生成之前**就定完了 —— 服务端那一刻就打了日志
# （`材料地板 0.65（…）：过滤后剩 N 句 / M 块`）⇒ 本题只等 9 秒、**不等生成**。
# 判据要什么就采什么：多采的部分不只浪费，还会把生成抖动引进来。
#
# ⚠️ **收尾一律放 finally**，且按"**生产当前默认**"复位、不按 false ——
# ab.ps1 开头那段记的就是这个坑（实验中途退出 ⇒ 生产留在实验路径上，而之后
# 采到的"基线"全是另一条路的数）。本脚本**只碰下面 $PROD 里列的那几个开关**，
# 每加一个臂开关就要在这里同步加一行。
$ErrorActionPreference = 'Continue'
# **控制台是 GBK** —— 与 Python 那两个探针开头做的是同一件事：
# 不设这个，`⇒`/中文一进管道就成乱码（本项目在 `material-ab.py` 上踩过一次：
# 整臂的输出乱掉，而报出来的是另一回事）。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$repo = 'D:\vs\rag-kb-java'
$jar = Join-Path $repo 'rag-kb-web\target\rag-kb.jar'
$java = 'C:\Program Files\Java\jdk-21\bin\java.exe'
$env:KB_DB_PASSWORD = (Get-Content 'D:\vs\rag-kb\data\pgapp.txt' -Raw).Trim()

# **生产姿态不写在这里** —— 点源 `scripts\prod-posture.ps1`（唯一定义处）。
# 2026-10-06 之前这里是**本地抄的一份**，而它漏了 `KB_FEED_POLL`/`KB_WEB_ENABLED`
# ⇒ 本脚本那次复位重启把生产留在了"抓取关着"的状态，**信息流 7 天一条没抓**。
. (Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts\prod-posture.ps1')

function Stop-App {
    Get-CimInstance Win32_Process -Filter "name='java.exe'" |
        Where-Object { $_.CommandLine -like '*rag-kb.jar*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep -Seconds 4
}

function Start-App {
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

function Apply-Prod {
    # **打印出来**：清单漏了一项时，至少在日志里看得见"它没被设"
    # （2026-10-06 那次就是因为清单漏了两项、而复位**什么都不说**，7 天后才发现）
    Write-Host "    复位到生产：$(Set-KbProdPosture)"
}

function Stamp { (Get-Date).ToString('HHmmss') }

Push-Location $repo
try {
    foreach ($spec in ($Arms -split '@@')) {
        $name, $pairs = $spec.Split('|', 2)
        Write-Host "`n========== 臂 $name　$pairs ==========" -ForegroundColor Cyan
        Stop-App
        Apply-Prod                      # 先回默认，再叠加本臂的开关（不叠加就不知道差在哪）
        foreach ($kv in ($pairs -split ';')) {
            if ($kv.Trim() -eq '') { continue }
            $k, $v = $kv.Split('=', 2)
            Set-Item -Path "Env:$($k.Trim())" -Value $v.Trim()
            Write-Host "    $($k.Trim()) = $($v.Trim())"
        }
        if (-not (Start-App)) { Write-Host "！应用没起来，跳过这臂" -ForegroundColor Red; continue }
        $out = Join-Path $repo "data\_mat_${Bench}_${name}_$(Stamp).log"
        # **按超集删旧落盘**：材料探针自己会清答案缓存，但日志是本脚本写的，
        # 同名会追加/覆盖混起来 —— 索性每次一个新文件名（带时刻），旧的原样留着当历史。
        python tools\eval\bench-material-probe.py $Bench 2>&1 | Tee-Object -FilePath $out
        Write-Host "（本臂日志：$out）"
    }
}
finally {
    Write-Host "`n========== 复位到生产默认 ==========" -ForegroundColor Yellow
    Stop-App
    Apply-Prod
    Start-App | Out-Null
    $h = try { (Invoke-RestMethod 'http://127.0.0.1:8080/actuator/health' -TimeoutSec 5).status } catch { 'DOWN' }
    Write-Host "（已复位，health=$h）"
}
Pop-Location
