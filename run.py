"""Pixiv Archive 本地服务入口。

- 只绑定 127.0.0.1，不开放局域网访问（LAN 模式已移除）。
- 单实例：同一时间只允许一个实例运行（Windows 命名互斥体），
  因此 Start.bat 重复双击不会起第二个服务、也不会抢端口。
- --browser / PA_BROWSER=1：启动后自动用系统默认浏览器打开页面（exe 分发版用）。
"""
import argparse
import asyncio
import os
import sys
import threading
import traceback
import webbrowser
from http import HTTPStatus
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import paths  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

# 先加载 .env，使设置里保存的 PA_PORT 生效
load_dotenv(paths.ENV_FILE)

DEFAULT_PORT = 6814
HOST = "127.0.0.1"
MUTEX_NAME = "PixivArchive_SingleInstance"


def acquire_single_instance():
    """获取单实例锁。

    返回互斥体句柄（需在进程存活期间保持引用）；已有实例在运行时返回 None；
    非 Windows 或调用失败时返回 True，表示不阻塞启动。
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return None
        return handle
    except Exception:
        return True


def _parse_server_args():
    p = argparse.ArgumentParser(description="Pixiv Archive 本地服务")
    p.add_argument("--port", type=int, default=0,
                   help=f"端口（默认 {DEFAULT_PORT}，也可用设置/环境变量 PA_PORT）")
    p.add_argument("--browser", action="store_true",
                   help="启动后用系统默认浏览器打开页面")
    a, _ = p.parse_known_args()
    port_str = (os.getenv("PA_PORT", "") or "").strip()
    port = a.port if a.port else (int(port_str) if port_str.isdigit() else DEFAULT_PORT)
    os.environ["PA_PORT"] = str(port)
    open_browser = a.browser or os.environ.get("PA_BROWSER") == "1"
    return port, open_browser


PORT, OPEN_BROWSER = _parse_server_args()

from app.main import app  # noqa: E402


def _status_text(code):
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return "Unknown"


class ASGIHTTPServer:
    def __init__(self, asgi_app, host="127.0.0.1", port=8000):
        self.asgi_app = asgi_app
        self.host = host
        self.port = port

    @staticmethod
    async def _write_response(writer, status, headers, body):
        """按 HTTP/1.1 写出响应，缺 Content-Length 时补上。"""
        has_length = any(key.lower() == b"content-length" for key, _ in headers)
        head = [f"HTTP/1.1 {status} {_status_text(status)}\r\n".encode()]
        for key, value in headers:
            head.append(key + b": " + value + b"\r\n")
        if not has_length:
            head.append(b"Content-Length: " + str(len(body)).encode() + b"\r\n")
        head.append(b"\r\n")
        writer.write(b"".join(head))
        writer.write(bytes(body))
        await writer.drain()

    async def handle(self, reader, writer):
        try:
            request_data = await reader.readuntil(b"\r\n\r\n")
            request_text = request_data.decode("utf-8", errors="replace")
            lines = request_text.split("\r\n")
            request_line = lines[0].split(" ")

            if len(request_line) < 2:
                writer.close()
                return

            method = request_line[0].upper()
            path = request_line[1]
            parsed = urlparse(path)
            path_only = parsed.path
            query_string = parsed.query.encode()

            headers = []
            content_length = 0
            for line in lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    key = key.strip().lower()
                    value = value.strip()
                    headers.append((key.encode(), value.encode()))
                    if key == "content-length":
                        content_length = int(value)

            body = b""
            if content_length > 0:
                body = await reader.readexactly(content_length)

            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.1"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": path_only,
                "raw_path": path_only.encode(),
                "query_string": query_string,
                "headers": headers,
                "client": writer.get_extra_info("peername") or ("127.0.0.1", 0),
                "server": (self.host, self.port),
            }

            response_status = 500
            response_headers = []
            response_body = bytearray()
            response_started = False

            async def receive():
                nonlocal body
                if body:
                    b = body
                    body = b""
                    return {"type": "http.request", "body": b, "more_body": False}
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(event):
                nonlocal response_status, response_headers, response_body, response_started
                if event["type"] == "http.response.start":
                    response_status = event["status"]
                    response_headers = event.get("headers", [])
                    response_started = True
                elif event["type"] == "http.response.body":
                    response_body.extend(event.get("body", b""))

            try:
                await self.asgi_app(scope, receive, send)
            except Exception:
                # 应用层仍有未处理异常时也必须回一个响应，绝不静默断开连接：
                # FastAPI 的 500 处理器会先生成响应再把异常抛出来，那种情况下把
                # 已生成的响应发回；否则补一个最小 500。
                traceback.print_exc()
                if response_started:
                    response_headers = [
                        (k, v) for (k, v) in response_headers
                        if k.lower() != b"content-length"
                    ]
                else:
                    response_status = 500
                    response_headers = [(b"content-type", b"text/plain; charset=utf-8")]
                    response_body = bytearray(b"Internal Server Error")

            await self._write_response(writer, response_status, response_headers, response_body)
        except Exception:
            traceback.print_exc()
        finally:
            writer.close()

    async def serve(self):
        server = await asyncio.start_server(self.handle, self.host, self.port)
        addr = server.sockets[0].getsockname()
        url = f"http://{addr[0]}:{addr[1]}/"
        print(f"Pixiv Archive running at {url}")
        if OPEN_BROWSER:
            threading.Timer(1.0, webbrowser.open, args=[url]).start()
        async with server:
            await server.serve_forever()


def main():
    lock = acquire_single_instance()
    if lock is None:
        print("Pixiv Archive 已在运行中，请勿重复启动。")
        print("如无响应，请先结束现有进程后重试。")
        return 1

    server = ASGIHTTPServer(app, HOST, PORT)
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        print("Server stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())