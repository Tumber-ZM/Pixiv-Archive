"""P0 本机访问守卫的决策表（离线，不起服务）。

直接调用 app/main.py 里的 `_local_access_guard`，用构造的请求头核对每一种判定：
Host 白名单（防 DNS rebinding）与 Origin/Referer 校验（防本机 CSRF）。
不起真实服务，因此即便端口被系统保留也能跑。
"""
import asyncio

import harness as h

h.bootstrap()

TITLE = "P0 本机访问守卫（Host / Origin / Referer）"
NEEDS_SERVER = False


def _call(method, host=None, origin=None, referer=None):
    """构造请求并返回守卫给出的响应对象。"""
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse

    from app import main as app_main

    raw = []
    if host is not None:
        raw.append((b"host", host.encode("utf-8")))
    if origin is not None:
        raw.append((b"origin", origin.encode("utf-8")))
    if referer is not None:
        raw.append((b"referer", referer.encode("utf-8")))
    scope = {"type": "http", "method": method, "headers": raw,
             "path": "/", "query_string": b"", "scheme": "http"}
    request = Request(scope)

    async def call_next(_request):
        return PlainTextResponse("ok", status_code=200)

    return asyncio.run(app_main._local_access_guard(request, call_next))


def run(t):
    # ---- Host 白名单 ----
    t.equal("本机 Host 放行", _call("GET", host="127.0.0.1:6814").status_code, 200)
    t.equal("localhost 放行", _call("GET", host="localhost:6814").status_code, 200)
    t.equal("IPv6 本机放行", _call("GET", host="[::1]:6814").status_code, 200)
    t.equal("Host 大小写不敏感", _call("GET", host="LocalHost:6814").status_code, 200)
    t.equal("非本机 Host 拒绝（DNS rebinding）",
            _call("GET", host="evil.example:6814").status_code, 403)
    t.equal("无 Host 头不拦（HTTP/1.0 客户端）", _call("GET").status_code, 200)

    # ---- Origin / Referer 校验 ----
    t.equal("本机 Origin 的 POST 放行",
            _call("POST", host="127.0.0.1:6814", origin="http://127.0.0.1:6814").status_code, 200)
    t.equal("跨站 Origin 的 POST 拒绝",
            _call("POST", host="127.0.0.1:6814", origin="http://evil.example").status_code, 403)
    t.equal("Origin: null 的 POST 拒绝",
            _call("POST", host="127.0.0.1:6814", origin="null").status_code, 403)
    t.equal("无 Origin 但跨站 Referer 的 POST 拒绝",
            _call("POST", host="127.0.0.1:6814", referer="http://evil.example/p").status_code, 403)
    t.equal("无 Origin 但本机 Referer 的 POST 放行",
            _call("POST", host="127.0.0.1:6814",
                  referer="http://127.0.0.1:6814/p").status_code, 200)
    t.equal("无 Origin 无 Referer 的本地 POST 放行",
            _call("POST", host="127.0.0.1:6814").status_code, 200)

    # ---- 只读方法不做 Origin 校验（避免误伤正常浏览）----
    t.equal("GET 带跨站 Origin 不拦（只读方法）",
            _call("GET", host="127.0.0.1:6814", origin="http://evil.example").status_code, 200)
    t.equal("DELETE 跨站 Origin 拒绝",
            _call("DELETE", host="127.0.0.1:6814", origin="http://evil.example").status_code, 403)

    # ---- 两个校验的组合 ----
    t.equal("Host 非本机时即便 Origin 是本机也拒绝",
            _call("POST", host="evil.example", origin="http://127.0.0.1:6814").status_code, 403)
