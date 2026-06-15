# DWS Skills 安装器 — Windows (PowerShell)
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File install.ps1              # 安装所有 skill
#   powershell -ExecutionPolicy Bypass -File install.ps1 dws-pipeline-analyzer  # 只装指定的
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Local       # 装到当前项目 .opencode/

param(
    [switch]$Local,
    [Parameter(Position=0)]
    [string]$SkillFilter = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Mode = if ($Local) { "local" } else { "global" }

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  DWS Skills 安装器 (Windows)" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

# ── 1. 扫描可用 skill ──
$AllSkills = @()
Get-ChildItem -Path $ScriptDir -Directory | ForEach-Object {
    if (Test-Path (Join-Path $_.FullName "SKILL.md")) {
        $AllSkills += $_.Name
    }
}

if ($AllSkills.Count -eq 0) {
    Write-Host "未找到任何 skill（需要 SKILL.md 文件）" -ForegroundColor Red
    exit 1
}

# 过滤
if ($SkillFilter) {
    $Skills = $AllSkills | Where-Object { $_ -like "*$SkillFilter*" }
    if ($Skills.Count -eq 0) {
        Write-Host "未找到匹配 '$SkillFilter' 的 skill" -ForegroundColor Red
        Write-Host "可用: $($AllSkills -join ', ')"
        exit 1
    }
} else {
    $Skills = $AllSkills
}

Write-Host "将安装 $($Skills.Count) 个 skill: $($Skills -join ', ')"
if ($Mode -eq "local") { Write-Host "模式: 项目级 (.opencode\)" } else { Write-Host "模式: 全局 (~\.config\opencode\)" }
Write-Host ""

# ── 2. 检测 Python（Windows 没有 python3）──
Write-Host "[1/4] 检测 Python..."
$Python = $null
$Candidates = @("python", "py -3", "python3")
foreach ($cmd in $Candidates) {
    $parts = $cmd -split " "
    $exe = $parts[0]
    $pyArgs = if ($parts.Count -gt 1) { $parts[1..99] } else { @() }
    try {
        & $exe @pyArgs -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $Python = $cmd; break }
    } catch { continue }
}

if (-not $Python) {
    Write-Host "  未找到 Python 3.10+" -ForegroundColor Red
    Write-Host "  请安装: winget install Python.Python.3.12 或 https://www.python.org/downloads/"
    Write-Host "  安装时勾选 'Add Python to PATH'"
    exit 1
}
$PyVersion = Invoke-Expression "$Python --version 2>&1"
Write-Host "  $Python ($PyVersion)" -ForegroundColor Green

# ── 3. venv + 依赖 ──
Write-Host ""
Write-Host "[2/4] 安装 Python 依赖..."
if ($Mode -eq "global") {
    $ConfigDir = Join-Path $env:USERPROFILE ".config\opencode"
    $VenvDir = Join-Path $ConfigDir "venv"
} else {
    $VenvDir = Join-Path $ScriptDir ".venv"
}
if (-not (Test-Path $VenvDir)) {
    Invoke-Expression "$Python -m venv `"$VenvDir`""
}
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"

# 汇总依赖
$AllReqs = @()
foreach ($s in $Skills) {
    $req = Join-Path $ScriptDir "$s\requirements.txt"
    if (Test-Path $req) {
        Get-Content $req | Where-Object { $_ -and $_ -notmatch '^#' } | ForEach-Object { $AllReqs += $_.Trim() }
    }
}
$UniqueReqs = $AllReqs | Sort-Object -Unique

if ($UniqueReqs) {
    & $VenvPy -m pip install --upgrade pip --quiet 2>$null
    & $VenvPy -m pip install $UniqueReqs --quiet
    Write-Host "  已安装: $($UniqueReqs -join ' ')" -ForegroundColor Green
}

# ── 4. 复制 skill + 命令 ──
Write-Host ""
Write-Host "[3/4] 安装 skill 文件..."
if ($Mode -eq "global") {
    $SkillsDir = Join-Path $ConfigDir "skills"
    $CommandsDir = Join-Path $ConfigDir "commands"
} else {
    $SkillsDir = ".opencode\skills"
    $CommandsDir = ".opencode\commands"
}
New-Item -ItemType Directory -Force -Path $SkillsDir | Out-Null
New-Item -ItemType Directory -Force -Path $CommandsDir | Out-Null

foreach ($s in $Skills) {
    $src = Join-Path $ScriptDir $s
    $dst = Join-Path $SkillsDir $s
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    # 复制（排除 __pycache__, .venv, .git）
    Get-ChildItem -Path $src -Recurse -Force |
        Where-Object { $_.FullName -notmatch '__pycache__|\.venv|\.git' } |
        ForEach-Object {
            $rel = $_.FullName.Substring($src.Length)
            $target = $dst + $rel
            if ($_.PSIsContainer) {
                New-Item -ItemType Directory -Force -Path $target | Out-Null
            } else {
                Copy-Item $_.FullName $target -Force
            }
        }
    Write-Host "  $s" -ForegroundColor Green
}

# 复制命令
$CmdSourceDir = Join-Path $ScriptDir "commands"
if (Test-Path $CmdSourceDir) {
    Get-ChildItem -Path $CmdSourceDir -Filter "*.md" | ForEach-Object {
        Copy-Item $_.FullName (Join-Path $CommandsDir $_.Name) -Force
        Write-Host "  命令: $($_.Name)" -ForegroundColor Green
    }
}

# ── 5. dws-run ──
Write-Host ""
Write-Host "[4/4] 检查 dws-run..."
$DwsRun = Join-Path $SkillsDir "dws-run.py"
if (Test-Path $DwsRun) {
    Write-Host "  dws-run 已就绪" -ForegroundColor Green
} else {
    Write-Host "  (无 dws-run.py，skill 仍可通过 python run.py 直接调用)" -ForegroundColor Yellow
}

# ── 完成 ──
Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  安装完成！" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Python 依赖: $($UniqueReqs -join ' ')"
Write-Host "安装位置: $SkillsDir"
Write-Host ""
Write-Host "在 opencode 中使用:"
Write-Host "  /analyze @execution_tasks.xlsx"
Write-Host ""
