# WeFlow -105 一键修复（2026-09-26 第二版：先重启，别动不动删注册表）
#
# 症状：WeFlow 打得开但读不了数据；%APPDATA%\weflow\logs\wcdb.log 里刷
#       [bootstrap] native runtime policy mismatch value=-105
#       （界面报「WCDB 打开失败 / 错误码 -105」）
#
# ★ 第一版结论已被推翻。2026-09-26 18:29 的受控实验：
#   上次（14:33）我们做的是"删锚点 + 重启"两个动作，没法区分是谁起的作用。
#   这次先记下锚点指纹，然后**只重启、不碰注册表** → 25 秒内就恢复正常，
#   而且锚点指纹自己变了（1530b945ea48a67f → bcac7f9a44073833）。
#   结论：**-105 不用删注册表，重启就能好**；删锚点只留作"重启无效"时的兜底。
#   时间线还显示：每次犯病都是"某个会话的第一次启动"卡住，之后它自己每 40 秒重试、
#   永远不成功；只要把进程重启一次就好。
#
# 用法：powershell -ExecutionPolicy Bypass -File repair-weflow.ps1
#       powershell -ExecutionPolicy Bypass -File repair-weflow.ps1 -SkipRegistryFallback   # 只重启
#
# 每次运行都会往 %APPDATA%\weflow\repair-backups\repair-history.log 追加证据
# （锚点指纹、开机时间、微信启动时间、两段修复各自的结果），方便下次看时间规律。

param(
    [int]$HealthTimeoutSeconds = 60,
    [switch]$SkipRegistryFallback,
    [switch]$Quiet
)

$ErrorActionPreference = 'Continue'

function Say($msg, $color = 'Gray') { if (-not $Quiet) { Write-Host $msg -ForegroundColor $color } }

$exe  = Join-Path $env:LOCALAPPDATA 'Programs\WeFlow\WeFlow.exe'
if (-not (Test-Path $exe)) { $exe = 'C:\Program Files\WeFlow\WeFlow.exe' }
$log  = Join-Path $env:APPDATA 'weflow\logs\wcdb.log'
$key  = 'HKCU\Software\WeFlow'
$base = 'http://127.0.0.1:5031'
$bakDir = Join-Path $env:APPDATA 'weflow\repair-backups'
New-Item -ItemType Directory -Force -Path $bakDir | Out-Null
$history = Join-Path $bakDir 'repair-history.log'

function Write-History($text) {
    Add-Content -Path $history -Value ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $text) -Encoding UTF8
}

function Get-Health {
    try { return ((Invoke-RestMethod -Uri "$base/api/v1/health" -TimeoutSec 5).status -eq 'ok') }
    catch { return $false }
}

function Wait-Health([int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    $i = 0
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        $i++
        if (Get-Health) { return ($i * 3) }
    }
    return 0
}

function Get-AnchorFingerprint {
    try {
        $k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\WeFlow\Runtime')
        if (-not $k) { return '（Runtime 键不存在）' }
        $name = $k.GetValueNames() | Select-Object -First 1
        if (-not $name) { return '（Runtime 下没有值）' }
        $blob = [byte[]]$k.GetValue($name)
        $sha = [System.Security.Cryptography.SHA256]::Create().ComputeHash($blob)
        $hex = ($sha | ForEach-Object { $_.ToString('x2') }) -join ''
        return ("{0} {1}字节 sha256={2}" -f $name, $blob.Length, $hex.Substring(0, 16))
    } catch { return ("（读取失败: {0}）" -f $_.Exception.Message) }
}

function Stop-WeFlowGracefully([int]$graceSeconds = 8) {
    $procs = @(Get-Process -Name WeFlow -ErrorAction SilentlyContinue)
    if (-not $procs) { return 0 }
    Say ("   关闭 WeFlow（{0} 个进程，先发 WM_CLOSE）…" -f $procs.Count)
    foreach ($p in $procs) { if ($p.MainWindowHandle -ne 0) { $p.CloseMainWindow() | Out-Null } }
    $deadline = (Get-Date).AddSeconds($graceSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        if (-not (Get-Process -Name WeFlow -ErrorAction SilentlyContinue)) { return 0 }
    }
    $left = @(Get-Process -Name WeFlow -ErrorAction SilentlyContinue)
    if ($left) {
        Say ("   {0} 个进程没退出，强制结束" -f $left.Count) 'Yellow'
        $left | Stop-Process -Force
        Start-Sleep -Seconds 2
    }
    return 1
}

function Start-WeFlow {
    if (-not (Test-Path $exe)) { throw "找不到 WeFlow.exe（试过 $env:LOCALAPPDATA\Programs\WeFlow 和 C:\Program Files\WeFlow）" }
    Start-Process -FilePath $exe -WorkingDirectory (Join-Path $env:APPDATA 'weflow')
}

function Get-LastMismatchLine {
    if (-not (Test-Path $log)) { return '（没有 wcdb.log）' }
    $hit = Get-Content $log -Tail 80 | Where-Object { $_ -match 'policy mismatch' } | Select-Object -Last 1
    if ($hit) { return $hit.Trim() } else { return '（最近日志里没有 -105）' }
}

Say "== WeFlow 修复（-105） ==" 'Cyan'
$anchorBefore = Get-AnchorFingerprint
$lastBad = Get-LastMismatchLine
Say ("锚点: " + $anchorBefore)
Say ("最近一条 -105: " + $lastBad)

if (Get-Health) {
    Say "✅ WeFlow 的 API 现在是好的（/api/v1/health = ok），不用修。" 'Green'
    Write-History ("跳过：调用时 API 已就绪；锚点=" + $anchorBefore)
    exit 0
}

$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
$wx = (Get-Process Weixin -ErrorAction SilentlyContinue | Sort-Object StartTime | Select-Object -First 1).StartTime
Write-History ("开始修复；锚点=" + $anchorBefore + "；开机=" + $boot + "；微信启动=" + $wx + "；最近 -105=" + $lastBad)

# ---- 第一段：只重启（2026-09-26 实测：这一步就够）----
Say "1) 第一段：优雅重启，不动注册表…" 'Cyan'
Stop-WeFlowGracefully | Out-Null
Start-WeFlow
$took = Wait-Health $HealthTimeoutSeconds
if ($took -gt 0) {
    Say ("✅ 重启即恢复（{0} 秒），注册表一个字没动" -f $took) 'Green'
    Write-History ("第一段成功：重启后 {0}s 恢复；重启后锚点=" -f $took)
    Write-History ("   重启后锚点=" + (Get-AnchorFingerprint))
    Say "下一步验证：在 wxbot 工程目录跑  ..\venv312\Scripts\python.exe -m wxbot.cli doctor"
    exit 0
}

Say ("⚠️ 第一段失败（{0} 秒没起来）" -f $HealthTimeoutSeconds) 'Yellow'
Write-History ("第一段失败：重启后 {0}s 仍未就绪" -f $HealthTimeoutSeconds)

if ($SkipRegistryFallback) {
    Say "（-SkipRegistryFallback：不做第二段）" 'Yellow'
    exit 2
}

# ---- 第二段：清"离线防回拨锚点"（先备份注册表）----
Say "2) 第二段：清防回拨锚点 + 重启（会先导出注册表备份）…" 'Cyan'
$bak = Join-Path $bakDir ("WeFlow-registry-{0}.reg" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
reg export $key $bak /y 2>&1 | Out-Null
Say "   注册表已备份: $bak"
Stop-WeFlowGracefully | Out-Null
reg delete "$key\Runtime" /f 2>&1 | Out-Null
Start-WeFlow
$took2 = Wait-Health 90
if ($took2 -gt 0) {
    Say ("✅ 清锚点后恢复（{0} 秒）" -f $took2) 'Green'
    Write-History ("第二段成功：清锚点后 {0}s 恢复（备份 {bak}）" -f $took2)
    exit 0
}

Say "⚠️ 两段都没修好（90 秒内 API 仍未就绪）" 'Red'
Write-History "两段都失败"
Say ("   看日志：{0}（正常启动会出现 native protection ok / open ok）" -f $log) 'Yellow'
Say "   可能它弹了对话框等你点，或需要重新选一次数据目录。" 'Yellow'
exit 1
