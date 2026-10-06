"""P1-5：DoH 查询的 TLS 校验（信任根必须被认证）。

DoH 应答决定「pixiv 域名 → 哪个 IP」，直连模式下这就是 HTTPS 的信任根。原实现
用 IP 直连 + 不发 SNI + `CERT_NONE`，等于完全信任应答 —— 中间人可把 pixiv 钉到
攻击者 IP。现要求：

1. 首选「发 SNI + `check_hostname` + `CERT_REQUIRED`」；
2. SNI 被阻断（握手失败）时**重连**并退回「不发 SNI + `CERT_REQUIRED` + SAN 精确
   匹配主机名」——只验链不验名不够，攻击者拿任意可信证书就能冒充；
3. 两条路都**绝不**接受 `CERT_NONE` 或主机名不匹配的证书。

全部离线：假 socket + 假 SSLContext，不联网、不起服务。
"""
import contextlib
import os
import ssl

import harness as h

h.bootstrap()
from app import direct_connect  # noqa: E402

TITLE = "P1-5 DoH 查询 TLS 校验（SNI 优先 / 免 SNI 回退 + 主机名匹配）"
NEEDS_SERVER = False


def _read(*parts):
    with open(os.path.join(h.ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


class _FakeRawSock:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeSSLSock:
    def __init__(self, payload, cert):
        self._buf = bytearray(payload)
        self._cert = cert
        self.sent = b""
        self.closed = False

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        if not self._buf:
            return b""
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def getpeercert(self):
        return self._cert

    def close(self):
        self.closed = True


class _FakeCtx:
    """记录每次握手的校验参数；按 sni_fails 决定发 SNI 的那次握手是否失败。"""

    def __init__(self, record, made, sni_fails, payload, cert):
        self._record = record
        self._made = made
        self._sni_fails = sni_fails
        self._payload = payload
        self._cert = cert
        self.check_hostname = False
        self.verify_mode = ssl.CERT_NONE

    def wrap_socket(self, sock, server_hostname=None):
        self._record.append({
            "check_hostname": self.check_hostname,
            "verify_mode": self.verify_mode,
            "server_hostname": server_hostname,
        })
        if server_hostname is not None and self._sni_fails:
            raise ssl.SSLError("模拟：SNI 被阻断")
        ssock = _FakeSSLSock(self._payload, self._cert)
        self._made.append(ssock)
        return ssock


def _cert_for(*hosts):
    return {"subjectAltName": tuple(("DNS", host) for host in hosts)}


def _doh_response(ips):
    answers = ",".join('{"name":"x","type":1,"data":"%s"}' % ip for ip in ips)
    body = '{"Status":0,"Answer":[%s]}' % answers
    return ("HTTP/1.1 200 OK\r\nContent-Type: application/dns-json\r\n\r\n" + body).encode()


@contextlib.contextmanager
def _patched_doh(payload, cert, sni_fails=False):
    record, made, conns = [], [], []
    real_conn = direct_connect.socket.create_connection
    real_ctx = direct_connect.ssl.create_default_context

    def fake_conn(address, timeout=None):
        conns.append(address)
        return _FakeRawSock()

    def fake_ctx():
        return _FakeCtx(record, made, sni_fails, payload, cert)

    direct_connect.socket.create_connection = fake_conn
    direct_connect.ssl.create_default_context = fake_ctx
    try:
        yield record, made, conns
    finally:
        direct_connect.socket.create_connection = real_conn
        direct_connect.ssl.create_default_context = real_ctx


HOST = "cloudflare-dns.com"
IP = "104.16.248.249"


def _san_matcher(t):
    cert = _cert_for("a.com", "*.a.com")
    t.check("P1-5 SAN 精确匹配通过",
            direct_connect._doh_san_matches(cert, "a.com") is True)
    t.check("P1-5 SAN 匹配大小写不敏感",
            direct_connect._doh_san_matches(_cert_for("A.com"), "a.com") is True)
    t.check("P1-5 刻意不支持通配符（子域不匹配）",
            direct_connect._doh_san_matches(cert, "x.a.com") is False)
    t.check("P1-5 证书为 None 时返回 False",
            direct_connect._doh_san_matches(None, "a.com") is False)
    t.check("P1-5 证书无 SAN 时返回 False",
            direct_connect._doh_san_matches({}, "a.com") is False)
    t.check("P1-5 SAN 条目畸形不抛异常",
            direct_connect._doh_san_matches({"subjectAltName": ("oops",)}, "a.com") is False)


def _sni_path(t):
    with _patched_doh(_doh_response(["1.2.3.4"]), _cert_for(HOST)) as (record, made, conns):
        answers = direct_connect._doh_query(HOST, IP, "i.pximg.net", 5)

    t.equal("P1-5 正常路径返回解析结果", answers, ["1.2.3.4"])
    t.equal("P1-5 正常路径只建立一次连接", len(conns), 1)
    t.equal("P1-5 连接目标是 DoH 的 443", conns[0], (IP, 443))
    t.check("P1-5 正常路径发送 SNI（server_hostname=DoH 主机名）",
            record[0]["server_hostname"] == HOST, "record=%r" % (record,))
    t.check("P1-5 正常路径开启主机名校验", record[0]["check_hostname"] is True)
    t.equal("P1-5 正常路径要求证书链校验", record[0]["verify_mode"], ssl.CERT_REQUIRED)
    t.check("P1-5 请求携带 DoH 主机名（Host 头）",
            ("Host: %s" % HOST).encode() in made[0].sent, "sent=%r" % made[0].sent[:120])
    t.check("P1-5 用完关闭连接", made[0].closed is True)


def _fallback_path(t):
    with _patched_doh(_doh_response(["5.6.7.8"]), _cert_for(HOST), sni_fails=True) as (record, made, conns):
        answers = direct_connect._doh_query(HOST, IP, "i.pximg.net", 5)

    t.equal("P1-5 SNI 被阻断后回退仍能取到解析结果", answers, ["5.6.7.8"])
    t.equal("P1-5 回退时重连（共两次连接）", len(conns), 2)
    t.equal("P1-5 首次握手确实发了 SNI", record[0]["server_hostname"], HOST)
    t.check("P1-5 回退路径不发 SNI", record[1]["server_hostname"] is None,
            "record=%r" % (record,))
    t.check("P1-5 回退路径关闭自动主机名校验（改为手工 SAN 比对）",
            record[1]["check_hostname"] is False)
    t.equal("P1-5 回退路径仍要求可信 CA 链", record[1]["verify_mode"], ssl.CERT_REQUIRED)
    t.check("P1-5 两条路径都不接受 CERT_NONE",
            all(item["verify_mode"] == ssl.CERT_REQUIRED for item in record),
            "record=%r" % (record,))


def _fallback_rejects_wrong_host(t):
    # 回退路径拿到的是「别的域名」的证书（链可能有效，但主机名不对）→ 必须拒绝
    with _patched_doh(_doh_response(["6.6.6.6"]), _cert_for("evil.example"), sni_fails=True) as (record, made, _conns):
        raised = None
        try:
            direct_connect._doh_query(HOST, IP, "i.pximg.net", 5)
        except Exception as exc:  # noqa: BLE001 - 测试就是要捕获任意异常
            raised = exc

    t.check("P1-5 回退时证书主机名不匹配必须拒绝",
            isinstance(raised, ssl.SSLCertVerificationError), "raised=%r" % (raised,))
    t.check("P1-5 被拒的握手连接被关闭",
            bool(made) and all(ssock.closed for ssock in made), "made=%r" % (made,))


def _resolve_fails_closed(t):
    # 校验全失败时，resolve_ip_via_doh 必须返回空 —— 绝不采用未认证的应答
    with _patched_doh(_doh_response(["6.6.6.6"]), _cert_for("evil.example"), sni_fails=True) as (record, _made, conns):
        result = direct_connect.resolve_ip_via_doh("i.pximg.net", timeout=1)

    t.equal("P1-5 校验失败时 resolve_ip_via_doh 返回空（不采用未认证应答）", result, [])
    t.check("P1-5 每个 DoH 服务器都被尝试过（2 主机 × 2 IP）", len(conns) == 8,
            "conns=%d" % len(conns))
    t.check("P1-5 所有尝试都带完整校验",
            all(item["verify_mode"] == ssl.CERT_REQUIRED for item in record))


def _no_plaintext_fallback(t):
    # 回退路径必须「先重连再握手」，不能拿失败的 socket 复用
    with _patched_doh(_doh_response(["9.9.9.9"]), _cert_for(HOST), sni_fails=True) as (record, _made, conns):
        direct_connect._doh_query(HOST, IP, "i.pximg.net", 5)
    t.check("P1-5 回退握手前重新建连（不复用已失败的 socket）",
            len(conns) == 2 and conns[0] == conns[1] == (IP, 443),
            "conns=%r" % (conns,))


def _doh_region(src):
    """截出 `_doh_connect` 到 `refresh_ips` 之间的代码段。

    找不到 `_doh_connect`（例如被回退成旧实现）时退化为整文件 —— 旧实现必然带
    `CERT_NONE`，断言仍会变红，且不会因为切片越界把整个模块打断。
    """
    if "def _doh_connect" not in src:
        return src
    return src.split("def _doh_connect")[1].split("def refresh_ips")[0]


def run(t):
    src = _read("app", "direct_connect.py")
    t.check("P1-5 DoH 查询经 _doh_connect 统一做 TLS 校验",
            "def _doh_connect(" in src and "_doh_connect(doh_host, doh_ip, timeout)" in src)
    t.check("P1-5 DoH 查询不再自带 CERT_NONE 上下文",
            "verify_mode = ssl.CERT_NONE" not in _doh_region(src))

    _san_matcher(t)
    _sni_path(t)
    _fallback_path(t)
    _fallback_rejects_wrong_host(t)
    _resolve_fails_closed(t)
    _no_plaintext_fallback(t)
