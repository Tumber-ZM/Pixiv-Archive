@echo off
rem Pixiv Archive - one-click setup for the distributed source version.
rem Creates a local venv and installs all dependencies (requirements.txt),
rem then creates .env from .env.template if missing.
setlocal
set "BASE=%~dp0"
set "PYTHON="

rem Bootstrap python: any Python 3.x on PATH is fine for creating the venv.
for %%P in (python py) do (
    if not defined PYTHON (
        %%P -c "import sys" >nul 2>&1
        if not errorlevel 1 set "PYTHON=%%P"
    )
)

if not defined PYTHON (
    echo [ERROR] Python was not found on this system.
    echo Install Python 3.x from https://www.python.org/downloads/ and re-run this script.
    echo Tip: during installation, check "Add python.exe to PATH".
    pause
    exit /b 1
)

if not exist "%BASE%venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment "venv" ...
    "%PYTHON%" -m venv "%BASE%venv"
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
) else (
    echo [1/3] venv already exists, skipping creation.
)

echo [2/3] Installing dependencies from requirements.txt ...
"%BASE%venv\Scripts\python.exe" -m pip install --upgrade pip --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org
"%BASE%venv\Scripts\python.exe" -m pip install -r "%BASE%requirements.txt" --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org
if errorlevel 1 (
    echo [ERROR] Dependency installation failed. Check your network and retry.
    pause
    exit /b 1
)

if not exist "%BASE%.env" (
    copy /y "%BASE%.env.template" "%BASE%.env" >nul
    echo [3/3] Created .env from .env.template.
    echo       Edit .env to set PIXIV_REFRESH_TOKEN and IMAGE_SOURCE_DIR.
) else (
    echo [3/3] .env already exists, left unchanged.
)

echo.
echo Setup complete. How to start:
echo   Start.bat     - start the server (open in browser afterwards)
echo   Start-LAN.bat - start the server in LAN mode
echo   launcher.bat  - desktop window mode
echo.
pause
endlocal