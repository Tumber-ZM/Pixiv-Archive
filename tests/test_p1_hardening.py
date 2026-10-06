"""P1 加固（P1-4 / P1-8）：

- P1-4  作者页 / 标签页缩略图的 `alt` 改用 escapeAttr —— escapeHtml 不转义引号，
        放在 `alt="..."` 里仍能被 `"` 闭合属性、注入事件处理器。
- P1-8a /api/settings 不再回显 token 片段（真实服务断言见 `test_http_smoke.py`）。
- P1-8b `create_date` 插入 innerHTML 前补转义（该字段源自 Pixiv API，可被篡改）。
- P1-8c 图片 URL 白名单（防受限 SSRF）：`fetch_profile_image` 与收藏订阅下载
        都只允许「官方图片域名 / 已配置镜像」，且重定向逐跳复核；
        另含 tag.html 的 `|tojson`（防标签名里的反斜杠打断脚本字面量）。

全部离线：只读源码 + 直调纯函数 + 假 session，不联网、不起服务。
"""
import contextlib
import os
import re
import tempfile

import harness as h

h.bootstrap()
from app import pixiv  # noqa: E402

TITLE = "P1 加固（alt 转义 / token 回显 / create_date / 图片白名单）"
NEEDS_SERVER = False

TEMPLATES = os.path.join(h.ROOT, "app", "templates")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as handle:
        return handle.read()


def run(t):
    _alt_escaping(t)
    _token_preview_removed(t)
    _create_date_escaping(t)
    _tag_name_tojson(t)
    _image_url_whitelist(t)
    _fetch_profile_image_gate(t)
    _redirect_hops(t)
    _download_ssrf(t)


# ---------------- P1-4：alt 转义 ----------------

# 匹配 alt="${...}"（表达式里不含花括号，够用且不会误吞相邻模板）
_ALT_RE = re.compile(r'alt="\$\{([^{}]*)\}"')


def _alt_escaping(t):
    for name in ("author.html", "tag.html"):
        text = _read(TEMPLATES, name)
        t.check("P1-4 %s 缩略图 alt 用 escapeAttr" % name,
                'alt="${escapeAttr(title)}"' in text)
        t.check("P1-4 %s 不再有裸 alt=\"${title}\"" % name, 'alt="${title}"' not in text)

    # 全模板扫描：任何 alt="${...}" 插值都必须经 escapeAttr（引号也要转义）。
    # 只查「是否出现 escapeAttr(」——用 escapeHtml 会漏转义引号，等价于没修。
    unescaped = []
    for base, _dirs, files in os.walk(TEMPLATES):
        for name in files:
            if not name.endswith(".html"):
                continue
            path = os.path.join(base, name)
            for expr in _ALT_RE.findall(_read(path)):
                if "escapeAttr(" not in expr:
                    unescaped.append("%s: alt=\"${%s}\""
                                     % (os.path.relpath(path, h.ROOT), expr))
    t.equal("P1-4 模板里所有 alt 插值都经过 escapeAttr", unescaped, [])


# ---------------- P1-8a：token 片段不再回显 ----------------

def _token_preview_removed(t):
    main_src = _read(h.ROOT, "app", "main.py")
    index = _read(TEMPLATES, "index.html")
    t.check("P1-8a 后端不再构造 token_preview", "token_preview" not in main_src)
    t.check("P1-8a 前端不再读取 token_preview", "token_preview" not in index)
    t.check("P1-8a /api/settings 仍返回 has_token", '"has_token"' in main_src)
    t.check("P1-8a 前端改为按 has_token 显示「已设置 / 未设置」",
            "data.has_token ? '已设置' : '未设置'" in index)


# ---------------- P1-8b：create_date 转义 ----------------

def _create_date_escaping(t):
    for name in ("artwork.html", "index.html"):
        text = _read(TEMPLATES, name)
        t.check("P1-8b %s 的 create_date 经 escapeHtml" % name,
                "escapeHtml(data.create_date" in text)
        t.check("P1-8b %s 不再裸插 create_date" % name,
                "${data.create_date || '未知'}" not in text)


# ---------------- tag.html：标签名改用 |tojson ----------------

def _tag_name_tojson(t):
    tag = _read(TEMPLATES, "tag.html")
    t.check("P1-8c tag.html 用 |tojson 传标签名",
            "{{ tag_name | tojson }}" in tag)
    t.check("P1-8c tag.html 不再把 Jinja 变量裸插进 JS 字符串",
            '"{{ tag_name }}"' not in tag)


# ---------------- P1-8c：图片 URL 白名单（纯函数决策表） ----------------

def _image_url_whitelist(t):
    for url in ("https://i.pximg.net/img/x.jpg",
                "https://i-f.pximg.net/img/x.jpg",
                "https://s.pximg.net/img/x.jpg",
                "https://I.PXIMG.NET/img/x.jpg"):
        t.check("P1-8c 放行官方图片域名 %s" % url,
                pixiv._is_allowed_image_url(url) is True)

    rejected = [
        ("http://i.pximg.net/x.jpg", "非 https"),
        ("https://127.0.0.1/x.jpg", "环回地址"),
        ("https://localhost/x.jpg", "localhost"),
        ("https://192.168.1.10/x.jpg", "私网地址"),
        ("https://169.254.169.254/x.jpg", "云元数据地址"),
        ("https://evil.example/x.jpg", "无关域名"),
        ("https://evil.example/i.pximg.net/x.jpg", "路径里冒充官方域名"),
        ("https://i.pximg.net.evil.example/x.jpg", "后缀混淆"),
        ("https://i.pximg.net@evil.example/x.jpg", "userinfo 欺骗"),
        ("https://i.pximg.net./x.jpg", "尾点 FQDN"),
        # 解析器差异绕过：urlsplit 看到 i.pximg.net，urllib3 实际连 127.0.0.1
        ("https://127.0.0.1\\@i.pximg.net/x", "反斜杠 + userinfo 绕过"),
        ("https://i.pximg.net\\@127.0.0.1/x", "反斜杠倒置绕过"),
        ("https://evil.example\\@i.pximg.net/x", "反斜杠 + 无关域名绕过"),
        ("https://i.pximg.net%5c@evil.example/x", "百分号编码反斜杠"),
        ("https://i.pximg.net/a\nb.jpg", "URL 内含换行"),
        ("https://i.pximg.net/a b.jpg", "URL 内含空格"),
        ("", "空串"),
        (None, "None"),
        (12345, "非字符串"),
        ("not a url", "非 URL"),
    ]
    for url, why in rejected:
        t.check("P1-8c 拒绝 %s（%s）" % (url if url else repr(url), why),
                pixiv._is_allowed_image_url(url) is False)

    saved = os.environ.get("PIXIV_IMAGE_MIRROR")
    try:
        os.environ["PIXIV_IMAGE_MIRROR"] = "mirror.example.com/pximg"
        t.check("P1-8c 已配置镜像的主机进入白名单",
                "mirror.example.com" in pixiv._allowed_image_hosts())
        t.check("P1-8c 放行已配置镜像上的图片",
                pixiv._is_allowed_image_url("https://mirror.example.com/pximg/x.jpg") is True)
        t.check("P1-8c 配置镜像后官方域名仍放行",
                pixiv._is_allowed_image_url("https://i.pximg.net/x.jpg") is True)
        t.check("P1-8c 配置镜像后无关域名仍拒绝",
                pixiv._is_allowed_image_url("https://other.example/x.jpg") is False)

        os.environ["PIXIV_IMAGE_MIRROR"] = "https://mirror2.example.com"
        t.check("P1-8c 镜像写成完整 URL 也能解析出主机",
                "mirror2.example.com" in pixiv._allowed_image_hosts())

        os.environ.pop("PIXIV_IMAGE_MIRROR", None)
        t.equal("P1-8c 未配置镜像时白名单回到官方三域名",
                pixiv._allowed_image_hosts(), set(pixiv._PIXIV_IMAGE_HOSTS))
    finally:
        if saved is None:
            os.environ.pop("PIXIV_IMAGE_MIRROR", None)
        else:
            os.environ["PIXIV_IMAGE_MIRROR"] = saved


# ---------------- P1-8c：fetch_profile_image 的请求行为 ----------------

class _FakeResp:
    def __init__(self, status, content=b"", location=None):
        self.status_code = status
        self.content = content
        self.headers = {}
        if location is not None:
            self.headers["location"] = location

    def close(self):
        pass


class _FakeSession:
    """记录被请求过的 URL，并按 URL 返回预设响应（未预设的一律 404）。"""

    def __init__(self, responses=None):
        self.calls = []
        self.redirect_flags = []
        self._responses = responses or {}

    def get(self, url, timeout=None, stream=False, allow_redirects=True):
        self.calls.append(url)
        self.redirect_flags.append(allow_redirects)
        return self._responses.get(url, _FakeResp(404))


def _raise_client():
    raise RuntimeError("测试环境不构造真实 Pixiv 客户端")


@contextlib.contextmanager
def _patched(**attrs):
    """临时替换 app.pixiv 的模块属性，退出时还原。"""
    saved = {name: getattr(pixiv, name) for name in attrs}
    for name, value in attrs.items():
        setattr(pixiv, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(pixiv, name, value)


def _with_fake(session):
    return _patched(enable_direct=lambda: None,
                    get_pixiv_client=_raise_client,
                    _build_session_with_referer=lambda *a, **k: session)


def _fetch_profile_image_gate(t):
    src = _read(h.ROOT, "app", "pixiv.py")
    t.check("P1-8c fetch_profile_image 入口即做白名单校验",
            "if not _is_allowed_image_url(url):" in src)
    t.check("P1-8c 镜像 / 官方候选地址逐个复检",
            "if not _is_allowed_image_url(candidate, allowed_hosts):" in src)

    # 被拒的地址必须**完全不发起请求**（不是「发了请求但失败」）。
    # 这里用假 session + 记录调用，才能真正区分「白名单拦截」与「网络失败」。
    session = _FakeSession()
    built = []
    with _patched(enable_direct=lambda: None,
                  get_pixiv_client=_raise_client,
                  _build_session_with_referer=lambda *a, **k: (built.append(a), session)[1]):
        t.check("P1-8c 环回地址返回 None",
                pixiv.fetch_profile_image("http://127.0.0.1:9/steal.jpg") is None)
        t.check("P1-8c 反斜杠绕过写法返回 None",
                pixiv.fetch_profile_image("https://127.0.0.1\\@i.pximg.net/x") is None)
        t.check("P1-8c 无关域名返回 None",
                pixiv.fetch_profile_image("https://evil.example/x.jpg") is None)
        t.check("P1-8c 空 URL 返回 None", pixiv.fetch_profile_image("") is None)
        t.equal("P1-8c 被拒地址既没建会话也没发请求", (built, session.calls), ([], []))

    # 白名单地址：确实发起请求并取回字节
    url = "https://i.pximg.net/avatar/x.jpg"
    session = _FakeSession({url: _FakeResp(200, b"IMG")})
    with _with_fake(session):
        t.check("P1-8c 白名单地址取回字节", pixiv.fetch_profile_image(url) == b"IMG")
        t.check("P1-8c 白名单地址确实发起了请求", url in session.calls)
        t.check("P1-8c 请求过的每个地址都通过白名单",
                all(pixiv._is_allowed_image_url(u) for u in session.calls),
                "calls=%r" % session.calls)
        # 自动跟随重定向会让「一次性白名单」失效（302 可指向内网）：必须自己逐跳复核。
        t.check("P1-8c 抓取时禁用自动重定向（改为逐跳复核）",
                bool(session.redirect_flags) and all(f is False for f in session.redirect_flags),
                "redirect_flags=%r" % session.redirect_flags)


def _redirect_hops(t):
    start = "https://i.pximg.net/avatar/redirect.jpg"
    internal = "http://127.0.0.1:9/secret.jpg"

    # 白名单主机 302 指向内网 → 必须不跟随
    session = _FakeSession({start: _FakeResp(302, location=internal)})
    with _with_fake(session):
        t.check("P1-8c 重定向指向内网时返回 None",
                pixiv.fetch_profile_image(start) is None)
        t.check("P1-8c 没有对内网地址发起请求", internal not in session.calls,
                "calls=%r" % session.calls)

    # 白名单内的重定向 → 正常跟随
    hop2 = "https://i-f.pximg.net/avatar/real.jpg"
    session = _FakeSession({start: _FakeResp(302, location=hop2),
                            hop2: _FakeResp(200, b"REAL")})
    with _with_fake(session):
        t.check("P1-8c 白名单内的重定向会被跟随",
                pixiv.fetch_profile_image(start) == b"REAL")
        t.check("P1-8c 重定向的每一跳都被请求",
                start in session.calls and hop2 in session.calls,
                "calls=%r" % session.calls)

    # 下载路径（流式）同样逐跳校验
    stream_session = _FakeSession({start: _FakeResp(302, location=internal)})
    blocked = False
    try:
        pixiv.open_allowed_image_stream(stream_session, start, 5)
    except ValueError:
        blocked = True
    t.check("P1-8c 下载路径的流式抓取拒绝内网重定向", blocked)
    t.check("P1-8c 下载路径没有请求内网地址", internal not in stream_session.calls,
            "calls=%r" % stream_session.calls)
    t.check("P1-8c 流式抓取同样禁用自动重定向",
            bool(stream_session.redirect_flags)
            and all(f is False for f in stream_session.redirect_flags),
            "redirect_flags=%r" % stream_session.redirect_flags)

    # 超出重定向上限 → 拒绝
    loop_a = "https://i.pximg.net/a.jpg"
    loop_b = "https://s.pximg.net/b.jpg"
    loop_session = _FakeSession({loop_a: _FakeResp(302, location=loop_b),
                                 loop_b: _FakeResp(302, location=loop_a)})
    with _with_fake(loop_session):
        t.check("P1-8c 重定向成环时返回 None",
                pixiv.fetch_profile_image(loop_a) is None)
        t.check("P1-8c 成环时只请求了环内白名单地址",
                set(loop_session.calls) <= {loop_a, loop_b},
                "calls=%r" % loop_session.calls)


# ---------------- P1-8c：收藏订阅下载同样过白名单 ----------------

def _download_ssrf(t):
    dl = _read(h.ROOT, "app", "download.py")
    t.check("P1-8c 收藏订阅下载也校验图片白名单",
            "_is_allowed_image_url(url)" in dl)
    t.check("P1-8c 下载走逐跳校验的流式抓取",
            "open_allowed_image_stream(" in dl)
    t.check("P1-8c 下载不再直接 session.get(图片 URL)",
            "session.get(url, timeout=REQUEST_TIMEOUT, stream=True)" not in dl)

    # 行为断言（源码字符串匹配不能证明真的拦得住）：白名单外的地址必须
    # 完全不下载 —— 返回 failed + 明确原因，且一个请求都不发。
    from app import download as dl_mod  # noqa: PLC0415

    root = tempfile.mkdtemp(prefix="pa-dl-ssrf-")
    session = _FakeSession()
    illust = {
        "id": 987654,
        "title": "SSRF 探针",
        "page_count": 1,
        "meta_single_page": {"original_image_url": "http://127.0.0.1:9/steal.jpg"},
    }
    status, err = dl_mod.download_single_illust(session, illust, root, "作者")
    t.equal("P1-8c 白名单外图片下载返回 failed", status, "failed")
    t.equal("P1-8c 白名单外图片下载给出明确错误", err, "图片地址不在允许的域名范围内")
    t.equal("P1-8c 白名单外图片下载不发起任何请求", session.calls, [])
