@echo off
cd /d "%~dp0"

REM 找 Python（先试 py launcher，再试 python）
py -3 --version >nul 2>&1
if not errorlevel 1 goto run_py3

python --version >nul 2>&1
if not errorlevel 1 goto run_python

echo.
echo ==========================================
echo   未找到 Python 3.10+
echo ==========================================
echo.
echo   请安装: winget install Python.Python.3.12
echo   或 https://www.python.org/downloads/
echo   安装时勾选 "Add Python to PATH"
echo.
pause
exit /b 1

:run_py3
py -3 install.py %*
goto finish

:run_python
python install.py %*
goto finish

:finish
if errorlevel 1 pause
