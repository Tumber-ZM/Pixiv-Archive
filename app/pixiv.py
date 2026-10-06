import os
import re
import time
import requests
from urllib.parse import urljoin, urlsplit
from pixivpy3 import AppPixivAPI
from dotenv import load_dotenv
from app import paths
from app.direct_connect import enable_direct, DirectAdapter, refresh_ips

load_dotenv(paths.ENV_FILE)

REQUEST_TIMEOUT = 30

# 官方图片主机（下载/头像字节的来源），镜像替换只作用于这些域名
_PIXIV_IMAGE_HOSTS = frozenset({"i.pximg.net", "i-f.pximg.net", "s.pximg.net"})
_SCHEME_RE = re.compile(r"^https?://")


def image_mirror_host():
    """设置里配置的第三方图片镜像域名（PIXIV_IMAGE_MIRROR，留空=不启用）。"""
    mirror = (os.getenv("PIXIV_IMAGE_MIRROR", "") or "").strip()
    if not mirror:
        return ""
    mirror = _SCHEME_RE.sub("", mirror).strip().rstrip("/")
    # 只保留主机+路径前缀部分，拒绝含空格的无效输入
    if not mirror or re.search(r"\s", mirror):
        return ""
    return mirror


def apply_image_mirror(url):
    """若启用了镜像，把 pixiv 官方图片 URL 的域名替换为镜像域名（保留路径与查询串）。

    仅影响图片字节的来源，不改变 API 调用与 URL 获取逻辑；
    非官方图片主机（含已是镜像的 URL）原样返回，保证幂等。
    """
    if not url:
        return url
    mirror = image_mirror_host()
    if not mirror:
        return url
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    host = (parsed.hostname or "").lower()
    if host not in _PIXIV_IMAGE_HOSTS:
        return url
    if not _SCHEME_RE.match(mirror):
        mirror = "https://" + mirror
    return mirror + parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _allowed_image_hosts():
    """允许抓取的图片主机：Pixiv 官方图片域名 + 设置里配置的镜像主机。

    `fetch_profile_image` 的 URL 来自数据库（源头是 Pixiv API 响应）。若链路被
    中间人篡改，攻击者可以把头像地址指向内网 / 本机地址做探测（受限 SSRF），
    因此只认这两类主机。
    """
    hosts = set(_PIXIV_IMAGE_HOSTS)
    mirror = image_mirror_host()
    if mirror:
        try:
            host = (urlsplit("https://" + mirror).hostname or "").lower()
        except ValueError:
            host = ""
        if host:
            hosts.add(host)
    return hosts


# URL 里不该出现的字符：反斜杠、空白、控制字符。合法图片 URL 不含这些。
_FORBIDDEN_URL_CHARS_RE = re.compile(r"[\\\s\x00-\x1f\x7f]")


def _is_allowed_image_url(url, allowed_hosts=None):
    """图片 URL 白名单校验：必须 https，且主机在官方域名 / 已配置镜像之内。

    判定分四层，任一层不过即拒（fail closed）：

    1. 拒绝含反斜杠 / 空白 / 控制字符的 URL —— urllib3 把 `\\` 当作 authority
       终止符，与 urlsplit 的解析不一致，`https://127.0.0.1\\@i.pximg.net/x`
       会被 urlsplit 认成主机 i.pximg.net，而实际连的是 127.0.0.1；
    2. scheme 必须 https，且不含 userinfo（`user:pass@host` 形态一律拒）；
    3. urlsplit 取出的 hostname 命中白名单；
    4. 交叉复核：HTTP 客户端实际会连接的主机必须与第 3 步一致，挡掉解析器差异。

    私网 / 环回地址不在白名单里，无需另设 IP 段规则。
    """
    if not url or not isinstance(url, str):
        return False
    if _FORBIDDEN_URL_CHARS_RE.search(url):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if allowed_hosts is None:
        allowed_hosts = _allowed_image_hosts()
    if host not in allowed_hosts:
        return False
    return _client_hostname(url) == host


def _client_hostname(url):
    """HTTP 客户端（requests / urllib3）实际会连接的 hostname。

    urlsplit 与 urllib3 对同一串 URL 的解析可能不一致，白名单若只看 urlsplit
    就会被解析器差异绕过。这里借 requests 的 PreparedRequest 拿到真正会被请求
    的 URL 再取主机；解析失败一律返回空串（fail closed）。
    """
    try:
        prepared = requests.Request("GET", url).prepare()
    except Exception:
        return ""
    try:
        return (urlsplit(prepared.url).hostname or "").lower()
    except ValueError:
        return ""


_MAX_IMAGE_REDIRECTS = 3


def _fetch_allowed_image(session, url, allowed_hosts, timeout):
    """按白名单逐跳抓取图片字节；重定向的每一跳都要重新过白名单。

    requests 默认自动跟随重定向，而白名单只校验初始 URL —— 白名单主机若返回
    302 指向内网地址就会被跟过去。这里关掉自动跟随，自己走跳并逐跳复核。
    """
    current = url
    for _ in range(_MAX_IMAGE_REDIRECTS + 1):
        if not _is_allowed_image_url(current, allowed_hosts):
            return None
        try:
            resp = session.get(current, timeout=timeout, allow_redirects=False)
        except Exception:
            return None
        if resp.status_code == 200:
            return resp.content
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location") or resp.headers.get("Location")
            if not location:
                return None
            current = urljoin(current, location)
            continue
        return None
    return None


def open_allowed_image_stream(session, url, timeout, allowed_hosts=None):
    """按白名单逐跳解析后返回最终响应（stream=True，调用方负责 close）。

    与 `_fetch_allowed_image` 同一套逐跳校验，供「流式落盘」的下载路径复用。
    校验不过 / 重定向超出上限时抛 ValueError（fail closed）。
    """
    if allowed_hosts is None:
        allowed_hosts = _allowed_image_hosts()
    current = url
    for _ in range(_MAX_IMAGE_REDIRECTS + 1):
        if not _is_allowed_image_url(current, allowed_hosts):
            raise ValueError("图片地址不在允许的域名范围内")
        resp = session.get(current, timeout=timeout, stream=True, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location") or resp.headers.get("Location")
            resp.close()
            if not location:
                raise ValueError("重定向缺少 Location")
            current = urljoin(current, location)
            continue
        return resp
    raise ValueError("重定向次数超出上限")


class PixivError(Exception):
    """Pixiv 操作异常基类。"""


class PixivAuthError(PixivError):
    """认证失败：refresh token 缺失 / 无效 / 认证请求失败。"""


class PixivNetworkError(PixivError):
    """网络 / 传输层失败（连接、超时、SSL 等）。"""


class PixivDeletedError(PixivError):
    """作品在 Pixiv 上已删除或不存在。"""


_AUTH_MSG_HINTS = (
    "auth", "token", "session", "login", "refresh",
    "invalid_grant", "401", "expired", "有効期限", "セッション",
)

# “作品确实不存在/已删除”的典型文案（英文 + 日文 + 中文）。只有命中这些才抛
# PixivDeletedError；429/5xx 等其它错误一律按网络错误处理，避免被误标 deleted。
# 这只是兜底手段：首选判定信号是 HTTP 状态码（见 _missing_illust）。
_DELETED_MSG_HINTS = (
    "not found", "does not exist", "doesn't exist", "not exist", "no such",
    "deleted", "見つかりません", "削除", "存在しません", "不存在",
)

# Pixiv 错误对象里可能承载说明文字的字段（含 *_lang 本地化变体）
_ERROR_MSG_KEYS = ("message", "user_message", "reason", "message_lang", "user_message_lang")


def _looks_like_auth_error(msg):
    m = (msg or "").lower()
    return any(k in m for k in _AUTH_MSG_HINTS)


def _looks_like_deleted(msg):
    # 归一化分隔符：Pixiv 的 reason 字段可能是 not_found / not-found 这类形式，
    # 直接子串匹配会漏掉，统一把 _ 和 - 换成空格再比对。
    m = re.sub(r"[_\-]+", " ", (msg or "").lower())
    return any(k in m for k in _DELETED_MSG_HINTS)


def _fmt_error(err):
    if isinstance(err, dict):
        parts = [v for v in (err.get(k) for k in _ERROR_MSG_KEYS) if v]
        return " / ".join(parts) or str(err)
    return str(err)


def _error_is_empty(err):
    """error 对象存在但没有任何说明文字。

    Pixiv 对「不存在的作品」会返回字段全空的 error 对象，此时 _fmt_error 只能
    给出 dict 字面量，任何关键词表都必然漏掉这种形态。
    """
    if not isinstance(err, dict):
        return not str(err or "").strip()
    return not any(str(err.get(k) or "").strip() for k in _ERROR_MSG_KEYS)


def _missing_illust(status, err, msg):
    """判断这个错误是否表示「作品在 Pixiv 上不存在」。

    首选信号是 HTTP 状态码：GET /v1/illust/detail 返回 404 就是权威答案，完全
    不依赖文案。其次看错误文案关键词。最后兜底处理「空的 error 对象」这一形态。

    明确的临时故障（429 / 5xx）与认证、风控类（401 / 403）绝不判为不存在——
    宁可重试，也不能把还在的作品标成已删除。
    """
    if status in (401, 403):
        return False
    if status is not None and (status >= 500 or status == 429):
        return False
    if status == 404:
        return True
    if _looks_like_deleted(msg):
        return True
    return _error_is_empty(err)


def _build_session():
    """Build a plain requests.Session (replaces cloudscraper for direct mode)."""
    return _build_session_with_referer("https://app-api.pixiv.net/")


def _build_session_with_referer(referer, use_proxy=False):
    session = requests.Session()
    proxy = (os.getenv("PIXIV_PROXY", "") or "").strip() if use_proxy else ""
    if proxy:
        # 代理模式走标准 TLS（SNI + 完整证书校验）：DirectAdapter 的无 SNI 握手
        # 是给 IP 直连用的，经代理隧道时 Cloudflare 对无 SNI 连接会直接掐断
        # （SSLEOFError）。requests.Session 原生支持 proxies，无需挂自定义 adapter。
        session.proxies.update({"http": proxy, "https": proxy})
    else:
        adapter = DirectAdapter()
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.verify = False
    session.headers.update({
        "referer": referer,
        "User-Agent": "PixivIOSApp/5.8.0",
    })
    return session


def fetch_profile_image(url, timeout=REQUEST_TIMEOUT):
    """Download a pixiv author profile image (for avatar cache). Returns bytes or None."""
    if not _is_allowed_image_url(url):
        # URL 源自数据库里的 Pixiv API 响应，可能被中间人篡改；非官方 / 非镜像主机
        # 一律不发起请求（受限 SSRF 防护）。
        return None
    try:
        enable_direct()
        try:
            use_proxy = get_pixiv_client().mode == "proxy"
        except Exception:
            use_proxy = False
        session = _build_session_with_referer("https://www.pixiv.net/", use_proxy=use_proxy)
        allowed_hosts = _allowed_image_hosts()
        candidates = [url]
        mirrored = apply_image_mirror(url)
        if mirrored and mirrored != url:
            # 优先走镜像，失败回退官方地址（部分镜像不服务 s.pximg.net 头像）
            candidates.insert(0, mirrored)
        for candidate in candidates:
            if not _is_allowed_image_url(candidate, allowed_hosts):
                continue
            content = _fetch_allowed_image(session, candidate, allowed_hosts, timeout)
            if content is not None:
                return content
        return None
    except Exception:
        return None


class PixivClient:
    def __init__(self):
        self.mode = os.getenv("PIXIV_MODE", "auto").strip().lower()
        self.proxy = os.getenv("PIXIV_PROXY", "")
        self._last_auth = 0
        self._auth_ttl = 3000
        self._build_api()

    def _build_api(self):
        if self.mode == "direct":
            enable_direct()
            self.api = AppPixivAPI()
            self.api.requests = _build_session()
            self.api.requests_kwargs = {"timeout": REQUEST_TIMEOUT}
        elif self.mode == "proxy" and self.proxy:
            # 注意：不能用 AppPixivAPI(proxies=...) 再赋值 requests_kwargs——
            # 构造函数把 proxies 存进 requests_kwargs，后续整体赋值会把它覆盖丢失
            self.api = AppPixivAPI()
            self.api.requests = _build_session_with_referer(
                "https://app-api.pixiv.net/", use_proxy=True
            )
            self.api.requests_kwargs = {"timeout": REQUEST_TIMEOUT}
        else:
            # auto: try direct first
            enable_direct()
            self.api = AppPixivAPI()
            self.api.requests = _build_session()
            self.api.requests_kwargs = {"timeout": REQUEST_TIMEOUT}
            self._auto_direct = True

    def _ensure_auth(self):
        refresh_token = os.getenv("PIXIV_REFRESH_TOKEN", "")
        if not refresh_token:
            raise PixivAuthError("未设置 PIXIV_REFRESH_TOKEN（请到 设置 → Pixiv Refresh Token 填写）")
        now = time.time()
        if now - self._last_auth < self._auth_ttl:
            return
        try:
            self.api.auth(refresh_token=refresh_token)
            self._last_auth = now
        except Exception as e:
            if getattr(self, "_auto_direct", False) and self.proxy:
                # auto mode: fall back to Clash proxy
                try:
                    self.mode = "proxy"
                    self._auto_direct = False
                    self.api = AppPixivAPI()
                    self.api.requests = _build_session_with_referer(
                        "https://app-api.pixiv.net/", use_proxy=True
                    )
                    self.api.requests_kwargs = {"timeout": REQUEST_TIMEOUT}
                    self.api.auth(refresh_token=refresh_token)
                    self._last_auth = now
                except Exception as e2:
                    raise PixivAuthError(f"认证失败（直连与代理均失败）：{e2}")
            else:
                cause = str(e)
                if "SSLEOF" in cause or "UNEXPECTED_EOF" in cause:
                    cause += "（常见原因：代理未把 oauth.secure.pixiv.net 走节点，请检查代理分流规则）"
                raise PixivAuthError(f"认证失败：{cause[:200]}")

    def get_illust_detail(self, illust_id):
        try:
            self._ensure_auth()
            return self._detail_once(illust_id)
        except PixivNetworkError:
            if getattr(self, "_auto_direct", False) and self.mode == "direct":
                return self._retry_direct(illust_id)
            raise
        except PixivError:
            raise
        except Exception as e:
            raise PixivNetworkError(f"请求作品详情失败：{e}")

    @staticmethod
    def parse_next_qs(next_url):
        """把 pixiv 返回的 next_url 解析成翻页参数 dict（offset / max_bookmark_id 等）。"""
        return AppPixivAPI.parse_qs(next_url)

    def list_user_bookmarks(self, user_id, max_bookmark_id=None):
        """取用户公开收藏列表一页（按收藏时间倒序）。返回 (illusts, next_url)。"""
        try:
            self._ensure_auth()
            response = self.api.user_bookmarks_illust(
                user_id, max_bookmark_id=max_bookmark_id
            )
        except Exception as e:
            raise PixivNetworkError(f"请求收藏列表失败：{e}")
        if response.get("error"):
            raise PixivNetworkError(_fmt_error(response.get("error")))
        return response.get("illusts", []), response.get("next_url")

    def get_user_display_name(self, user_id):
        """取用户显示名（user_detail），失败回退为 ID 字符串。"""
        try:
            self._ensure_auth()
            detail = self.api.user_detail(user_id)
            if not detail.get("error"):
                name = (detail.get("user") or {}).get("name", "")
                if name:
                    return name
        except Exception:
            pass
        return str(user_id)

    def _retry_direct(self, illust_id):
        """auto-direct 模式下刷新直连 IP 并重试一次。"""
        try:
            refresh_ips()
            self._last_auth = 0
            self._ensure_auth()
            return self._detail_once(illust_id)
        except PixivError:
            raise
        except Exception as e:
            raise PixivNetworkError(f"刷新直连 IP 后仍失败：{e}")

    def _detail_request(self, illust_id):
        """请求作品详情，返回 (解析后的响应, HTTP 状态码或 None)。

        这里没有直接调用 api.illust_detail()：pixivpy3 3.7.5 的 illust_detail()
        完全不检查 HTTP 状态码（aapi.py 里就是发请求后 parse_json），404 的 JSON
        错误体会原样返回，状态码就此丢失；而「404 = 作品不存在」是判定已删除最
        可靠、且不依赖任何文案的信号。若 pixivpy3 内部结构变化导致拿不到
        Response，则回退到公开方法（此时状态码为 None，由文案判断兜底）。
        """
        try:
            res = self.api.no_auth_requests_call(
                "GET",
                "%s/v1/illust/detail" % self.api.hosts,
                params={"illust_id": illust_id},
                req_auth=True,
            )
        except Exception:
            res = None
        if res is None:
            try:
                return self.api.illust_detail(illust_id), None
            except Exception as e:
                raise PixivNetworkError(f"请求作品详情失败：{e}")
        status = getattr(res, "status_code", None)
        try:
            return self.api.parse_result(res), status
        except Exception as e:
            # 响应体不是 JSON（常见于 5xx / 中间盒页面）：没有可靠信号，按网络错误重试
            raise PixivNetworkError(f"作品详情响应无法解析（HTTP {status}）：{e}")

    def _detail_once(self, illust_id):
        response, status = self._detail_request(illust_id)
        err = response.get("error")
        msg = _fmt_error(err) if err else ""

        if err and _looks_like_auth_error(msg):
            # 疑似 token 过期等认证问题：重新认证一次再试
            self._last_auth = 0
            try:
                self.api.auth(refresh_token=os.getenv("PIXIV_REFRESH_TOKEN", ""))
            except Exception as e:
                raise PixivAuthError(f"认证失效且重新认证失败：{e}")
            response, status = self._detail_request(illust_id)
            err = response.get("error")
            msg = _fmt_error(err) if err else ""
            if err and _looks_like_auth_error(msg):
                # 刚认证过仍是认证类错误：判定为认证问题，由 sync.py 中断整批
                raise PixivAuthError(f"重新认证后仍返回认证错误：{msg}")

        if err:
            empty = _error_is_empty(err)
            if _missing_illust(status, err, msg):
                if not empty:
                    raise PixivDeletedError(msg)
                raise PixivDeletedError(
                    f"Pixiv 返回 HTTP {status}，作品不存在" if status is not None
                    else "Pixiv 未返回作品数据，且未给出错误说明")
            # 429 / 5xx / 未知错误：不是“作品不存在”，按网络错误抛出，
            # 由 sync.py 记 sync_error 并在下次同步重试（不改 deleted 状态）
            if not empty:
                raise PixivNetworkError(msg)
            raise PixivNetworkError(
                f"Pixiv 返回 HTTP {status}" if status is not None
                else "Pixiv 请求失败，且未给出错误说明")

        illust = response.get("illust")
        if not illust:
            raise PixivDeletedError("Pixiv 返回的作品数据为空")
        return self._parse_illust(illust)

    def _parse_illust(self, illust):
        tags = []
        for tag_data in illust.get("tags", []):
            tags.append({
                "name": tag_data.get("name", ""),
                "translated_name": tag_data.get("translated_name", ""),
            })

        meta_pages = illust.get("meta_pages", [])
        if meta_pages:
            image_urls = [mp.get("image_urls", {}).get("original", "") for mp in meta_pages]
        else:
            image_urls = [illust.get("meta_single_page", {}).get("original_image_url", "")]

        return {
            "pixiv_id": illust.get("id", 0),
            "title": illust.get("title", ""),
            "description": illust.get("caption", ""),
            "author_id": illust.get("user", {}).get("id", 0),
            "author_name": illust.get("user", {}).get("name", ""),
            "author_profile_image": illust.get("user", {}).get("profile_image_urls", {}).get("medium", ""),
            "create_date": illust.get("create_date", ""),
            "page_count": illust.get("page_count", 1),
            "width": illust.get("width", 0),
            "height": illust.get("height", 0),
            # Pixiv：1 = 非 AI，2 = AI 生成。缺失时按 1（非 AI）处理，避免误标
            "ai_type": illust.get("illust_ai_type", 1),
            "tags": tags,
            "image_urls": image_urls,
            "total_view": illust.get("total_view", 0),
            "total_bookmarks": illust.get("total_bookmarks", 0),
        }


_pixiv_client = None


def get_pixiv_client():
    global _pixiv_client
    if _pixiv_client is None:
        _pixiv_client = PixivClient()
    return _pixiv_client


def reset_pixiv_client():
    global _pixiv_client
    _pixiv_client = None


def refresh_direct_ips():
    return refresh_ips()
