import os
import json
import ssl
import socket
import threading
import urllib3
import urllib3.util.connection as urllib3_connection
from requests.adapters import HTTPAdapter
from app import paths
from app import envfile

# 注意：不在 import 时全局抑制 TLS 告警。免 SNI 直连必须关闭证书校验，
# 但 urllib3.disable_warnings() 是进程级全局副作用，import 本模块就静音会
# 掩盖其它模块真实的 TLS 告警；改为只在 enable_direct() 里抑制（见下）。

# Hardcoded IP overrides (mirrors pixez Hoster._constMap)
DEFAULT_IP_MAP = {
    "app-api.pixiv.net": "210.140.139.155",
    "oauth.secure.pixiv.net": "210.140.139.155",
    "i.pximg.net": "210.140.139.133",
    "s.pximg.net": "210.140.139.133",
}

# DoH servers tried in order (host -> anycast IPs). Cloudflare is primary.
DOH_SERVERS = [
    ("cloudflare-dns.com", ["104.16.248.249", "104.16.249.249"]),
    ("doh.dns.sb", ["185.222.222.222", "45.11.45.11"]),
]

_patch_lock = threading.Lock()
# urllib3 建连时通过 `urllib3.util.connection.create_connection` 解析地址
# （urllib3.connection.HTTPConnection._new_conn 里以模块属性方式调用，见 2.8.0）。
# 这里保存真正的原始函数，patch 只发生在这个模块属性上——作用范围限于
# requests/urllib3 发起的连接，不再改动进程级的 socket.getaddrinfo。
_create_connection_original = urllib3_connection.create_connection
_patched = False

ENV_KEYS = {
    "app-api.pixiv.net": "PIXIV_IP_APP_API",
    "oauth.secure.pixiv.net": "PIXIV_IP_OAUTH",
    "i.pximg.net": "PIXIV_IP_IMAGE",
    "s.pximg.net": "PIXIV_IP_STATIC",
}


def _env_path():
    return paths.ENV_FILE


def _read_env():
    return envfile.read_env(_env_path())


def _write_env(updates):
    """只更新给定的键；注释、空行与原有顺序全部保留（见 app/envfile.py）。"""
    envfile.update_env(_env_path(), updates)


def get_ip_map():
    env = _read_env()
    mapping = {}
    for host, key in ENV_KEYS.items():
        ip = env.get(key, "").strip()
        mapping[host] = ip if ip else DEFAULT_IP_MAP[host]
    return mapping


def save_ip_override(host, ip):
    if host not in ENV_KEYS:
        return
    _write_env({ENV_KEYS[host]: ip})


def _patched_create_connection(address, *args, **kwargs):
    """把 pixiv 主机的域名换成 .env 里配置的 IP，再交给 urllib3 建连。

    仅挂在 urllib3 的连接创建路径上（见 enable_direct），因此只影响
    requests/urllib3 发起的连接，不动进程级的 socket.getaddrinfo。
    """
    try:
        host, port = address
    except (TypeError, ValueError):
        return _create_connection_original(address, *args, **kwargs)
    mapping = get_ip_map()
    if host in mapping:
        address = (mapping[host], port)
    return _create_connection_original(address, *args, **kwargs)


def enable_direct():
    global _patched
    with _patch_lock:
        if _patched:
            return
        # 只有真正启用直连时才抑制告警：该抑制仅针对本模块发起的免 SNI 连接
        # （IP 直连没有域名可用于 SNI，也就无法做证书主机名校验，见
        # DirectAdapter._make_ssl_context），与调用方其它 HTTPS 请求无关。
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        urllib3_connection.create_connection = _patched_create_connection
        _patched = True


class DirectAdapter(HTTPAdapter):
    """HTTPAdapter that connects without TLS SNI and without cert verification."""

    def _make_ssl_context(self):
        # 为什么必须关校验：直连用 IP 连接但 Host 仍是 pixiv 域名，TLS 握手时
        # 不发送 SNI（server_hostname=None）。缺少 SNI 时服务器返回的证书链
        # 不保证匹配所请求域名，且证书主机名校验也无法进行——一旦开启
        # check_hostname/CERT_REQUIRED，握手会直接失败，直连功能即失效。
        # 代价：该会话不再有中间人防护，仅适用于本工具对 pixiv 固定 IP 的
        # 媒体/API 拉取；不要把此 adapter 复用到需要真实校验的场景。
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        orig_wrap = ctx.wrap_socket

        def wrap_without_sni(*args, **kwargs):
            kwargs["server_hostname"] = None
            return orig_wrap(*args, **kwargs)

        ctx.wrap_socket = wrap_without_sni
        return ctx

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._make_ssl_context()
        kwargs["server_hostname"] = None
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._make_ssl_context()
        kwargs["server_hostname"] = None
        return super().proxy_manager_for(*args, **kwargs)


def resolve_ip_via_doh(host, timeout=10):
    """Query DNS-over-HTTPS for A records of a pixiv host (best effort)."""
    for doh_host, doh_ips in DOH_SERVERS:
        for ip in doh_ips:
            try:
                ips = _doh_query(doh_host, ip, host, timeout)
                if ips:
                    return ips
            except Exception:
                continue
    return []


def _doh_query(doh_host, doh_ip, query_host, timeout):
    # 与 DirectAdapter 同理：DoH 服务器用 IP 直连、握手不带 SNI，必须关校验
    # （见 _make_ssl_context 的说明）；代价同样是没有中间人防护。
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    orig_wrap = ctx.wrap_socket

    def wrap_without_sni(*args, **kwargs):
        kwargs["server_hostname"] = None
        return orig_wrap(*args, **kwargs)

    ctx.wrap_socket = wrap_without_sni

    sock = socket.create_connection((doh_ip, 443), timeout=timeout)
    ssock = ctx.wrap_socket(sock, server_hostname=None)
    try:
        request = (
            f"GET /dns-query?name={query_host}&type=A HTTP/1.1\r\n"
            f"Host: {doh_host}\r\n"
            f"Accept: application/dns-json\r\n"
            f"Connection: close\r\n\r\n"
        )
        ssock.sendall(request.encode())
        data = b""
        while True:
            chunk = ssock.recv(4096)
            if not chunk:
                break
            data += chunk
        body = data.split(b"\r\n\r\n", 1)[1]
        result = json.loads(body)
        answers = [
            a["data"]
            for a in result.get("Answer", [])
            if a.get("type") == 1 and a.get("data")
        ]
        return answers
    finally:
        ssock.close()


def _response_is_usable(resp):
    """判断一次探测响应是否说明该 IP 可用（可直连 pixiv 主机）。

    只排除明确的拦截/代理信号，避免误杀正常 Pixiv IP：
    - 无鉴权访问 "/" 时 Pixiv 返回 404 JSON 属正常，仍算可用；
    - 451（法律封禁）/ 407（代理要求认证）→ 被拦截，不可用；
    - 403 且 Content-Type 为 text/html → 典型中间盒拦截页，不可用
      （403 + JSON 则可能是 API 鉴权类响应，不代表 IP 不通，仍算可用）；
    - 其余 < 500 视为可用。
    """
    status = getattr(resp, "status_code", None)
    if not isinstance(status, int):
        return False
    if status >= 500:
        return False
    if status in (451, 407):
        return False
    if status == 403:
        headers = getattr(resp, "headers", None) or {}
        content_type = ""
        for key, value in getattr(headers, "items", lambda: [])():
            if str(key).lower() == "content-type":
                content_type = str(value or "")
                break
        if "text/html" in content_type.lower():
            return False
    return True


def _validate_ip(host, ip, timeout=10):
    """Test if an IP works for SNI-less direct connection to a pixiv host."""
    try:
        from requests import Session
        session = Session()
        adapter = DirectAdapter()
        session.mount("https://", adapter)
        session.verify = False
        session.headers.update({
            "referer": "https://app-api.pixiv.net/",
            "User-Agent": "PixivIOSApp/5.8.0",
        })
        # Temporarily force this host to resolve to the candidate IP.
        # 只在本函数作用域内 patch urllib3 的连接创建函数（不再改全局
        # socket.getaddrinfo）；委托到真正的原始函数，避免与 enable_direct 的
        # IP 映射叠加——否则候选 IP 会被 .env 里的旧 IP 覆盖，探测失去意义。
        original = urllib3_connection.create_connection

        def forced(address, *args, **kwargs):
            try:
                h, port = address
            except (TypeError, ValueError):
                return _create_connection_original(address, *args, **kwargs)
            if h == host:
                address = (ip, port)
            return _create_connection_original(address, *args, **kwargs)

        urllib3_connection.create_connection = forced
        try:
            resp = session.get(f"https://{host}/", timeout=timeout)
            return _response_is_usable(resp)
        finally:
            urllib3_connection.create_connection = original
    except Exception:
        return False


def refresh_ips():
    """Query DoH for each pixiv host, validate, and persist working IPs."""
    updated = {}
    for host in DEFAULT_IP_MAP:
        ips = resolve_ip_via_doh(host)
        for ip in ips:
            if _validate_ip(host, ip):
                updated[ENV_KEYS[host]] = ip
                break
    if updated:
        _write_env(updated)
    return updated
