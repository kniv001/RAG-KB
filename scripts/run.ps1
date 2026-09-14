# 启动本地服务（前台，Ctrl+C 停止）
$repo = Split-Path $PSScriptRoot -Parent
Set-Location $repo
& "$repo\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
