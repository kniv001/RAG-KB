# 启动 Cloudflare 快速隧道（前台，Ctrl+C 停止）
# 出站长连接，不开放任何入站端口；启动后会打印一个 https://xxx.trycloudflare.com 地址
$repo = Split-Path $PSScriptRoot -Parent
& "$repo\bin\cloudflared.exe" tunnel --url http://127.0.0.1:8000 --no-autoupdate
