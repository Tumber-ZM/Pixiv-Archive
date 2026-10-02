@echo off
rem Pixiv Archive - desktop window mode (pywebview).
rem Browser mode:  launcher.bat --browser
rem Uses the project venv if present, otherwise falls back to python/py on PATH.
setlocal
set "BASE=%~dp0"
set "PYTHON="

if exist "%BASE%venv\Scripts\python.exe" (
    set "PYTHON=%BASE%venv\Scripts\python.exe"
) else (
    for %%P in (python py) do (
        if not defined PYTHON (
            %%P -c "import fastapi, pixivpy3, dotenv, PIL" >nul 2>&1
            if not errorlevel 1 set "PYTHON=%%P"
        )
    )
)

if not defined PYTHON (
    echo [ERROR] No usable Python with the project dependencies was found.
    echo Run install.bat first to create the local venv and install requirements.
    pause
    exit /b 1
)

echo Desktop window mode. For browser mode: launcher.bat --browser
"%PYTHON%" "%BASE%launcher.py" %*

if errorlevel 1 (
    echo.
    echo [ERROR] Launch failed. Run install.bat if dependencies are missing.
    pause
)
endlocal