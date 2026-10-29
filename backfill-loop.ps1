# gameqker - 版号存量回填 · 自动续跑多轮
#
#   .\backfill-loop.ps1                          默认连跑 6 轮，每轮最多 400 款
#   .\backfill-loop.ps1 -Rounds 20               连跑 20 轮
#   .\backfill-loop.ps1 -Budget 500 -Minutes 10  调整每轮的款数 / 时长
#   .\backfill-loop.ps1 -RestSeconds 60          轮间休息 60 秒（默认 30）
#   .\backfill-loop.ps1 -Merge                   跑完后把候选并入主表
#
# 每轮执行 collector.backfill：拿全量版号名（27k 款）去 Steam 美区定向搜索，
# 补上「早已出海、不在新发售列表里」的存量游戏。进度写在 data/backfill_state.json，
# 随时 Ctrl+C 中断，下次运行会跳过已搜过的，不会重复请求。
#
# 自动停止条件（退出码）：3 = Steam 连续连不上，4 = 全量已扫完。
# 日志追加到 data/backfill-run.log。

param(
    [int]$Rounds = 6,
    [int]$Budget = 400,
    [double]$Minutes = 8,
    [int]$RestSeconds = 30,
    [switch]$Merge
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Resolve-Python {
    $candidates = @()
    foreach ($name in @('python', 'python3', 'py')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { $candidates += $command.Source }
    }
    $candidates += @(
        (Join-Path $env:USERPROFILE '.local\bin\python3.12.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:USERPROFILE 'miniconda3\python.exe'),
        (Join-Path $env:USERPROFILE 'miniforge3\python.exe')
    )
    foreach ($candidate in $candidates) {
        if (-not $candidate) { continue }
        # 跳过 Microsoft Store 的占位程序，它只会弹出应用商店
        if ($candidate -like '*\WindowsApps\*') { continue }
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    throw '未找到 Python 3.10+。请先安装 Python，或把解释器路径加进 PATH。'
}

function Invoke-WithLog {
    # 原生命令往 stderr 写内容时（比如 backfill 的「Steam 连不上」提示），
    # Stop 模式会把它当成终止错误 NativeCommandError，脚本还没读到退出码就退出了。
    # 这里临时放宽，并用 finally 保证一定会还原——否则后面任何失败都会被吞掉。
    param(
        [string[]]$Arguments,
        [string]$LogPath
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        # 逐行交给 Write-Host 并直接用 .NET 追加 UTF-8 日志：
        # - 不用 Tee-Object：它按宿主编码落盘，实测往 UTF-16 日志里混进过乱码；
        # - 输出不进管道：否则函数返回值会被整段输出顶掉，退出码分支全部失效。
        & $python @Arguments 2>&1 | ForEach-Object {
            $line = "$_"
            Write-Host $line
            [System.IO.File]::AppendAllText($LogPath, $line + [Environment]::NewLine, $utf8NoBom)
        }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

$python = Resolve-Python
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$log = Join-Path (Get-Location) 'data\backfill-run.log'

# 旧日志是 Tee-Object 写的 UTF-16，先统一转成 UTF-8，避免同一文件里混两种编码
if (Test-Path -LiteralPath $log) {
    $head = [System.IO.File]::ReadAllBytes($log)
    if ($head.Length -ge 2 -and $head[0] -eq 255 -and $head[1] -eq 254) {
        $text = [System.IO.File]::ReadAllText($log, [System.Text.Encoding]::Unicode)
        [System.IO.File]::WriteAllText($log, $text, $utf8NoBom)
    }
}

Write-Host "使用解释器: $python" -ForegroundColor DarkGray
Write-Host "计划 $Rounds 轮，每轮预算 $Budget 款 / $Minutes 分钟，日志 $log" -ForegroundColor DarkGray

$started = Get-Date
$finishedAll = $false
$executed = 0
for ($i = 1; $i -le $Rounds; $i++) {
    Write-Host ''
    Write-Host ("=== 第 {0}/{1} 轮 · {2} ===" -f $i, $Rounds, (Get-Date -Format 'MM-dd HH:mm:ss')) -ForegroundColor Cyan
    $code = Invoke-WithLog -LogPath $log -Arguments @(
        '-m', 'collector.backfill', '--pages', '13', '--budget', $Budget, '--minutes', $Minutes)
    $executed = $i

    if ($code -eq 3) {
        Write-Host 'Steam 当前连不上，先停在这儿。网络恢复后重跑同一条命令即可接着往下扫。' -ForegroundColor Yellow
        break
    }
    if ($code -eq 4) {
        Write-Host '版号存量已全部扫完。' -ForegroundColor Green
        $finishedAll = $true
        break
    }
    if ($code -ne 0) {
        Write-Host "backfill 退出码 $code，停止。" -ForegroundColor Red
        break
    }
    if ($i -lt $Rounds) {
        Write-Host "休息 $RestSeconds 秒后进入下一轮（Ctrl+C 可随时中断）..." -ForegroundColor DarkGray
        Start-Sleep -Seconds $RestSeconds
    }
}

$elapsed = [math]::Round(((Get-Date) - $started).TotalMinutes, 1)
Write-Host ''
Write-Host ("本轮循环结束：跑了 {0} 轮，用时 {1} 分钟。{2}" -f $executed, $elapsed,
    $(if ($finishedAll) { '全量已完成。' } else { '未完成的部分下次运行会自动续上。' })) -ForegroundColor Green

if ($Merge) {
    Write-Host '并入主表：collector.update --no-git' -ForegroundColor Cyan
    $code = Invoke-WithLog -LogPath $log -Arguments @('-m', 'collector.update', '--no-git')
    if ($code -ne 0) { Write-Host "update 退出码 $code" -ForegroundColor Yellow }
}
