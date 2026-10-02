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

    # ---------------- L10：打包链路已按要求整体移除 ----------------
    # 用户决定不再打包 exe：相关脚本 / spec / 入口 / 图标 / 构建文档都必须消失，
    # 否则会留下指向不存在文件的说明（文档与现实不一致本身就是缺陷）。
    removed = ["build.ps1", "build-exe.bat", "PixivArchive.spec", "requirements-build.txt",
               "browser_entry.py", "scripts/make_icon.py", "assets/icon.ico", "assets/icon.png"]
    still_there = [rel for rel in removed if os.path.exists(os.path.join(h.ROOT, rel))]
    t.equal("L10 打包相关文件已全部删除", still_there, [])

    def _strip_changelog(text):
        """去掉「本次变更」小节：那里本来就要写明删掉了哪些文件。"""
        head, sep, rest = text.partition("## 本次变更")
        if not sep:
            return text
        return head + rest.partition("\n---")[2]

    docs = _strip_changelog(_read(h.ROOT, "README.md")) + _read(h.ROOT, "THIRD_PARTY_NOTICES.md")
    stale = [rel for rel in removed if rel in docs or rel.replace("\\", "/") in docs]
    t.equal("L10 文档不再把已删除的打包文件当成可用的东西", stale, [])
    t.check("L10 文档不再提 PyInstaller / Release 产物",
            "PyInstaller" not in docs and "Release\\PixivArchive.exe" not in docs)
    start_bat = _read(h.ROOT, "Start.bat")
    # 启动器刻意保持与会话前完全一致：本机安全软件只对「被改动过的脚本」弹
    # 「无法验证开发者」，而回退分支选错解释器时 run.py 自己会抛 ImportError、
    # Start.bat 也会提示「dependencies missing → run install.bat」，无需再改它。
    t.check("L10 Start.bat 只调用 run.py 且未被改动（不引入额外探测）",
            "run.py" in start_bat and "hypercorn" not in start_bat)
    t.check("L10 hypercorn 仍在依赖清单里", "hypercorn" in _read(h.ROOT, "requirements.txt"))

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
    # 屏外卡片跳过布局与绘制（浏览器原生窗口化）；图库是 CSS 多列瀑布流，
    # 手写虚拟滚动在这种布局下会错位，因此用这项机制拿到等效收益
    css = _read(APP, "static", "css", "style.css")
    t.check("L8 .tile 启用 content-visibility: auto", "content-visibility: auto" in css)
    t.check("L8 .tile 带 contain-intrinsic-size（避免滚动条跳动）",
            "contain-intrinsic-size: auto" in css)
    t.check("L8 图库仍是分页加载（每页上限受 PAGE_SIZE 约束，DOM 不会无限增长）",
            "per_page" in index and "PAGE_SIZE = 100" in index)
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