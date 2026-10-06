# **起 Java 版的生产实例**（2026-10-06 建）—— 开机自启、手动、以及实验脚本复位**都走这一个**。
#
# 为什么需要它：**抓取只活在 Java 版里**（`FeedPoller` 是应用内的 `@Scheduled`），
# 而 Java 版此前**没有任何自启** —— Python 版有一条 VBS 自启（`rag-kb\scripts\start.ps1`，
# 09-15 建，已连续跑 21 天），Java 版每次都是"谁想起来谁手动起"。
# ⇒ 一旦它被实验脚本重启、或机器重启，**没有任何东西会把它拉回来**；
#   而它静默地停着时，Python 版照跑、隧道照开、`/actuator/health` 照绿。
#   2026-09-29 那次就是这样丢了 7 天的抓取。
#
# 姿态**不写在本文件里** —— 点源 `scripts\prod-posture.ps1`（唯一定义处）。
#
# 用法：
#     powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-java.ps1
#     powershell ... -File scripts\start-java.ps1 -IfRunning Skip     # 已经在跑就不动它
#     powershell ... -File scripts\start-java.ps1 -Verify             # 再等它抓完第一轮
#
# ⚠️ 仓库是**公开的** —— 本文件不写任何地址、密钥；密码从仓库外的文件读。
param(
    [ValidateSet('Restart', 'Skip')][string]$IfRunning = 'Restart',
    # 起完之后**等第一轮抓取真的跑完**（判据落在日志的新行上），再写"就绪"。
    # 开机自启建议带上：多花两三分钟，换"它到底在不在抓"这个事实。
    [switch]$Verify,
    [int]$VerifyWaitSec = 300
)
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$repo = Split-Path $PSScriptRoot -Parent
$jar = Join-Path $repo 'rag-kb-web\target\rag-kb.jar'
$java = 'C:\Program Files\Java\jdk-21\bin\java.exe'
$log = Join-Path $repo 'data\_start_java.log'
$applog = Join-Path $repo 'data\app.log'

function Say($msg) {
    $line = "[{0}] {1}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding UTF8
}

function App-Pids {
    Get-CimInstance Win32_Process -Filter "name='java.exe'" |
        Where-Object { $_.CommandLine -like '*rag-kb.jar*' } |
        Select-Object -ExpandProperty ProcessId
}

function Health {
    try { (Invoke-RestMethod 'http://127.0.0.1:8080/actuator/health' -TimeoutSec 3).status }
    catch { 'DOWN' }
}

Say "---- start-java 开始 ----"

# ── 0) 已经在跑？───────────────────────────────────────────────────────────
$pids = @(App-Pids)
if ($pids.Count -gt 0 -and $IfRunning -eq 'Skip' -and (Health) -eq 'UP') {
    Say "已经在跑（PID $($pids -join ',')）且 health=UP ⇒ -IfRunning Skip，不动它"
    return
}

# ── 1) 姿态（唯一定义处）───────────────────────────────────────────────────
. (Join-Path $PSScriptRoot 'prod-posture.ps1')

# ── 2) 凭据：从**仓库外**读（仓库是公开的）────────────────────────────────
$pgpass = 'D:\vs\rag-kb\data\pgapp.txt'
if (-not (Test-Path $pgpass)) { Say "！找不到密码文件 $pgpass —— 起不来"; exit 2 }
$env:KB_DB_PASSWORD = (Get-Content $pgpass -Raw).Trim()

# ── 3) 停旧 ────────────────────────────────────────────────────────────────
if ($pids.Count -gt 0) {
    Say "停旧实例：PID $($pids -join ',')"
    $pids | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 4
}

# ── 4) 应用姿态并**打印出来**（漏了至少看得见 —— 09-29 那次就是因为复位什么都不说）──
Say "生产姿态：$(Set-KbProdPosture)"

# ── 5) 起 ──────────────────────────────────────────────────────────────────
if (-not (Test-Path $jar)) { Say "！找不到 jar：$jar —— 先 mvn package"; exit 3 }

# **等 PostgreSQL 先起来**（开机自启时这条是必须的）：
# 启动文件夹里的两条自启（Python 版那条 / 本脚本）**顺序没有保证**，
# 而 PG 是 Python 版那条里的 `pg.ps1 start` 拉起来的。
# 少了这一步，开机会出现"Java 先起、连不上库、起不来"，而**没有任何重试**。
$pgUp = $false
for ($i = 0; $i -lt 30; $i++) {
    if (Get-NetTCPConnection -LocalPort 5432 -State Listen -ErrorAction SilentlyContinue) {
        $pgUp = $true; break
    }
    if ($i -eq 0) { Say "等 PostgreSQL（5432）……" }
    Start-Sleep -Seconds 2
}
if (-not $pgUp) { Say "！60 秒内 5432 没在听 —— 先跑 rag-kb\scripts\pg.ps1 start"; exit 6 }
Say "PostgreSQL 已在听（5432）"

$t0 = Get-Date
Start-Process -FilePath $java -ArgumentList '-jar', $jar -WorkingDirectory $repo -WindowStyle Hidden
Say "已启动（jar 时间戳 $(Get-Item $jar | Select-Object -ExpandProperty LastWriteTime)）"

$up = $false
for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep -Seconds 2
    if ((Health) -eq 'UP') { $up = $true; Say "health=UP（等了 $(($i+1)*2) 秒）"; break }
}
if (-not $up) { Say "！120 秒内 health 没 UP —— 看 data\app.log"; exit 4 }

# ── 6) 验证"它到底在不在抓"（判据 = **日志里的新行**，不是我们设了什么）────
# 为什么不信"我设了 KB_FEED_POLL=true"：09-29 那次开关就是没生效，而**没有任何东西报警**。
# 唯一骗不了的判据是 FeedPoller 自己打的那行日志，且时间戳在本轮之后。
function Poll-Lines-Since([datetime]$t) {
    <#
      ⚠️ **两个 `-match` 不能连着写**（2026-10-06 自己踩的）：
      `if ($_ -match A -and $_ -match B)` 之后 `$matches` 里装的是 **B** 的结果，
      而 B 没有捕获组 ⇒ `$matches[1]` 是空 ⇒ 时间比较恒假 ⇒ **永远报"没在抓"**。
      ⇒ 先把时间戳**存进变量**，再判第二个模式。

      ⚠️ **必须写 `-Encoding UTF8`**（2026-10-06 抓到的真凶）：
      PS 5.1 的 `Get-Content` 对**没有 BOM** 的文件按 **ANSI（本机是 GBK）** 解码，
      而 Logback 写的 `app.log` 正是 **UTF-8 无 BOM** ⇒ **中文全成乱码** ⇒
      `-match '信息流轮询：'` 恒为假。而**时间戳是 ASCII、照常匹配** ——
      所以仪器看起来"在读文件、在解析时间戳"，**只是永远找不到那一行**，
      最后报出一句看着像结论的话：「抓取大概没跑」（而它跑着）。
      ⇒ 本项目的 `tools/ruler/corpus.py` 早就为 psql 记过同一条（CRLF、单遍反转义），
        这次是它在**日志**这一侧的第二次。

      ⚠️ **失败时要把"看见了什么"带出来**：
      第一版只 `return 0`，于是"没找到"和"根本没读对文件"长得一模一样 ——
      而判据是"抓取在不在跑"，那是个**会被写进结论**的答案。
      ⇒ 返回一个**带诊断的**对象，而不是一个数字。（正是这个诊断把编码揪出来的：
        它报"读入 2623 行、时间戳匹配 2429 行、最后一个时间戳…"，而命中 0。）
    #>
    $diag = [ordered]@{ hit = 0; exists = $false; scanned = 0; tsMatched = 0; lastTs = '' }
    $diag.exists = Test-Path $applog
    if (-not $diag.exists) { return $diag }
    try {
        Get-Content $applog -Encoding UTF8 -ErrorAction Stop | ForEach-Object {
            $diag.scanned++
            if ($_ -match '^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})') {
                $diag.tsMatched++
                $lineTs = $matches[1]
                $diag.lastTs = $lineTs
                if ($_ -match '信息流轮询：') {
                    $d = [datetime]::ParseExact($lineTs, 'yyyy-MM-ddTHH:mm:ss', $null)
                    if ($d -ge $t) { $diag.hit++ }
                }
            }
        }
    } catch { $diag.err = $_.Exception.Message }
    return $diag
}
$deadline = (Get-Date).AddSeconds($VerifyWaitSec)
$dg = $null
while ((Get-Date) -lt $deadline) {
    $dg = Poll-Lines-Since $t0
    if ($dg.hit -gt 0) { break }
    Start-Sleep -Seconds 10
}
if ($dg -and $dg.hit -gt 0) {
    Say "✅ 抓取确认在跑（$($t0.ToString('HH:mm:ss')) 之后有 $($dg.hit) 行『信息流轮询』）"
} elseif ($Verify) {
    Say "⚠️ 等了 $VerifyWaitSec 秒，没找到『信息流轮询』的新行。**它看见了什么**："
    if ($dg) {
        Say ("   日志 $applog · 存在=$($dg.exists) · 读入 $($dg.scanned) 行 · " +
             "时间戳匹配 $($dg.tsMatched) 行 · 最后一个时间戳 [$($dg.lastTs)]" +
             $(if ($dg['err']) { " · 读取出错：$($dg['err'])" } else { "" }))
    }
    Say "   ⇒ 若上面『读入行数』是 0，是**读不到文件**（仪器问题）；否则才是抓取真没跑。"
    exit 5
} else {
    Say "（没开 -Verify，抓取没验证 —— 首轮在启动后 2 分钟）"
}
Say "---- start-java 结束 ----"
