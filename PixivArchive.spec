# -*- mode: python ; coding: utf-8 -*-
# Pixiv Archive PyInstaller 打包配置（onefile / 无控制台 / 自动开浏览器）
from PyInstaller.utils.hooks import collect_all

def _collect(pkg):
    d, h, b = collect_all(pkg)
    return d, h

datas = [
    ("app/templates", "app/templates"),
    ("app/static", "app/static"),
]
hiddenimports = []

# 需要收集运行时数据 / 动态导入的包。
# 注意：浏览器分发版不使用 pywebview / pythonnet，故意不收集以缩小体积。
for pkg in ("pixivpy3", "PIL", "pydantic", "pystray"):
    d, h = _collect(pkg)
    datas += d
    hiddenimports += h

# collect_all 偶尔会把 datas（文件元组）误并进 hiddenimports，这里只保留纯模块名
hiddenimports = [x for x in hiddenimports if isinstance(x, str)]

a = Analysis(
    ["browser_entry.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# onefile：把二进制与数据全部并入单个 EXE，不再使用 COLLECT
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PixivArchive",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",
)