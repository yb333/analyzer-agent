@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1

REM ============================================================
REM sync_to_internal.bat — 一键同步外网代码到内网 git 仓库
REM
REM 用法（在内网 Windows 电脑上运行）：
REM   sync_to_internal.bat                          REM 用默认配置
REM   sync_to_internal.bat D:\path\to\repo          REM 指定内网仓库路径
REM   sync_to_internal.bat --config D:\path\to\repo REM 设置默认路径（记住）
REM
REM 自动完成：
REM   1. git clone --depth 1 外网GitHub最新代码
REM   2. 同步成品文件到内网仓库（排除开发文件）
REM   3. git add + commit + push 到内网远端
REM ============================================================

REM ── 配置 ──
set "EXTERNAL_REPO=https://github.com/yb333/analyzer-agent.git"
set "CONFIG_FILE=%USERPROFILE%\.analyzer-agent-sync.conf"

REM 读取配置文件（记住上次设置的内网仓库路径）
if exist "%CONFIG_FILE%" (
    set /p INTERNAL_REPO=<"%CONFIG_FILE%"
)

REM 命令行参数处理
if "%~1"=="--config" (
    if not "%~2"=="" (
        echo %~2>"%CONFIG_FILE%"
        echo [OK] 已保存内网仓库路径: %~2
        echo 以后直接运行 sync_to_internal.bat 即可，不用再指定路径。
        goto :eof
    )
)

REM 覆盖默认路径
if not "%~1"=="" (
    set "INTERNAL_REPO=%~1"
)

REM 检查内网仓库路径
if "!INTERNAL_REPO!"=="" (
    echo [ERROR] 未指定内网 git 仓库路径
    echo.
    echo 用法：
    echo   首次使用先配置：sync_to_internal.bat --config D:\path\to\internal\repo
    echo   后续直接运行：sync_to_internal.bat
    goto :eof
)

if not exist "!INTERNAL_REPO!\.git" (
    echo [ERROR] 不是 git 仓库: !INTERNAL_REPO!
    echo 请确认路径正确，且该目录已 git init 或 clone 自内网远端
    goto :eof
)

echo ═══════════════════════════════════════════════
echo   同步外网代码 → 内网 git 仓库
echo ═══════════════════════════════════════════════
echo 外网仓库: %EXTERNAL_REPO%
echo 内网仓库: !INTERNAL_REPO!
echo.

REM ── Step 1: 从外网拉最新代码 ──
set "TEMP_DIR=%TEMP%\analyzer-agent-sync-%RANDOM%"
echo [Step 1] 拉取外网最新代码...
git clone --depth 1 %EXTERNAL_REPO% "%TEMP_DIR%" 2>&1
if not exist "%TEMP_DIR%\.git" (
    echo [ERROR] clone 失败，请检查网络或仓库地址
    goto :cleanup
)
echo.

REM ── Step 2: 同步外网代码到内网仓（以外网为准）──
REM 策略：robocopy /MIR 镜像同步（源覆盖目标，源没有的删除），保留 .git
echo [Step 2] 同步到内网仓库（以外网为准）...

REM 用 robocopy /MIR 镜像同步（排除 .git 和开发文件）
REM /MIR = /E + /PURGE（复制+删除目标多余的），以外网为准
robocopy "%TEMP_DIR%" "!INTERNAL_REPO!" /MIR /NFL /NDL /NP /NJH /NJS ^
    /XD tests docs release __pycache__ .pytest_cache .git telemetry-server ^
    /XF architecture.md sync_to_internal.sh sync_to_internal.bat start_telemetry.sh start_telemetry.bat stop_telemetry.bat sample_rule.yml .gitignore .DS_Store 2>nul

REM robocopy 返回码 0-7 都是正常的（0=无变更 1=有复制）
if !errorlevel! GTR 7 (
    echo [ERROR] robocopy 失败
    goto :cleanup
)
echo   + 同步完成（以外网为准）

REM 清理垃圾文件（robocopy 可能带过来的）
for /d /r "!INTERNAL_REPO!" %%D in (__pycache__) do (
    if exist "%%D" rmdir /s /q "%%D" 2>nul
)
del /s /q "!INTERNAL_REPO!\*.pyc" 2>nul
del /s /q "!INTERNAL_REPO!\.DS_Store" 2>nul

echo.

REM ── Step 3: git commit + push ──
echo [Step 3] 提交到内网 git...
pushd "!INTERNAL_REPO!"

REM 强制以外网内容为准（丢弃内网仓的本地修改）
git checkout -- . 2>nul

REM 先 pull 内网仓可能有的远端变更（rebase）
git pull --rebase 2>&1

git add -A

REM 检查有没有变更
git diff --cached --quiet
if !errorlevel! equ 0 (
    echo   无变更，内容已是最新。
) else (
    REM 提取外网最新 commit 的完整 message（用原始提交信息，不加"同步"前缀）
    pushd "%TEMP_DIR%"
    REM 取最新 commit 的 subject（第一行 message）
    for /f "delims=" %%H in ('git log -1 --format^="%%s"') do set "COMMIT_MSG=%%H"
    REM 取最新 commit 的完整 hash 短缩
    for /f "delims=" %%H in ('git rev-parse --short HEAD') do set "COMMIT_HASH=%%H"
    REM 取 body（如果有多行 message）
    for /f "delims=" %%H in ('git log -1 --format^="%%b"') do set "COMMIT_BODY=%%H"
    popd
    echo   提交信息: !COMMIT_MSG!
    echo.
    REM 用外网的原始 commit message 做提交信息（带 hash 标注来源）
    if "!COMMIT_BODY!"=="" (
        git commit -m "!COMMIT_MSG! (!COMMIT_HASH!)" 2>&1
    ) else (
        git commit -m "!COMMIT_MSG! (!COMMIT_HASH!)" -m "!COMMIT_BODY!" 2>&1
    )
    echo.
    echo [Step 4] 推送到内网远端...
    REM 先 pull 内网仓可能有的变更（rebase 避免无谓 merge commit）
    git pull --rebase 2>&1
    git push 2>&1
    echo.
    echo ═══════════════════════════════════════════════
    echo   同步完成
    echo   !COMMIT_HASH! !COMMIT_MSG!
    echo ═══════════════════════════════════════════════
)
popd

:cleanup
if exist "%TEMP_DIR%" rmdir /s /q "%TEMP_DIR%" 2>nul
echo.
echo 按任意键关闭...
pause >nul
