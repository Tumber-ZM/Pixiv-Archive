# Pixiv Archive 一键打包脚本（Windows / PowerShell）
# 用法：powershell -ExecutionPolicy Bypass -File build.ps1
# （未签名脚本在默认执行策略下无法直接运行；产物与 build-exe.bat、README 一致：Release\PixivArchive.exe）
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# 1) 确保 venv 存在。与 install.bat 保持一致：python 不在 PATH 时退回 py 启动器。
$venvPy = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    $bootstrap = $null
    foreach ($cand in @("python", "py")) {
        if (Get-Command $cand -ErrorAction SilentlyContinue) { $bootstrap = $cand; break }
    }
    if (-not $bootstrap) {
        throw "找不到 Python（python / py 都不在 PATH）。请先安装 Python 3.x 并勾选 Add python.exe to PATH，或先运行 install.bat。"
    }
    Write-Host "==> 创建虚拟环境 venv（$bootstrap -m venv venv）..."
    & $bootstrap -m venv (Join-Path $root "venv")
    if ($LASTEXITCODE -ne 0) { throw "创建 venv 失败" }
    if (-not (Test-Path $venvPy)) { throw "创建 venv 后仍找不到 $venvPy" }
}
else {
    Write-Host "==> venv 已存在，跳过创建"
}

Write-Host "==> 安装依赖..."
& $venvPy -m pip install -r requirements.txt -r requirements-build.txt `
    --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org
if ($LASTEXITCODE -ne 0) { throw "pip install 失败" }

Write-Host "==> 生成图标..."
& $venvPy scripts\make_icon.py
if ($LASTEXITCODE -ne 0) { throw "图标生成失败" }
$iconPath = Join-Path $root "assets\icon.ico"
if (-not (Test-Path $iconPath)) { throw "图标生成失败：找不到 $iconPath" }

Write-Host "==> PyInstaller 打包..."
# 产物目录必须与 build-exe.bat / README 保持一致（Release），不要用 PyInstaller 的默认 dist
& $venvPy -m PyInstaller --clean --noconfirm --distpath Release --workpath build PixivArchive.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }

# PixivArchive.spec 是 onefile 配置：产物是 Release 下的单个 exe，
# 不是 Release\PixivArchive\ 目录（此前脚本打印的正是那个并不存在的子目录路径）。
$artifact = Join-Path $root "Release\PixivArchive.exe"
if (-not (Test-Path $artifact)) { throw "打包结束但找不到产物：$artifact" }

Write-Host ""
Write-Host "打包完成: Release\PixivArchive.exe"
Write-Host "提示: 把 archive.db / metadata / thumbnails / .env 复制到 exe 同目录即可直接使用已有数据。"
