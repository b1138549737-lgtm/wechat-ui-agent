# wxbot 开机自启（T231）
# 用法：
#   powershell -ExecutionPolicy Bypass -File autostart.ps1 -Enable           # 装（默认 8765 端口）
#   powershell -ExecutionPolicy Bypass -File autostart.ps1 -Enable -Port 9000
#   powershell -ExecutionPolicy Bypass -File autostart.ps1 -Disable          # 卸
#   powershell -ExecutionPolicy Bypass -File autostart.ps1 -Status           # 看
# 说明：在"启动"文件夹里放一个 cmd，开机登录后拉起来的是 Web 控制面板（内含常驻循环）。
#       单实例锁会挡住重复启动，所以手动再开一个也不会出现两个进程抢微信界面。
param(
    [switch]$Enable,
    [switch]$Disable,
    [switch]$Status,
    [int]$Port = 8765,
    [string]$PythonPath = ""
)
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$startup = [Environment]::GetFolderPath('Startup')
$cmdPath = Join-Path $startup 'wxbot-autostart.cmd'
$py = $PythonPath
if (-not $py) {
    $py = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $py)) { $py = Join-Path $root '.venv312\Scripts\python.exe' }
}
$logDir = Join-Path $root 'data'

function Show-Status {
    Write-Host "启动文件夹: $startup"
    if (Test-Path $cmdPath) {
        Write-Host "✅ 已开启开机自启: $cmdPath" -ForegroundColor Green
        Get-Content $cmdPath | ForEach-Object { Write-Host "   $_" }
    } else {
        $run = (Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' `
                -Name 'wxbot' -ErrorAction SilentlyContinue).wxbot
        if ($run) { Write-Host "✅ 已开启开机自启（注册表 Run）: $run" -ForegroundColor Green }
        else { Write-Host "· 未开启开机自启" -ForegroundColor Yellow }
    }
    # ★2026-09-27 审查：原来只看"命令行里有 wxbot.cli web"就报"在跑"，实测把空转残留
    # （0 CPU、不持锁、不占端口）也报了进来。改成**按端口判定**：8765 真的在监听才算在跑。
    $listen = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -eq $Port }
    if ($listen) {
        $pids = ($listen | Select-Object -ExpandProperty OwningProcess -Unique) -join ', '
        Write-Host "✅ 面板在跑（端口 $Port 正在监听，PID $pids）" -ForegroundColor Green
    } else {
        $stale = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like '*wxbot.cli*web*' }
        if ($stale) {
            Write-Host "⚠️ 端口 $Port 没人监听，但有 $($stale.ProcessId.Count) 个 wxbot 进程残留（空转）: $($stale.ProcessId -join ', ')" -ForegroundColor Yellow
            Write-Host "   建议：Stop-Process -Id $($stale.ProcessId -join ',') -Force 之后重新启动" -ForegroundColor Yellow
        } else {
            Write-Host "· 面板没在跑（端口 $Port 无监听）" -ForegroundColor Yellow
        }
    }
}

if ($Status -or (-not $Enable -and -not $Disable)) { Show-Status; exit 0 }

if ($Disable) {
    $removed = 0
    if (Test-Path $cmdPath) { Remove-Item -LiteralPath $cmdPath -Force; $removed++ }
    try {
        Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' `
            -Name 'wxbot' -ErrorAction Stop
        $removed++
    } catch { }
    if ($removed) { Write-Host "已移除开机自启（$removed 处）" } else { Write-Host "本来就没开" }
    exit 0
}

if (-not (Test-Path $py)) {
    Write-Host "❌ 找不到虚拟环境里的 python：$py（先跑 install.ps1）" -ForegroundColor Red
    exit 1
}
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
# ★工单第 6⑤ 条：autostart.log 没有上限 —— 常驻跑久了会一直涨。
# 超过 2MB 就轮转一次，只留上一份 autostart.log.old，不占磁盘。
$autoLog = Join-Path $logDir 'autostart.log'
if ((Test-Path $autoLog) -and ((Get-Item $autoLog).Length -gt 2MB)) {
    Move-Item -LiteralPath $autoLog -Destination "$autoLog.old" -Force
}
# 启动器固定放在工程目录（这里一定可写），自启项只负责调用它
$launcher = Join-Path $root 'run-web.cmd'
# ★工单第 6④ 条：覆盖已有的 run-web.cmd 前先留一份 .bak（用户可能手工改过端口/参数）
if (Test-Path $launcher) { Copy-Item -LiteralPath $launcher -Destination "$launcher.bak" -Force }
$launcherLines = @(
    '@echo off',
    "cd /d `"$root`"",
    "if not exist `"data`" mkdir `"data`"",
    "`"$py`" -m wxbot.cli web --port $Port --seconds 0 >> `"$logDir\autostart.log`" 2>&1"
)
Set-Content -LiteralPath $launcher -Value $launcherLines -Encoding OEM -ErrorAction SilentlyContinue
if (-not (Test-Path $launcher)) {
    Write-Host "❌ 连工程目录里的 run-web.cmd 都写不了：$launcher" -ForegroundColor Red
    exit 1
}
$installed = ""
# 先试启动文件夹；写不进去（权限/沙箱）就退到 HKCU 的 Run 键
try {
    Set-Content -LiteralPath $cmdPath -Value @('@echo off', "call `"$launcher`"") `
        -Encoding OEM -ErrorAction Stop
    if (Test-Path $cmdPath) { $installed = "启动文件夹：$cmdPath" }
} catch {
    Write-Host "· 启动文件夹写不进去：$($_.Exception.Message)" -ForegroundColor Yellow
}
if (-not $installed) {
    try {
        New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' `
            -Name 'wxbot' -Value "`"$launcher`"" -PropertyType String -Force -ErrorAction Stop |
            Out-Null
        $installed = "注册表：HKCU\Software\Microsoft\Windows\CurrentVersion\Run\wxbot"
    } catch {
        Write-Host "· 注册表也写不进去：$($_.Exception.Message)" -ForegroundColor Yellow
    }
}
if (-not $installed) {
    Write-Host "❌ 开机自启没装上（两处都被拒绝）。可以手动把下面这行做成快捷方式放进启动文件夹：" -ForegroundColor Red
    Write-Host "   $launcher" -ForegroundColor Yellow
    exit 1
}
Write-Host "✅ 已开启开机自启（登录后自动拉起控制面板）：$installed" -ForegroundColor Green
Show-Status
