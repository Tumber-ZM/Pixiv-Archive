import os
import json
import ssl
import socket
import threading
import urllib3
from requests.adapters import HTTPAdapter
from app import paths

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
_getaddrinfo_original = socket.getaddrinfo
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
    result = {}
    path = _env_path()
    if not os.path.exists(path):
        return result
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                result[key.strip()] = value.strip()
    return result


def _write_env(updates):
    path = _env_path()
    existing = _read_env()
    existing.update(updates)
    with open(path, "w", encoding="utf-8") as f:
        for key, value in existing.items():
            f.write(f"{key}={value}\n")


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


def _patched_getaddrinfo(host, *args, **kwargs):
    mapping = get_ip_map()
    if host in mapping:
        host = mapping[host]
    return _getaddrinfo_original(host, *args, **kwargs)


def enable_direct():
    global _patched
    with _patch_lock:
        if _patched:
            return
        socket.getaddrinfo = _patched_getaddrinfo
        _patched = True


class DirectAdapter(HTTPAdapter):
    """HTTPAdapter that connects without TLS SNI and without cert verification."""

    def _make_ssl_context(self):
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
        # Temporarily override resolution to the candidate IP
        original = socket.getaddrinfo
        def forced(hostname, *args, **kwargs):
            if hostname == host:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]
            return original(hostname, *args, **kwargs)
        socket.getaddrinfo = forced
        try:
            resp = session.get(f"https://{host}/", timeout=timeout)
            return resp.status_code < 500
        finally:
            socket.getaddrinfo = original
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
