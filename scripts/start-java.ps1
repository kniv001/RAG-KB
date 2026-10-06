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
function Poll-Lines-Since($t) {
    if (-not (Test-Path $applog)) { return 0 }
    $n = 0
    try {
        Get-Content $applog -Tail 400 -ErrorAction Stop | ForEach-Object {
            if ($_ -match '^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}:\d{2})' -and $_ -match '信息流轮询：') {
                if ([datetime]::ParseExact($matches[1], 'HH:mm:ss', $null) -ge $t) { $n++ }
            }
        }
    } catch { }
    return $n
}
$ts = $t0.ToString('HH:mm:ss')
$hit = 0
$deadline = (Get-Date).AddSeconds($VerifyWaitSec)
while ((Get-Date) -lt $deadline) {
    $hit = Poll-Lines-Since $ts
    if ($hit -gt 0) { break }
    Start-Sleep -Seconds 10
}
if ($hit -gt 0) {
    Say "✅ 抓取确认在跑（$ts 之后有 $hit 行『信息流轮询』）"
} elseif ($Verify) {
    Say "⚠️ 等了 $VerifyWaitSec 秒，日志里**没有**『信息流轮询』的新行"
    Say "   ⇒ 抓取大概没跑。先查：/api/feed/stats 的『上轮抓取』是不是『还没跑过』"
    exit 5
} else {
    Say "（没开 -Verify，不抓取没验证 —— 首轮在启动后 2 分钟）"
}
Say "---- start-java 结束 ----"
