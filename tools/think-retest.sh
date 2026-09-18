#!/usr/bin/env bash
# 带思考重测：同一样本、两种条件（KB_THINK=0/1）逐项对照。
#
# 背景：今天所有带 format 的探针都跑在 think:false 下 —— 语法约束把思考整个禁掉。
# 本次把「判断力类」的探针全部重跑一遍，开/关思考各一次，**样本已钉死**
# （ORDER BY md5(id::text)；hier 那四个取块数最多的那篇文档，本来就确定），
# 所以两次跑的输入逐字相同，只动 think 这一个变量。
#
# 同一块 GPU 只能串行（并行跑会互相抢显存，测出来的时间也不可用）。
# 日志：data/_think-retest/<探针>.<off|on>.log
#
# 用法：bash tools/think-retest.sh
cd "$(dirname "$0")/.." || exit 1
OUT=data/_think-retest
mkdir -p "$OUT"
echo "开始 $(date '+%F %T')"

run() {
  p="$1"; shift
  for cond in 0 1; do
    if [ "$cond" = 1 ]; then tag=on; else tag=off; fi
    s=$(date +%s)
    KB_THINK=$cond python "tools/$p" "$@" > "$OUT/$p.$tag.log" 2>&1
    rc=$?
    echo "  $p  think=$tag  退出=$rc  用时 $(( $(date +%s) - s ))s"
  done
}

run anaphora-choice-probe.py
run judge-verify-probe.py
run discourse-graph-probe.py
run hier-segment-probe.py
run hier-recursive-probe.py
run hier-kmeans-probe.py
run hier-binary-tree-probe.py

# 抽取那条线自带 think 对照（V0 现状 vs V2 开think），一次跑就够
s=$(date +%s)
KB_THINK=0 python tools/extract-fix-probe.py > "$OUT/extract-fix-probe.log" 2>&1
echo "  extract-fix-probe.py  自带对照  退出=$?  用时 $(( $(date +%s) - s ))s"

run pointing-verify-probe.py

echo "全部结束 $(date '+%F %T')"
