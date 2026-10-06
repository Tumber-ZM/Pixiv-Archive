"""服务端渲染结果检查：L5/L6/L7/L8 的结构护栏 + 页面可访问性。

L6 的核心断言是「共享片段里的每一行静态内容都出现在渲染结果里」：这样模板一旦
被拆错、少 include 一段，测试立刻报警，而不需要把 HTML 硬编码成期望字符串。
"""
import os

import harness as h

TITLE = "页面渲染：L6 片段完整性 / L5 / L7 / L8"
NEEDS_SERVER = True

TEMPLATES = os.path.join(h.ROOT, "app", "templates")
PARTIALS = os.path.join(TEMPLATES, "partials")
PAGES = ("/", "/tag/R-18", "/author/1", "/artwork/148494017")


def static_lines(path):
    """取片段里不含 Jinja 标记、长度足够的静态行，用来核对渲染结果。"""
    lines = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if len(line) > 12 and not any(marker in line for marker in ("{%", "{{", "{#", "#}")):
                lines.append(line)
    return lines


def run(t, server):
    base = server.base
    pages = {}
    for path in PAGES:
        status, text = h.get(base, path)
        t.equal("页面 GET %s 返回 200" % path, status, 200)
        pages[path] = text

    # ---------- L6：共享片段完整落地 ----------
    head_lines = static_lines(os.path.join(PARTIALS, "head_boot.html"))
    t.check("head_boot 片段有可核对的静态行", len(head_lines) >= 2)
    for path in PAGES:
        missing = [line for line in head_lines if line not in pages[path]]
        t.check("L6 %s 含 head_boot 的全部静态行" % path, not missing,
                "缺失 %d 行，例如 %r" % (len(missing), missing[:1]))

    for partial, targets in (("sidebar_simple.html", ("/tag/R-18", "/author/1", "/artwork/148494017")),
                             ("sidebar_js.html", ("/tag/R-18", "/author/1", "/artwork/148494017")),
                             ("common_js.html", ("/tag/R-18", "/author/1", "/artwork/148494017"))):
        lines = static_lines(os.path.join(PARTIALS, partial))
        t.check("%s 有可核对的静态行" % partial, len(lines) >= 2)
        for path in targets:
            missing = [line for line in lines if line not in pages[path]]
            t.check("L6 %s 含 %s 的全部静态行" % (path, partial), not missing,
                    "缺失 %d 行，例如 %r" % (len(missing), missing[:1]))

    # ---------- 公共脚本只加载一次 ----------
    for path in ("/tag/R-18", "/author/1", "/artwork/148494017"):
        t.equal("L6 %s 只定义一份 escapeHtml" % path, pages[path].count("function escapeHtml"), 1)
        t.equal("L6 %s 只定义一份 fetchJson" % path, pages[path].count("function fetchJson"), 1)
    t.equal("L6 图库页也只定义一份 escapeHtml", pages["/"].count("function escapeHtml"), 1)

    # ---------- 侧栏选中态（重构时最容易丢的东西）----------
    t.check("侧栏渲染出选中态", 'class="side-nav-item active"' in pages["/artwork/148494017"])
    t.equal("非作品页没有误标选中态", pages["/author/1"].count('class="side-nav-item active"'), 0)

    # ---------- L11 ----------
    for path in PAGES:
        t.check("L11 %s 不再引用已删除的 i-external" % path, "i-external" not in pages[path])

    # ---------- L5 / L7 / L8 在成品页面上确实生效 ----------
    t.check("L5 标签页按标签名转义", "escapeHtml(tagName)" in pages["/tag/R-18"])
    t.check("L5 作者页按作者名转义", "escapeHtml(author.name)" in pages["/author/1"])
    t.check("L7 标签页分页失败不再卡住", "showLoadError(" in pages["/tag/R-18"])
    t.check("L7 作者页分页失败不再卡住", "showLoadError(" in pages["/author/1"])
    t.check("L7 详情页请求失败有提示", "fetchJson(" in pages["/artwork/148494017"])
    t.check("L8 图库使用共享分页常量", "per_page=${PAGE_SIZE}" in pages["/"])
    t.equal("L8 图库两处分页都走常量", pages["/"].count("per_page=${PAGE_SIZE}"), 2)
    t.check("L2 图库提示删除失败项", "files_failed" in pages["/"])
    t.check("作者页只保留两个必要的请求",
            pages["/author/1"].count("fetchJson(`") + pages["/author/1"].count("fetchJson('") == 2)

    # ---------- F-008：标签名含反斜杠时不打断脚本字面量 ----------
    # 用 "{{ tag_name }}" 的旧写法：反斜杠不被 Jinja 转义，会吃掉结尾引号导致
    # 整个 <script> 语法错误（标签页白屏）。改 | tojson 后反斜杠成对出现。
    status, tag_text = h.get(base, "/tag/a%5Cb")
    t.equal("F-008 标签名含反斜杠仍返回 200", status, 200)
    idx = tag_text.find("decodeURIComponent(")
    t.check("F-008 tag_name 经 tojson 注入（反斜杠被 JSON 转义，字面量不断裂）",
            'decodeURIComponent("a\\\\b")' in tag_text,
            "实际=%r" % tag_text[idx:idx + 40])