"""Pixiv Archive 本地服务入口。

- 只绑定 127.0.0.1，不开放局域网访问（LAN 模式已移除）。
- 单实例：同一时间只允许一个实例运行（数据目录下的独占锁文件 + PID 存活检测），
  因此 Start.bat 重复双击不会起第二个服务、也不会抢端口。
- --browser / PA_BROWSER=1：启动后自动用系统默认浏览器打开页面（exe 分发版用）。
"""
import argparse
import asyncio
import os
import sys
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import paths  # noqa: E402
from app.config import DEFAULT_PORT  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

# 先加载 .env，使设置里保存的 PA_PORT 生效
load_dotenv(paths.ENV_FILE)

HOST = "127.0.0.1"
LOCK_FILE_NAME = ".instance.lock"
# 创建锁文件与写入 PID 之间被强杀时可能留下空文件，超过这个秒数即视为陈旧
LOCK_FILE_GRACE_SECONDS = 10


def _pid_is_alive(pid):
    """尽力判断 pid 是否仍存活（仅 Windows；判不出来时按「存活」保守处理）。"""
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            # 打不开时区分两种情况：ERROR_INVALID_PARAMETER(87) 表示该 PID 不存在；
            # 其它错误（如拒绝访问）说明进程还在只是查不了，按「存活」处理更安全，
            # 否则会误抢一个正在运行实例的锁。
            return ctypes.get_last_error() != 87
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True


def _lock_file_is_stale(lock_path):
    """锁文件存在：判断持有者是否已经不在（陈旧锁可被接管）。"""
    try:
        with open(lock_path, "r", encoding="ascii", errors="ignore") as f:
            raw = (f.read() or "").strip()
    except OSError:
        return False
    if raw.isdigit() and int(raw) > 0:
        return not _pid_is_alive(int(raw))
    try:
        return (time.time() - os.path.getmtime(lock_path)) > LOCK_FILE_GRACE_SECONDS
    except OSError:
        return False


def acquire_single_instance():
    """获取单实例锁。

    返回锁文件路径（需在进程存活期间保持引用）；已有实例在运行时返回 None；
    无法判定时返回 True，表示不阻塞启动。

    这里用「独占锁文件 + PID 存活检测」，而不是 Windows 命名互斥体：实测某环境下
    CreateMutexW("PixivArchive_SingleInstance") 会返回 NULL 且 GetLastError()=5
    （ERROR_ACCESS_DENIED）——同名内核对象已存在却不属于当前安全上下文；而原来的
    判断只认 ERROR_ALREADY_EXISTS(183)，于是「创建失败」被当成「拿到锁」，单实例
    保护形同虚设，会同时跑起两个服务、并发写同一个 SQLite 库。
    锁文件不依赖内核对象命名空间，且上次异常退出留下的陈旧锁能靠 PID 检测清掉。
    """
    lock_path = os.path.join(paths.DATA_DIR, LOCK_FILE_NAME)
    try:
        for attempt in (0, 1):
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                # 只在第一次尝试接管陈旧锁；第二次仍冲突说明确实有实例在跑
                if attempt or not _lock_file_is_stale(lock_path):
                    return None
                try:
                    os.remove(lock_path)
                except OSError:
                    return None
                continue
            except OSError:
                return True  # 数据目录不可写等：不阻塞启动
            try:
                os.write(fd, str(os.getpid()).encode("ascii"))
            finally:
                os.close(fd)
            return lock_path
        return None
    except Exception:
        return True


def release_single_instance(lock):
    """退出时清理锁文件（仅当确实是自己创建的那个路径）。"""
    if isinstance(lock, str):
        try:
            os.remove(lock)
        except OSError:
            pass


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

    try:
        server = ASGIHTTPServer(app, HOST, PORT)
        try:
            asyncio.run(server.serve())
        except KeyboardInterrupt:
            print("Server stopped")
        return 0
    finally:
        release_single_instance(lock)


if __name__ == "__main__":
    sys.exit(main())