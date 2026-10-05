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

echo [2/3] Installing dependencies ...
rem pip 默认校验 PyPI 的 TLS 证书；这里不再传 --trusted-host（那会关闭校验、
rem 让中间人有机可乘）。如遇自签证书的中间盒，请改用系统信任链或临时设置
rem PIP_CERT，而不是长期关闭校验。
"%BASE%venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] Failed to upgrade pip. Check your network and retry.
    pause
    exit /b 1
)

rem 优先用 requirements.lock 做带哈希校验的可复现安装。该锁覆盖 Windows +
rem CPython 3.10-3.13；若本机解释器不在其内，pip 会拒绝，此时回退到
rem requirements.txt，保证安装仍能成功。
set "LOCK_OK="
if exist "%BASE%requirements.lock" (
    "%BASE%venv\Scripts\python.exe" -m pip install -r "%BASE%requirements.lock" --require-hashes
    if not errorlevel 1 set "LOCK_OK=1"
)
if not defined LOCK_OK (
    echo [WARN] Hash-verified install unavailable for this interpreter;
    echo        falling back to requirements.txt without hash verification.
    "%BASE%venv\Scripts\python.exe" -m pip install -r "%BASE%requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed. Check your network and retry.
        pause
        exit /b 1
    )
)

rem NOTE: this block deliberately uses goto + labels instead of nested if blocks.
rem Measured: exit /b 1 inside a doubly nested if block makes cmd return 0,
rem so a genuine failure would be reported as success.
if exist "%BASE%.env" goto :env_exists

if not exist "%BASE%.env.template" goto :env_no_template

copy /y "%BASE%.env.template" "%BASE%.env" >nul
if errorlevel 1 goto :env_copy_failed
if not exist "%BASE%.env" goto :env_copy_failed

echo [3/3] Created .env from .env.template.
echo       Edit .env to set PIXIV_REFRESH_TOKEN and IMAGE_SOURCE_DIR.
goto :env_done

:env_exists
echo [3/3] .env already exists, left unchanged.
goto :env_done

:env_no_template
echo [ERROR] .env.template is missing, cannot create .env.
echo         Re-download the project, or create .env manually.
pause
exit /b 1

:env_copy_failed
echo [ERROR] Failed to create .env from .env.template.
pause
exit /b 1

:env_done

echo.
echo Setup complete. How to start:
echo   Start.bat     - start the local server (single instance, 127.0.0.1 only)
echo.
pause
endlocal