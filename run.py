import argparse
import os
import sys
import asyncio
import traceback
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import paths  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

# 先加载 .env，使设置里保存的 PA_PORT / PA_HOST / PA_ACCESS_TOKEN 生效
load_dotenv(paths.ENV_FILE)

DEFAULT_PORT = 6814


def _parse_server_args():
    p = argparse.ArgumentParser(description="Pixiv Archive 本地服务")
    p.add_argument("--host", default="", help="绑定地址（默认 127.0.0.1）")
    p.add_argument("--lan", action="store_true", help="开放局域网访问（绑定 0.0.0.0，自动生成访问令牌）")
    p.add_argument("--port", type=int, default=0, help=f"端口（默认 {DEFAULT_PORT}，也可用设置/环境变量 PA_PORT）")
    a, _ = p.parse_known_args()
    host = a.host or ("0.0.0.0" if a.lan else os.getenv("PA_HOST", "127.0.0.1"))
    if host in ("0.0.0.0", "::", "::0", "*"):
        host = "0.0.0.0"
    port_str = (os.getenv("PA_PORT", "") or "").strip()
    port = a.port if a.port else (int(port_str) if port_str.isdigit() else DEFAULT_PORT)
    os.environ["PA_HOST"] = host
    os.environ["PA_PORT"] = str(port)
    return host, port


HOST, PORT = _parse_server_args()

from app.main import app, LAN_MODE, lan_access_url  # noqa: E402


class ASGIHTTPServer:
    def __init__(self, asgi_app, host="127.0.0.1", port=8000):
        self.asgi_app = asgi_app
        self.host = host
        self.port = port

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

            async def receive():
                nonlocal body
                if body:
                    b = body
                    body = b""
                    return {"type": "http.request", "body": b, "more_body": False}
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(event):
                nonlocal response_status, response_headers, response_body
                if event["type"] == "http.response.start":
                    response_status = event["status"]
                    response_headers = event.get("headers", [])
                elif event["type"] == "http.response.body":
                    response_body.extend(event.get("body", b""))

            await self.asgi_app(scope, receive, send)

            status_text = {200: "OK", 404: "Not Found", 500: "Internal Server Error"}.get(
                response_status, "Unknown"
            )
            resp_line = f"HTTP/1.1 {response_status} {status_text}\r\n"
            writer.write(resp_line.encode())

            for key, value in response_headers:
                writer.write(key + b": " + value + b"\r\n")
            writer.write(b"\r\n")
            writer.write(bytes(response_body))
            await writer.drain()
        except Exception:
            traceback.print_exc()
        finally:
            writer.close()

    async def serve(self):
        server = await asyncio.start_server(self.handle, self.host, self.port)
        addr = server.sockets[0].getsockname()
        print(f"Pixiv Archive running at http://{addr[0]}:{addr[1]}")
        if LAN_MODE:
            print()
            print("=" * 52)
            print("局域网访问已开启（LAN mode）")
            print(f"  访问地址: {lan_access_url()}")
            print("  访问令牌请在 URL 末尾保留 ?token= 参数（可在设置中自定义令牌）")
            print("  警告: 局域网内任何持有令牌的设备都能查看/管理图库！")
            print("=" * 52)
            print()
        async with server:
            await server.serve_forever()


def main():
    server = ASGIHTTPServer(app, HOST, PORT)
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        print("Server stopped")


if __name__ == "__main__":
    main()