import os
import re
import time
import requests
from urllib.parse import urlsplit
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


def _looks_like_auth_error(msg):
    m = (msg or "").lower()
    return any(k in m for k in _AUTH_MSG_HINTS)


def _fmt_error(err):
    if isinstance(err, dict):
        parts = [
            v for v in (err.get("message"), err.get("user_message"), err.get("reason"))
            if v
        ]
        return " / ".join(parts) or str(err)
    return str(err)


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
    if not url:
        return None
    try:
        enable_direct()
        try:
            use_proxy = get_pixiv_client().mode == "proxy"
        except Exception:
            use_proxy = False
        session = _build_session_with_referer("https://www.pixiv.net/", use_proxy=use_proxy)
        candidates = [url]
        mirrored = apply_image_mirror(url)
        if mirrored and mirrored != url:
            # 优先走镜像，失败回退官方地址（部分镜像不服务 s.pximg.net 头像）
            candidates.insert(0, mirrored)
        for candidate in candidates:
            try:
                resp = session.get(candidate, timeout=timeout)
                if resp.status_code == 200:
                    return resp.content
            except Exception:
                continue
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

    def _detail_once(self, illust_id):
        try:
            response = self.api.illust_detail(illust_id)
        except Exception as e:
            raise PixivNetworkError(f"请求作品详情失败：{e}")

        if response.get("error"):
            err = response.get("error")
            msg = _fmt_error(err)
            if _looks_like_auth_error(msg):
                # 疑似 token 过期等认证问题：重新认证一次再试
                try:
                    self._last_auth = 0
                    self.api.auth(refresh_token=os.getenv("PIXIV_REFRESH_TOKEN", ""))
                    response = self.api.illust_detail(illust_id)
                except Exception as e:
                    raise PixivAuthError(f"认证失效且重新认证失败：{e}")
                if response.get("error"):
                    raise PixivAuthError(f"重新认证后仍返回错误：{_fmt_error(response.get('error'))}")
            else:
                raise PixivDeletedError(msg)

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
