# **生产姿态 —— 唯一定义处**（2026-10-06 建）
#
# 为什么要有这个文件：这份清单此前**抄了三份**，而三份互不相同 ——
#   · `tools/ab.ps1` 的 Reset-Prod（17 个）
#   · `tools/mat-arm.ps1` 的 $PROD（12 个）
#   · 以及"人手动起应用时记得带的那几个"（就几个）
# 代价已经付过一次：**2026-09-29** 跑完材料 A/B，`mat-arm.ps1` 的 finally 重启了生产，
# 而它的清单里**没有 `KB_FEED_POLL` / `KB_WEB_ENABLED`** ⇒ 新实例的抓取是关的
# ⇒ **信息流 7 天一条没抓**（`/api/feed/stats` 的「上轮抓取：还没跑过」是铁证）。
# 而它长得完全正常：Python 版照跑、隧道照开、health 照绿。
# ⇒ **清单要么别维护、要么就得维护全 —— 而"维护全"靠人记是记不住的，所以收到一处。**
#
# 用法（**只能点源**，不能当脚本直接跑）：
#     . "$PSScriptRoot\..\scripts\prod-posture.ps1"
#     Set-KbProdPosture            # 应用姿态，返回一句人看的摘要
#
# ⚠️ 表里每加一项都要能说出理由。分两类：
#   · **值** = 生产需要它有一个非默认值（不设 = 静默少一个功能）
#   · **$null** = 实验可能设过、生产必须**删掉**这个变量（回到 yml 默认）
#     为什么用"删"而不是"设成 false"：本项目量过 —— **猜默认值会猜错**，
#     而删掉变量是"回到 yml 说了算"，那份默认值只有一处、不用在这里抄一遍。

$KB_PROD = [ordered]@{

    # ── 一、生产**需要设**的 ──────────────────────────────────────────────
    # 抓取与联网：**这两条就是 2026-09-29 那次漏掉的两个**
    KB_FEED_POLL        = 'true'
    KB_WEB_ENABLED      = 'true'

    # 分层注入（09-23 转正：装入 token −30%、时间不变、质量不变）
    KB_SENT_WINDOW      = 'true'
    # 契约进代码（09-22 起默认开；硬写 false 会让每次实验结束都把生产留在旧路上）
    KB_CONTRACT_IN_CODE = 'true'
    # 「部分可答」的措辞档（09-29 转正）+ 材料地板（09-29 转正）
    KB_PARTIAL_HINT     = 'floor'
    KB_MAT_FLOOR        = '0.65'
    KB_MAT_FLOOR_MODE   = 'sent'
    KB_SENT_NOHEAD      = 'true'
    # 逐块挑句 / 相邻块补全：**0 = 关**（两条都没转正，开关留着）
    KB_SENT_CHUNK_K     = '0'
    KB_NEIGHBOR         = '0'

    # ── 二、实验设过、生产**必须删掉**的 ──────────────────────────────────
    KB_TWO_STAGE        = $null
    KB_CTX_IN_PROMPT    = $null
    KB_SHAPE_THINKING   = $null
    KB_NO_RESTATE       = $null
    KB_SENT_ADDR        = $null
    KB_JEV_PICK         = $null
    KB_ASPECTS          = $null
    KB_SENT_M           = $null
    KB_TOP_K            = $null   # 唯一一个"数值型"的实验开关（top-k 8→16 那轮）
}

function Set-KbProdPosture {
    <#
      把当前进程的环境变量摆成生产姿态。返回一句人看的摘要。
      ⚠️ **必须打印**（调用方负责）：这份清单 09-29 那次漏了两项，而复位**什么都不说**
         —— 所以它藏了 7 天。
    #>
    $shown = @()
    foreach ($k in $KB_PROD.Keys) {
        if ($null -eq $KB_PROD[$k]) {
            Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue
            $shown += "$k=(删)"
        } else {
            Set-Item -Path "Env:$k" -Value $KB_PROD[$k]
            $shown += "$k=$($KB_PROD[$k])"
        }
    }
    return ($shown -join ' · ')
}
