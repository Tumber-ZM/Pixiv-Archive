@echo off
rem Pixiv Archive - start local server (browser mode).
rem Uses the project venv if present, otherwise falls back to python/py on PATH
rem (only if those already have the project dependencies installed).
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

"%PYTHON%" "%BASE%run.py" %*

if errorlevel 1 (
    echo.
    echo [ERROR] Startup failed. Likely causes:
    echo   - dependencies missing  -^> run install.bat
    echo   - configure .env (PIXIV_REFRESH_TOKEN / IMAGE_SOURCE_DIR)
    pause
)
endlocal