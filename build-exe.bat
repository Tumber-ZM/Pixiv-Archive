@echo off
rem Pixiv Archive - build Release\PixivArchive.exe (single file) from source.
rem One click: creates venv, installs runtime + build dependencies,
rem regenerates the icon, then runs PyInstaller.
setlocal
set "BASE=%~dp0"
set "PYTHON="

for %%P in (python py) do (
    if not defined PYTHON (
        %%P -c "import sys" >nul 2>&1
        if not errorlevel 1 set "PYTHON=%%P"
    )
)
if not defined PYTHON (
    echo [ERROR] Python was not found on this system.
    echo Install Python 3.x from https://www.python.org/downloads/ and re-run this script.
    pause
    exit /b 1
)

if not exist "%BASE%venv\Scripts\python.exe" (
    echo [1/4] Creating virtual environment "venv" ...
    "%PYTHON%" -m venv "%BASE%venv"
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
) else (
    echo [1/4] venv already exists, skipping creation.
)

set "TRUSTED=--trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org"

echo [2/4] Installing runtime dependencies (requirements.txt) ...
"%BASE%venv\Scripts\python.exe" -m pip install -r "%BASE%requirements.txt" %TRUSTED%
if errorlevel 1 (
    echo [ERROR] Runtime dependency installation failed. Check your network and retry.
    pause
    exit /b 1
)

echo [3/4] Installing build dependencies (PyInstaller) ...
"%BASE%venv\Scripts\python.exe" -m pip install -r "%BASE%requirements-build.txt" %TRUSTED%
if errorlevel 1 (
    echo [ERROR] Build dependency installation failed. Check your network and retry.
    pause
    exit /b 1
)

echo [4/4] Regenerating icon and building the exe ...
"%BASE%venv\Scripts\python.exe" "%BASE%scripts\make_icon.py"
"%BASE%venv\Scripts\python.exe" -m PyInstaller --clean --noconfirm --distpath "%BASE%Release" --workpath "%BASE%build" "%BASE%PixivArchive.spec"
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    pause
    exit /b 1
)

echo.
echo Done: "%BASE%Release\PixivArchive.exe"
echo The exe is standalone and auto-opens the default browser on start.
pause
endlocal