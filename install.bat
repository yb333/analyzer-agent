@echo off
REM DWS Skills 安装器 (Windows)
REM 双击即可执行，核心逻辑在 install.py 中

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

REM 找 Python（Windows 没有 python3）
py -3 --version >nul 2>&1 && (
    py -3 "%SCRIPT_DIR%\install.py" %*
    goto :end
)

python --version >nul 2>&1 && (
    python "%SCRIPT_DIR%\install.py" %*
    goto :end
)

echo ==========================================
echo   未找到 Python 3.10+
echo ==========================================
echo.
echo   请安装 Python 3.10+:
echo     winget install Python.Python.3.12
echo   或 https://www.python.org/downloads/
echo.
echo   安装时勾选 "Add Python to PATH"
echo.
pause
exit /b 1

:end
if errorlevel 1 pause
