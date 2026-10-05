"""真实服务上的回归：冒烟、H1 坏请求、M4 随机分页、M8 只收 POST、L2 删除响应形状、
单实例锁端到端（含第二个进程拒绝启动）。

只发只读请求；写接口仅使用「空列表删除」和「必然校验失败」的请求体，不改动归档数据。
"""
import json
import os
import subprocess
import sys
import tempfile

import harness as h

h.bootstrap()
import run as run_mod  # noqa: E402

TITLE = "HTTP 冒烟 / H1 / M4 / M8 / L2 / 锁端到端"
NEEDS_SERVER = True

SMOKE_PATHS = (
    "/", "/tag/R-18", "/author/1", "/artwork/148494017",
    "/api/tags", "/api/authors", "/api/settings",
    "/api/artworks?page=1&per_page=3", "/api/search?q=",
    "/thumbnails/148494017.jpg",
)


def run(t, server):
    base = server.base

    # ---------------- 未处理异常的兜底（H1 的连接层部分）----------------
    from app import main as app_main

    chain = []
    node = app_main.app.build_middleware_stack()
    while node is not None:
        chain.append(type(node).__name__)
        node = getattr(node, "app", None)
    t.check("H1 未处理异常由 Starlette 的 500 中间件兜底（不会静默断连）",
            "ServerErrorMiddleware" in chain, "中间件链=%s" % chain)

    # ---------------- 冒烟 ----------------
    for path in SMOKE_PATHS:
        status, _text = h.get(base, path)
        t.equal("冒烟 GET %s" % path, status, 200)
    status, _text = h.get(base, "/definitely-not-a-route")
    t.equal("未知路径返回 404", status, 404)
    status, text = h.get(base, "/api/artworks?page=1&per_page=1")
    data = h.load_json(text) or {}
    t.check("列表接口返回 artwork 数组与总数",
            isinstance(data.get("artworks"), list) and isinstance(data.get("total"), int))
    artwork_id = (data.get("artworks") or [{}])[0].get("id")
    if artwork_id:
        status, _text = h.get(base, "/api/artworks/%d" % artwork_id)
        t.equal("按 id 取单件作品", status, 200)
    else:
        t.skip("库里没有作品，跳过单件作品接口")

    # ---------------- H1：坏请求不能打掉服务 ----------------
    settings_before = h.get(base, "/api/settings")[1]
    status, text, _headers = h.post_json(base, "/api/settings", "{ this is not json")
    t.equal("H1 设置接口收到坏 JSON 返回 400", status, 400)
    t.check("H1 400 响应体里有可读原因", bool(text.strip()) and "Internal Server Error" not in text)
    status, _text, _headers = h.post_json(base, "/api/artworks/batch-delete", "{{{")
    t.equal("H1 批量删除收到坏 JSON 返回 400", status, 400)
    status, _text, _headers = h.post_json(base, "/api/artworks/batch-delete",
                                         json.dumps({"artwork_ids": "not-a-list"}))
    t.equal("H1 字段类型错误返回 422", status, 422)
    t.equal("H1 之后服务仍然可用", h.get(base, "/api/tags")[0], 200)
    t.check("H1 坏请求没有改动设置", h.get(base, "/api/settings")[1] == settings_before)

    # ---------------- M4：随机排序翻页不重不漏 ----------------
    t.check("M4 随机排序不再使用 ORDER BY RANDOM()",
            "RANDOM()" not in app_random_sql())
    first = h.get(base, "/api/artworks?page=1&per_page=20&sort=random&seed=12345")
    again = h.get(base, "/api/artworks?page=1&per_page=20&sort=random&seed=12345")
    ids_first = [row["pixiv_id"] for row in (h.load_json(first[1]) or {}).get("artworks", [])]
    ids_again = [row["pixiv_id"] for row in (h.load_json(again[1]) or {}).get("artworks", [])]
    t.check("M4 同一 seed 两次请求顺序完全一致", ids_first == ids_again and len(ids_first) == 20)

    pages = []
    for page in (1, 2, 3):
        status, text = h.get(base, "/api/artworks?page=%d&per_page=20&sort=random&seed=12345" % page)
        t.equal("M4 第 %d 页返回 200" % page, status, 200)
        pages.append([row["pixiv_id"] for row in (h.load_json(text) or {}).get("artworks", [])])
    all_ids = [x for page_ids in pages for x in page_ids]
    t.check("M4 三页之间没有重复项", len(all_ids) == len(set(all_ids)), "共 %d 项" % len(all_ids))
    t.check("M4 第 1 页不受翻页影响", pages[0] == ids_first)

    other_seed = h.get(base, "/api/artworks?page=1&per_page=20&sort=random&seed=999")
    ids_other = [row["pixiv_id"] for row in (h.load_json(other_seed[1]) or {}).get("artworks", [])]
    t.check("M4 换 seed 会得到不同顺序", ids_other != ids_first)

    # ---------------- M8：写操作接口只收 POST ----------------
    for path in ("/api/bookmark-subs/check", "/api/scan", "/api/sync", "/api/open-folder"):
        status, _text, headers = h.request(base + path, method="GET")
        t.equal("M8 GET %s 被拒（405）" % path, status, 405)
        allow = h.header(headers, "Allow")
        t.check("M8 %s 的 Allow 头是 POST" % path, "POST" in allow.upper(), "Allow=%r" % allow)
    # 批量删除只保留 POST：GET 会落到 /api/artworks/{artwork_id} 被整数校验拒绝，
    # 老版 DELETE /api/artworks/batch-delete 的写法也必须不再能删东西
    status, _text, _headers = h.request(base + "/api/artworks/batch-delete", method="GET")
    t.check("M8 批量删除不能通过 GET 触发", status in (405, 422), "status=%d" % status)
    status, _text, _headers = h.request(base + "/api/artworks/batch-delete", method="DELETE")
    t.check("M8 旧版 DELETE 批量删除已失效", status in (405, 422), "status=%d" % status)
    status, text, _headers = h.post_json(base, "/api/artworks/batch-delete", {"artwork_ids": []})
    t.equal("M8 新版 POST 批量删除可用", status, 200)

    # ---------------- P0：本机访问守卫（Host / Origin / Referer）----------------
    # 统一用「空列表批量删除」作探测体：即便守卫失效也不会改动任何归档数据。
    t.equal("P0 正常本机 GET 仍可用", h.get(base, "/api/tags")[0], 200)
    status, _text = h.request_with_headers(base, "/api/tags", host="evil.example")
    t.equal("P0 非本机 Host 被拒绝（防 DNS rebinding）", status, 403)
    status, _text = h.request_with_headers(base, "/api/tags",
                                           host="127.0.0.1:%d" % server.port)
    t.equal("P0 带端口的本机 Host 放行", status, 200)

    empty_delete = json.dumps({"artwork_ids": []})
    status, _text = h.request_with_headers(
        base, "/api/artworks/batch-delete", method="POST", body=empty_delete,
        origin="http://evil.example")
    t.equal("P0 跨站 Origin 的 POST 被拒绝（防 CSRF）", status, 403)
    status, _text = h.request_with_headers(
        base, "/api/artworks/batch-delete", method="POST", body=empty_delete,
        referer="http://evil.example/page")
    t.equal("P0 无 Origin 但跨站 Referer 的 POST 被拒绝", status, 403)
    status, _text = h.request_with_headers(
        base, "/api/artworks/batch-delete", method="POST", body=empty_delete,
        origin="http://127.0.0.1:%d" % server.port)
    t.equal("P0 同源 Origin 的 POST 放行", status, 200)
    status, _text = h.request_with_headers(
        base, "/api/artworks/batch-delete", method="POST", body=empty_delete)
    t.equal("P0 不带 Origin/Referer 的本地 POST 仍可用", status, 200)
    t.equal("P0 拦截之后服务仍然可用", h.get(base, "/api/tags")[0], 200)

    # ---------------- L2：删除响应形状 ----------------
    status, text, _headers = h.post_json(base, "/api/artworks/batch-delete", {"artwork_ids": []})
    t.equal("L2 空列表删除返回 200", status, 200)
    payload = h.load_json(text) or {}
    t.check("L2 响应含 files_removed", "files_removed" in payload, "键=%s" % sorted(payload))
    t.check("L2 响应含 files_failed", "files_failed" in payload, "键=%s" % sorted(payload))
    t.equal("L2 空列表没有删除任何文件", payload.get("files_removed"), [])

    # ---------------- 单实例锁端到端 ----------------
    lock_path = os.path.join(run_mod.paths.DATA_DIR, run_mod.LOCK_FILE_NAME)
    t.check("锁端到端：服务运行期间锁文件存在", os.path.exists(lock_path))
    raw = ""
    if os.path.exists(lock_path):
        with open(lock_path, encoding="ascii", errors="ignore") as handle:
            raw = handle.read().strip()
    t.check("锁端到端：锁内容是一个存活进程的 PID",
            raw.isdigit() and run_mod._pid_is_alive(int(raw)) is True, "锁内容=%r" % raw)
    t.equal("锁端到端：持锁期间再申请会被拒绝", run_mod.acquire_single_instance(), None)
    with open(lock_path, encoding="ascii", errors="ignore") as handle:
        t.check("锁端到端：被拒绝的一方没有破坏锁文件", handle.read().strip() == raw)

    other_port = server.port + 1
    tmp = tempfile.mkdtemp(prefix="pa-second-")
    log_path = os.path.join(tmp, "second.log")
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [sys.executable, os.path.join(h.ROOT, "run.py"), "--port", str(other_port)],
            cwd=h.ROOT, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            stdout=log, stderr=subprocess.STDOUT)
        try:
            code = proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            code = None
    with open(log_path, encoding="utf-8", errors="replace") as handle:
        second_out = handle.read()
    t.equal("单实例锁：第二个实例以退出码 1 结束", code, 1)
    t.check("单实例锁：提示已在运行中", "已在运行中" in second_out, second_out.strip()[:120])
    t.check("单实例锁：第二个实例没有占用端口", h.get("http://127.0.0.1:%d" % other_port, "/api/tags", timeout=5)[0] == 0)
    t.equal("单实例锁：第一个实例仍在服务", h.get(base, "/api/tags")[0], 200)


def app_random_sql():
    """直接问实现要随机排序片段，用来确认它不再依赖 SQLite 的 RANDOM()。"""
    from app import main as app_main

    return app_main._random_order_sql(12345) + app_main._random_order_sql(0)