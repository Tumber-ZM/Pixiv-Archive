"""Pixiv Archive 桌面应用启动器。

- 后台线程启动内置 ASGI HTTP 服务（端口自动避让 6814+）
- 默认以 pywebview 原生窗口呈现；WebView2 缺失或失败时回退到系统浏览器
- 支持：--browser 强制浏览器模式 / --port 指定端口 / --check 冒烟自检
- 单实例锁：重复启动时提示并退出
- 系统托盘：显示窗口 / 退出
"""
import argparse
import asyncio
import ctypes
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

from app import paths
from dotenv import load_dotenv

# 先加载 .env，使设置里保存的 PA_PORT / PA_HOST / PA_ACCESS_TOKEN 生效
load_dotenv(paths.ENV_FILE)

DEFAULT_PORT = 6814


def _argv_flag(name):
    argv = sys.argv[1:]
    if name in argv:
        return True
    if f"--{name}" in argv:
        return True
    return False


def _argv_value(name):
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == f"--{name}" and i + 1 < len(argv):
            return argv[i + 1]
    return None


# 必须在导入 app.main 之前解析 host，以便主进程按局域网模式生成令牌
_PA_HOST = _argv_value("host") or ("0.0.0.0" if _argv_flag("lan")
                                   else (os.getenv("PA_HOST", "127.0.0.1").strip() or "127.0.0.1"))
if _PA_HOST in ("0.0.0.0", "::", "::0", "*"):
    _PA_HOST = "0.0.0.0"
os.environ["PA_HOST"] = _PA_HOST

_port_env = (os.getenv("PA_PORT", "") or "").strip()
_argv_port = _argv_value("port")
os.environ["PA_PORT"] = _argv_port or _port_env or str(DEFAULT_PORT)

from app.main import app, LAN_MODE, lan_access_url  # noqa: E402

LOG_FILE = os.path.join(paths.DATA_DIR, "pixiv_archive.log")
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("launcher")

MUTEX_NAME = "PixivArchive_SingleInstance"


def _message_box(title, text, flags=0x40):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, flags | 0x40000)
    except Exception:
        pass


def acquire_single_instance():
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        already_running = kernel32.GetLastError() == 183
        if already_running:
            return False
        globals()["_mutex_handle"] = handle
        return True
    except Exception:
        return True


def find_free_port(start=None, tries=20):
    if start is None:
        port_env = (os.getenv("PA_PORT", "") or "").strip()
        start = int(port_env) if port_env.isdigit() else DEFAULT_PORT
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return None


class ServerThread(threading.Thread):
    def __init__(self, port):
        super().__init__(daemon=True, name="http-server")
        self.port = port
        self.error = None
        self.started = threading.Event()

    def run(self):
        try:
            from run import ASGIHTTPServer

            server = ASGIHTTPServer(app, _PA_HOST, self.port)
            self.started.set()
            asyncio.run(server.serve())
        except Exception as e:
            self.error = e
            log.exception("http server error")
        finally:
            self.started.set()


def wait_ready(server, timeout=20.0):
    url = f"http://127.0.0.1:{server.port}/"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server.error:
            return None, server.error
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return url, None
        except Exception:
            pass
        time.sleep(0.2)
    return None, TimeoutError(f"服务在 {timeout} 秒内未就绪（端口 {server.port}）")


class Api:
    """暴露给前端 JS 的原生能力。"""

    def open_external(self, url):
        try:
            webbrowser.open(url)
            return True
        except Exception:
            return False


def _webview2_available():
    """检查系统是否安装了 WebView2 Runtime（EdgeChromium 需要）。"""
    try:
        import winreg
    except Exception:
        return False
    clients = (
        r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    )
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for sub in clients:
            try:
                with winreg.OpenKey(hive, sub) as k:
                    winreg.QueryValueEx(k, "pv")
                    return True
            except OSError:
                continue
    return False


def _make_tray_icon():
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=16, fill=(59, 158, 255, 255))
    try:
        font = ImageFont.truetype("arialbd.ttf", 42)
    except Exception:
        try:
            font = ImageFont.truetype("arial.ttf", 42)
        except Exception:
            font = ImageFont.load_default()
    d.text((17, 8), "P", font=font, fill=(255, 255, 255, 255))
    return img


def start_tray(window_holder, quit_event):
    try:
        import pystray
    except Exception:
        log.warning("pystray unavailable, tray disabled")
        return None

    def on_show(icon, item):
        win = window_holder.get("win")
        if win is not None:
            try:
                win.show()
                win.focus()
            except Exception:
                pass
        else:
            # 浏览器模式：重新打开系统默认浏览器
            url = window_holder.get("url") or ""
            if url:
                try:
                    webbrowser.open(url)
                except Exception:
                    pass

    def on_quit(icon, item):
        # 先置位退出事件，再销毁窗口/停止托盘：
        # pystray 的菜单回调运行在托盘线程上，此时调用 icon.stop()
        # 在某些后端会阻塞/不返回，导致主循环永远无法退出、进程残留。
        quit_event.set()
        win = window_holder.get("win")
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        try:
            icon.stop()
        except Exception:
            pass

    icon = pystray.Icon(
        "PixivArchive",
        _make_tray_icon(),
        "Pixiv Archive",
        menu=pystray.Menu(
            pystray.MenuItem("显示窗口", on_show, default=True),
            pystray.MenuItem("退出", on_quit),
        ),
    )
    t = threading.Thread(target=icon.run, name="tray", daemon=True)
    t.start()
    return icon


def run_check(args):
    port = args.port or find_free_port()
    if port is None:
        return 1
    server = ServerThread(port)
    server.start()
    url, err = wait_ready(server, timeout=15)
    ok = bool(url) and err is None
    if ok:
        try:
            with urllib.request.urlopen(url + "api/artworks", timeout=5) as resp:
                ok = resp.status == 200
        except Exception:
            ok = False
    marker = os.path.join(paths.DATA_DIR, "pixiv_archive_check.txt")
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(f"{'OK' if ok else 'FAIL'}\nurl={url}\nerror={err}\n")
    except Exception as e:
        print(f"write marker failed: {e}", file=sys.stderr)
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(description="Pixiv Archive launcher")
    parser.add_argument("--browser", action="store_true",
                        help="用系统浏览器打开而不是桌面窗口")
    parser.add_argument("--port", type=int, default=0, help="指定端口（默认从 PA_PORT / 6814 起自动避让）")
    parser.add_argument("--check", action="store_true", help="冒烟自检后退出")
    args = parser.parse_args()

    log.info("launcher start, frozen=%s, data_dir=%s", getattr(sys, "frozen", False), paths.DATA_DIR)

    if args.check:
        sys.exit(run_check(args))

    if not acquire_single_instance():
        _message_box("Pixiv Archive", "应用已在运行中。\n如无响应，请结束现有进程后重试。", 0x40)
        return 1

    port = args.port or find_free_port()
    if port is None:
        _message_box("Pixiv Archive", "无法找到可用端口（默认 6814 起的连续端口均被占用）。\n请关闭占用端口的程序，或在设置中改用其它端口后重试。", 0x10)
        return 1

    server = ServerThread(port)
    server.start()
    url, err = wait_ready(server)
    if err or not url:
        _message_box("Pixiv Archive", f"服务启动失败：\n{err}\n\n日志：{LOG_FILE}", 0x10)
        return 1

    if LAN_MODE:
        lan_url = lan_access_url()
        log.info("LAN mode enabled: %s", lan_url)
        _message_box(
            "Pixiv Archive — 局域网已开放",
            f"局域网访问地址：\n{lan_url}\n\n"
            "访问令牌已生效（可在 设置 中自定义）。请保留 URL 末尾的 ?token= 参数分享给其它设备。\n\n"
            "警告：局域网内任何持有该地址/令牌的设备都可查看和管理你的图库！",
            0x40,
        )

    browser_mode = args.browser or os.environ.get("PA_BROWSER") == "1"
    quit_event = threading.Event()
    window_holder = {"win": None, "url": url}

    if not browser_mode and _webview2_available():
        try:
            import webview

            window = webview.create_window(
                "Pixiv Archive",
                url,
                width=1400,
                height=900,
                min_size=(960, 600),
                background_color="#101218",
                js_api=Api(),
            )
            window_holder["win"] = window
            start_tray(window_holder, quit_event)
            log.info("webview window opened at %s", url)
            webview.start(debug=False)
        except Exception as e:
            log.exception("webview failed, fallback to browser")
            browser_mode = True

    if browser_mode:
        log.info("browser mode at %s", url)
        tray_icon = start_tray(window_holder, quit_event)
        webbrowser.open(url)
        try:
            while not quit_event.wait(1.0):
                pass
        except KeyboardInterrupt:
            quit_event.set()
        if tray_icon is not None:
            try:
                tray_icon.stop()
            except Exception:
                pass

    log.info("launcher exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
