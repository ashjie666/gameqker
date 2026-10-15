# gameqker - 国产游戏出海信息差情报表 · 一键运行
#
#   .\run.ps1                巡检一次，然后启动本地预览服务器
#   .\run.ps1 -UpdateOnly    只巡检，不启动预览服务器
#   .\run.ps1 -Probe         只体检数据源（Steam / 官网），不写任何文件
#   .\run.ps1 -DryRun        完整跑一遍流程，但不写文件
#   .\run.ps1 -NoCache       忽略本地 HTTP 缓存，强制重新请求
#   .\run.ps1 -BudgetSeconds 300 -Port 9000
#
# 本机没有把 python 加进 PATH 时，脚本会自动回退到 %USERPROFILE%\.local\bin\python3.12.exe。

param(
    [switch]$UpdateOnly,
    [switch]$Probe,
    [switch]$DryRun,
    [switch]$NoCache,
    [int]$BudgetSeconds = 180,
    [int]$Port = 8322
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

$python = Resolve-Python
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "使用解释器: $python" -ForegroundColor DarkGray

$updateArgs = @('-m', 'collector.update', '--budget-seconds', $BudgetSeconds)
if ($Probe)   { $updateArgs += '--probe' }
if ($DryRun)  { $updateArgs += '--dry-run' }
if ($NoCache) { $updateArgs += '--no-cache' }

& $python @updateArgs
if ($LASTEXITCODE -ne 0) { throw "巡检退出码 $LASTEXITCODE" }

if ($Probe -or $DryRun -or $UpdateOnly) { return }

Write-Host ''
Write-Host "预览地址: http://localhost:$Port/" -ForegroundColor Green
Write-Host '按 Ctrl+C 停止服务器。' -ForegroundColor DarkGray
& $python -m http.server $Port --directory web
