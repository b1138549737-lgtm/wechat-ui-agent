# wxbot 一键部署：建虚拟环境、装依赖、生成配置、跑自检
# 用法：powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
Write-Host "== wxbot 部署 ==" -ForegroundColor Cyan
Write-Host "工程目录: $root"

function Get-PyVersion([string]$exe) {
    # 返回 '3.12' 这种版本号；不是 Python 3.10+ 就返回 $null
    if (-not $exe -or -not (Test-Path $exe)) { return $null }
    try { $v = & $exe -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null }
    catch { return $null }
    if ("$v" -match '^(\d+)\.(\d+)$' -and [int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 10) {
        return "$v"
    }
    return $null
}

function Find-Python {
    # 注意：Get-ChildItem 的 -Filter **不支持字符类**（'cpython-3.1[0-9]*' 会一个都匹配不到），
    # 这里统一用 Where-Object 过滤。
    $cands = @()
    $uv = Join-Path $env:APPDATA 'uv\python'
    if (Test-Path $uv) {
        $cands += @(Get-ChildItem $uv -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^cpython-3\.1[0-3]' } |
            ForEach-Object { Join-Path $_.FullName 'python.exe' } |
            Where-Object { Test-Path $_ } |
            Sort-Object -Descending)
    }
    foreach ($name in @('python', 'py')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $cands += @($cmd.Source) }
    }
    $cands += @('C:\Python312\python.exe', 'C:\Python313\python.exe', 'C:\Python311\python.exe')
    $fallback = $null
    foreach ($c in $cands) {
        $v = Get-PyVersion $c
        if ($v) {
            if ([int]($v.Split('.')[1]) -le 13) { return @{ exe = $c; ver = $v } }
            if (-not $fallback) { $fallback = @{ exe = $c; ver = $v; warn = $true } }
        }
    }
    return $fallback        # 只有 3.14+ 时才用它（依赖可能没有对应轮子）
}

$found = Find-Python
if (-not $found) {
    Write-Host "❌ 找不到 Python 3.10+。建议装 uv（会自动带一个 Python 3.12）：" -ForegroundColor Red
    Write-Host "   powershell -c `"irm https://astral.sh/uv/install.ps1 | iex`""
    exit 1
}
$py = $found.exe
Write-Host "使用 Python $($found.ver) ($py)"
if ($found.warn) {
    Write-Host "⚠️ Python 3.14+ 缺少 maa-mcp/maafw 的预编译包，建议改用 3.12（uv python install 3.12）" -ForegroundColor Yellow
}

$venv = Join-Path $root '.venv'
if (-not (Test-Path $venv)) {
    Write-Host "1) 建虚拟环境 .venv …"
    & $py -m venv $venv
} else { Write-Host "1) 虚拟环境已存在，跳过" }
$vpy = Join-Path $venv 'Scripts\python.exe'

Write-Host "2) 安装依赖（pyyaml / mcp / maa-mcp / pillow）…"
& $vpy -m pip install --quiet --upgrade pip
# ★工单第 6③ 条：和 requirements.txt/pyproject.toml 同一套上限 —— 上游大版本别静默装上
$deps = @('pyyaml>=6.0,<7', 'mcp>=1.0,<2', 'maa-mcp>=1.2.3,<2', 'pillow>=10.0,<13')
# 默认源 + 清华镜像重试（国内直连 pypi.org 经常卡住，实测会一直停在第 2 步不动）
$indexes = @(
    @{ name = '默认源'; args = @() },
    @{ name = '清华镜像'; args = @('-i', 'https://pypi.tuna.tsinghua.edu.cn/simple') }
)
if ($env:PIP_INDEX_URL) { $indexes[0].args = @('-i', $env:PIP_INDEX_URL) }
$rc = 1
foreach ($idx in $indexes) {
    Write-Host "   使用 $($idx.name) …"
    & $vpy -m pip install -q --timeout 30 --retries 2 @($idx.args) @deps
    $rc = $LASTEXITCODE
    if ($rc -eq 0) { break }
    Write-Host "   ⚠️ $($idx.name) 失败（退出码 $rc），换下一个源" -ForegroundColor Yellow
}
if ($rc -ne 0) { Write-Host "⚠️ 依赖安装有失败（退出码 $rc），请检查网络/镜像设置" -ForegroundColor Yellow }
# ★工单第 6③ 条：装完回显实际版本 —— 出问题时一眼对得上"当时是什么版本"
if ($rc -eq 0) {
    Write-Host "   已装版本："
    & $vpy -m pip list --disable-pip-version-check 2>$null |
        Select-String -Pattern '^(pyyaml|mcp|maa-mcp|pillow)\s' |
        ForEach-Object { Write-Host "     $($_.Line)" }
}

$cfg = Join-Path $root 'config.yaml'
if (-not (Test-Path $cfg)) {
    Write-Host "3) 生成 config.yaml（从示例复制，记得填 WeFlow token / LLM）"
    Copy-Item (Join-Path $root 'config.example.yaml') $cfg
} else { Write-Host "3) config.yaml 已存在，保留不动" }

Write-Host "4) 自检（doctor）…"
Push-Location $root
& $vpy -m wxbot.cli doctor
$rc = $LASTEXITCODE
Pop-Location

Write-Host ""
if ($rc -eq 0) {
    Write-Host "✅ 部署完成。常用命令：" -ForegroundColor Green
    Write-Host "   .\.venv\Scripts\python.exe -m wxbot.cli run --seconds 600     # 常驻"
    Write-Host "   .\.venv\Scripts\python.exe -m wxbot.cli web --port 8765       # 控制面板"
} else {
    Write-Host "⚠️ 自检未全部通过，请按上面提示补齐（多数是微信窗口/WeFlow token/LLM 未就绪）" -ForegroundColor Yellow
}
