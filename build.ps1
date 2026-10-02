# Pixiv Archive 一键打包脚本（Windows / PowerShell）
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "==> 安装依赖..."
venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt `
    --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org
if ($LASTEXITCODE -ne 0) { throw "pip install 失败" }

Write-Host "==> 生成图标..."
venv\Scripts\python.exe scripts\make_icon.py
if ($LASTEXITCODE -ne 0) { throw "图标生成失败" }

Write-Host "==> PyInstaller 打包..."
venv\Scripts\python.exe -m PyInstaller --clean --noconfirm PixivArchive.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }

Write-Host ""
Write-Host "打包完成: dist\PixivArchive\PixivArchive.exe"
Write-Host "提示: 将你现有的 archive.db / metadata / thumbnails / .env 复制到 dist\PixivArchive 即可直接使用已有数据。"
