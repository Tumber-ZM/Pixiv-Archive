"""静态结构检查：H2 安装完整性、L5/L6/L7/L8 前端结构、L9 端口单一来源、L10 打包脚本、L11 死代码。

只读源码与模板，不起服务；页面渲染层面的同类检查见 `test_pages.py`。
"""
import os

import harness as h

h.bootstrap()

TITLE = "静态结构：H2 / L5-L11"
NEEDS_SERVER = False

APP = os.path.join(h.ROOT, "app")
TEMPLATES = os.path.join(APP, "templates")
PARTIALS = os.path.join(TEMPLATES, "partials")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as handle:
        return handle.read()


def run(t):
    # ---------------- H2：安装链路完整性 ----------------
    t.check("H2 .env.template 存在", os.path.isfile(os.path.join(h.ROOT, ".env.template")))
    install = _read(h.ROOT, "install.bat")
    t.check("H2 install.bat 会复制模板", ".env.template" in install and ".env" in install)
    t.check("H2 模板缺失/复制失败时报错退出",
            "ERRORLEVEL" in install.upper() or "exit /b 1" in install.lower())

    # ---------------- L9：默认端口单一来源 ----------------
    config = _read(APP, "config.py")
    t.check("L9 app/config.py 定义 DEFAULT_PORT=6814", "DEFAULT_PORT = 6814" in config)
    offenders = []
    for name in os.listdir(APP):
        if not name.endswith(".py"):
            continue
        text = _read(APP, name)
        if "6814" in text and name != "config.py":
            offenders.append(name)
    run_py = _read(h.ROOT, "run.py")
    if "6814" in run_py:
        offenders.append("run.py")
    t.equal("L9 其它源文件不再硬编码 6814", offenders, [])
    t.check("L9 run.py 导入 DEFAULT_PORT", "from app.config import DEFAULT_PORT" in run_py)

    # ---------------- L10：打包脚本 ----------------
    build = _read(h.ROOT, "build.ps1")
    t.check("L10 缺 venv 时自动创建", "venv" in build and "pip install" in build)
    t.check("L10 同时尝试 python 与 py 启动器", "python" in build and "py" in build)
    t.check("L10 校验图标与产物存在",
            "icon.ico" in build and "PixivArchive.exe" in build and "Test-Path" in build)
    t.check("L10 打印 onefile 真实路径", "Release\\PixivArchive.exe" in build)
    t.check("L10 显式指定产物目录，不依赖 PyInstaller 默认值", "--distpath Release" in build)
    bat = _read(h.ROOT, "build-exe.bat")
    t.check("L10 build.ps1 与 build-exe.bat 产物目录一致（Release）",
            "--distpath" in bat and "Release" in bat and "dist\\PixivArchive" not in build)
    t.check("L10 build.ps1 顶部写明执行策略用法", "-ExecutionPolicy Bypass" in build)
    with open(os.path.join(h.ROOT, "build.ps1"), "rb") as handle:
        raw = handle.read()
    t.check("L10 build.ps1 含中文时必须是 UTF-8 with BOM（PS 5.1 才解析得了）",
            raw[:3] == b"\xef\xbb\xbf")
    t.check("L10 build.ps1 以换行结尾", raw.endswith(b"\n"))
    spec = _read(h.ROOT, "PixivArchive.spec")
    t.check("L10 spec 引用图标与入口脚本", "icon.ico" in spec and "browser_entry.py" in spec)
    t.check("L10 spec 打包模板与静态目录", "app/templates" in spec and "app/static" in spec)
    t.check("L10 spec 排除已移除的 pywebview 依赖",
            "excludes" in spec and '"webview"' in spec and "run.py" not in spec)

    # ---------------- L11：删掉无引用的图标 symbol ----------------
    icons = _read(TEMPLATES, "icons.html")
    t.check("L11 icons.html 已无 i-external", "i-external" not in icons)
    hits = []
    for base, _dirs, files in os.walk(APP):
        for name in files:
            path = os.path.join(base, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    if "i-external" in handle.read():
                        hits.append(os.path.relpath(path, h.ROOT))
            except (OSError, UnicodeDecodeError):
                continue
    t.equal("L11 模板里也不再有 #i-external 引用", hits, [])

    # ---------------- L6：模板去重结构 ----------------
    t.check("L6 base.html 存在", os.path.isfile(os.path.join(TEMPLATES, "base.html")))
    base = _read(TEMPLATES, "base.html")
    for block in ("title", "sidebar", "sidebar_js", "content"):
        t.check("L6 base.html 提供 %s 块" % block, "block %s" % block in base)
    for name in ("head_boot.html", "sidebar_simple.html", "sidebar_js.html", "common_js.html"):
        t.check("L6 partials/%s 存在" % name, os.path.isfile(os.path.join(PARTIALS, name)))
    for name in ("author.html", "tag.html", "artwork.html"):
        text = _read(TEMPLATES, name)
        t.check("L6 %s 继承 base.html" % name, text.lstrip().startswith("{% extends 'base.html' %}"))
        t.check("L6 %s 不再自带 escapeHtml 定义" % name, "function escapeHtml" not in text)
    common = _read(PARTIALS, "common_js.html")
    t.check("L6 公共脚本定义 escapeHtml", "function escapeHtml" in common)
    t.check("L6 公共脚本定义 escapeAttr", "function escapeAttr" in common)
    t.check("L6 公共脚本定义 fetchJson", "function fetchJson" in common)

    # ---------------- L5：转义 ----------------
    t.check("L5 标签页转义标签名", "escapeHtml(tagName)" in _read(TEMPLATES, "tag.html"))
    t.check("L5 作者页转义作者名", "escapeHtml(author.name)" in _read(TEMPLATES, "author.html"))
    t.check("L5 详情页 alt 使用 escapeAttr", "escapeAttr(" in _read(TEMPLATES, "artwork.html"))

    # ---------------- L7：失败提示 ----------------
    for name in ("author.html", "tag.html", "artwork.html"):
        text = _read(TEMPLATES, name)
        t.check("L7 %s 用 fetchJson" % name, "fetchJson(" in text)
    index = _read(TEMPLATES, "index.html")
    t.check("L7 图库加载失败会显示原因（不再停在“加载中”）",
            "加载失败：" in index and "e.message" in index)

    # ---------------- L8（常量部分）：分页大小 ----------------
    t.check("L8 index.html 定义 PAGE_SIZE", "const PAGE_SIZE" in index)
    t.equal("L8 两个分页 URL 都使用 PAGE_SIZE", index.count("per_page=${PAGE_SIZE}"), 2)
    t.check("L3 缩略图始终重编码为 JPEG（回归护栏）",
            "convert(\"RGB\")" in _read(APP, "thumbnails.py") and "JPEG" in _read(APP, "thumbnails.py"))

    # ---------------- M7：依赖声明 ----------------
    # 只统计真正声明依赖的正文行，注释里提到包名不算声明
    req_declared = " ".join(
        line.split("#", 1)[0].strip().lower()
        for line in _read(h.ROOT, "requirements.txt").splitlines()
        if line.split("#", 1)[0].strip()
    )
    for package in ("fastapi", "pydantic", "requests", "urllib3", "pixivpy3", "pillow", "jinja2", "python-dotenv"):
        t.check("M7 requirements 声明 %s" % package, package in req_declared)
    # 声明要和 run.py 的实际实现一致：自研服务器时期不该声明 hypercorn，换用之后必须声明
    uses_hypercorn = "hypercorn" in run_py
    t.check("M7 依赖声明与 run.py 实现一致（%s）" % ("hypercorn" if uses_hypercorn else "自研 ASGI 服务器"),
            ("hypercorn" in req_declared) == uses_hypercorn)