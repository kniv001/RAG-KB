# PostgreSQL 启停控制
#   .\scripts\pg.ps1 start | stop | status | restart
#
# 注意：start 刻意不用 `pg_ctl start`。pg_ctl 启动的 postgres 进程会继承 stdout 句柄，
# 导致 shell 管道永不关闭、命令挂死（踩过一次）。改用 Start-Process 完全脱离。

param([ValidateSet('start', 'stop', 'status', 'restart')][string]$Action = 'status')

$repo = Split-Path $PSScriptRoot -Parent
$pg = "$repo\pgsql"
$data = "$repo\pgdata"

function Get-PgUp {
    [bool](Get-NetTCPConnection -LocalPort 5432 -State Listen -ErrorAction SilentlyContinue)
}

function Start-Pg {
    if (Get-PgUp) { Write-Host "postgres already listening on 127.0.0.1:5432"; return }
    Start-Process -FilePath "$pg\bin\postgres.exe" `
        -ArgumentList '-D', $data, '-p', '5432', '-c', 'listen_addresses=127.0.0.1' `
        -WindowStyle Hidden `
        -RedirectStandardOutput "$data\pg.out.log" -RedirectStandardError "$data\pg.err.log"
    for ($i = 0; $i -lt 24; $i++) {
        Start-Sleep -Milliseconds 500
        if (Get-PgUp) { break }
    }
    if (Get-PgUp) { Write-Host "postgres started -> 127.0.0.1:5432" -ForegroundColor Green }
    else { Write-Host "postgres FAILED to start - check pgdata\pg.err.log" -ForegroundColor Red }
}

function Stop-Pg {
    if (-not (Get-PgUp)) { Write-Host "postgres not running"; return }
    & "$pg\bin\pg_ctl.exe" -D $data stop -m fast
}

switch ($Action) {
    'start' { Start-Pg }
    'stop' { Stop-Pg }
    'restart' { Stop-Pg; Start-Sleep -Seconds 2; Start-Pg }
    'status' {
        if (Get-PgUp) {
            Write-Host "postgres : UP   127.0.0.1:5432" -ForegroundColor Green
            & "$pg\bin\pg_ctl.exe" -D $data status
        } else {
            Write-Host "postgres : DOWN" -ForegroundColor Red
        }
    }
}
