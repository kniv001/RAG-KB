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
    # **两臂的值可以不是 true/false** —— 数值开关（如 KB_SENT_CHUNK_K 的 0/3）也要能测。
    # 默认保持 false/true，所以既有的用法一个字不用改。
    [string]$A = 'false',
    [string]$B = 'true',
    [int]$Offset = 3,          # 速度那段的题目起点（错开，避开答案缓存）
    [int]$SpeedN = 5,
    [int]$Repeat = 1,          # 质量采集重复几次（>1 时「全部通过」才算过）
    # **每轮实验的文件名前缀**（默认 `__armA` / `__armB`）。
    # 为什么要有它：落盘名是 `{bench}__{model}{tag}.json`，两轮不同的实验用同一个
    # 默认 tag ⇒ **后一轮直接覆盖前一轮**（2026-09-23 实测：ctx-in-prompt 那一轮
    # 差点把 no-restate 那一轮的两臂覆盖掉，靠手工 cp 抢下来的）。
    # 跑新实验时给个前缀，例如 -TagPrefix __nr ⇒ `...__nr-armA.json`。
    [string]$TagPrefix = '',
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
    # **姿态不写在这里** —— 点源 `scripts\prod-posture.ps1`（唯一定义处）。
    #
    # 2026-10-06 之前这里是**本地抄的一份**（17 行），而 `tools\mat-arm.ps1` 里另抄了一份
    # （12 行）—— **两份互不相同**，而且**两份都没有 `KB_FEED_POLL`/`KB_WEB_ENABLED`**。
    # 代价：09-29 那次复位重启把生产留在"抓取关着"的状态 ⇒ **信息流 7 天一条没抓**，
    # 而它长得完全正常（health 绿、隧道通、日志每天 ~480 字节）。
    # ⇒ 抄三份必然对不上；收到一处之后，"生产姿态"只有一个来源。
    . (Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts\prod-posture.ps1')

    # ⚠️ **本次实验用的那个开关也要删掉**，让它回到 yml 默认（2026-09-24 的教训：
    # 那次跑 `-Switch KB_TOP_K` 而清单里没有它，生产被留在 top-k=24，装入 token 翻倍）。
    # 这条与上面的姿态表**互补**：表管"所有历史上被设过的开关"，这条管"这一次的"。
    Remove-Item -Path "Env:$Switch" -ErrorAction SilentlyContinue
    if ($KB_PROD.Contains($Switch)) { Write-Host "（$Switch 也在姿态表里，已被表覆盖）" }
    Write-Host "    复位到生产：$(Set-KbProdPosture)"
    Start-Process -FilePath $java -ArgumentList '-jar', $jar `
        -WorkingDirectory $repo -WindowStyle Hidden
    Start-Sleep -Seconds 25
    # 这一行原来是"（已复位：前三个开关关、契约进代码=默认开）" ——
    # 那是**照着本地清单写的散文**，而清单已经删了。姿态的真相由 Set-KbProdPosture 逐项打印。
    Write-Host "`n（已复位 —— 逐项见上面那行「复位到生产：…」）"
}

try {
    foreach ($arm in @(@{ n = 'A'; on = $A }, @{ n = 'B'; on = $B })) {
        Write-Host "`n========== 臂 $($arm.n)　$Switch=$($arm.on) ==========" -ForegroundColor Cyan
        Stop-App
        if (-not (Start-App $arm.on)) { Write-Host "！应用没起来，跳过这臂"; continue }

        Push-Location $repo
        # **每臂开跑前清空答案缓存** —— 否则第二次跑同一个臂键完全相同、全命中，
        # 报出来的耗时是回放（实测：臂 A 21 题里 14 题命中，耗时中位 2.6s 毫无意义）。
        # 它**只污染速度不污染质量**（命中的是同一份提示词的答案），所以最容易漏。
        node tools\clear-answers.mjs
        if (-not $SkipQuality) {
            Write-Host "`n--- 质量 ---"
            # **开跑前删掉本臂的旧落盘** —— 采集端（collect.mjs）有**断点续跑**：
            # 它看到目标文件里已有这道题就直接跳过。而落盘名只由 bench+model+tag 决定，
            # 于是**同一轮实验跑第二次时，整臂会"续跑"成上一次的数据**，
            # 报出来的分数和耗时看起来完全正常（2026-09-23 实测：ctx-in-prompt 那一轮的
            # 臂 A 报 21/21、24.1s，其实是 no-restate 那一轮臂 A 的数 —— **它一题都没跑**）。
            # 这与本项目反复吃的亏同族：**仪器坏掉时不报错，只让结果悄悄变旧**。
            # ⚠️ **要按通配删，不能按精确名**：`-Repeat N`（N>1）时落盘名中间多一段
            # `__r1`/`__r2`（`…__r1__swp__armA.json`），精确名**一个都删不到** ——
            # 于是又回到本段开头那个坑：续跑把整臂换成上一次的数据。
            $stalePat = Join-Path $repo "tools\eval\_runs\answer-quality__qwen3-4b*$TagPrefix`__arm$($arm.n).json"
            @(Get-ChildItem -Path $stalePat -ErrorAction SilentlyContinue) | ForEach-Object {
                Write-Host "（已删除上一轮同名落盘：$($_.Name)）"
                Remove-Item $_.FullName -Force
            }
            # **每臂留 tag**：不然第二臂会覆盖第一臂的落盘结果，A/B 只剩后一臂
            python tools\eval.py run answer-quality --model qwen3:4b --repeat $Repeat --tag "$TagPrefix`__arm$($arm.n)"
        }
        Write-Host "`n--- 速度 ---"
        node tools\latency-probe.mjs --n $SpeedN --offset $Offset
        Pop-Location
    }
}
finally {
    Reset-Prod
}
