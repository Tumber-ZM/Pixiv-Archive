"""Pixiv Archive 本地服务入口。

- 只绑定 127.0.0.1，不开放局域网访问（LAN 模式已移除）。
- HTTP 服务交给 hypercorn（ASGI 服务器），不再自带手写实现：keep-alive、流式响应
  与连接错误都由它负责处理。应用层仍未处理的异常由 Starlette 的 500 中间件兜底成
  一个响应再抛出，因此不会出现「静默断开连接」。
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
import uuid
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import paths  # noqa: E402
from app import logging_setup  # noqa: E402
from app.config import DEFAULT_PORT  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

# 先加载 .env，使设置里保存的 PA_PORT 生效
load_dotenv(paths.ENV_FILE)

logger = logging_setup.get_logger("pixiv_archive")

HOST = "127.0.0.1"
LOCK_FILE_NAME = ".instance.lock"
# 创建锁文件与写入 PID 之间被强杀时可能留下空文件，超过这个秒数即视为陈旧
LOCK_FILE_GRACE_SECONDS = 10
# 接管临时文件（.stale.*）的清理宽限期：必须远大于一次接管的耗时（微秒级），
# 否则可能误删并发实例正在进行的接管
STALE_LOCK_GRACE_SECONDS = 60


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


def _lock_content(lock_path):
    """读取锁文件内容；不存在 / 读不到时返回 None。"""
    try:
        with open(lock_path, "r", encoding="ascii", errors="ignore") as f:
            return (f.read() or "").strip()
    except OSError:
        return None


def _raw_is_stale(raw, lock_path):
    """根据已读到的内容判断锁是否陈旧（持有者已不在）。"""
    if raw.isdigit() and int(raw) > 0:
        return not _pid_is_alive(int(raw))
    try:
        return (time.time() - os.path.getmtime(lock_path)) > LOCK_FILE_GRACE_SECONDS
    except OSError:
        return False


def _lock_file_is_stale(lock_path):
    """锁文件存在：判断持有者是否已经不在（陈旧锁可被接管）。"""
    raw = _lock_content(lock_path)
    if raw is None:
        return False
    return _raw_is_stale(raw, lock_path)


def _sweep_abandoned_stale(lock_path):
    """清理先前被强杀留下的 `.stale.*` 接管临时文件。

    接管路径是「rename 到 `.stale.<pid>.<uuid>` → 复核内容 → 删除」。若进程在
    rename 与删除之间被强杀（Windows 上 TerminateProcess 不会执行 finally），该
    文件会永久残留。这里在**成功持有锁之后**顺手清掉超过宽限期的遗留文件；宽限期
    保证不会误删并发实例正在进行的接管（那只是微秒级窗口）。
    """
    directory = os.path.dirname(lock_path) or "."
    prefix = os.path.basename(lock_path) + ".stale."
    now = time.time()
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.startswith(prefix):
            continue
        path = os.path.join(directory, name)
        try:
            if now - os.path.getmtime(path) < STALE_LOCK_GRACE_SECONDS:
                continue
            os.remove(path)
        except OSError:
            pass


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
        for attempt in (0, 1, 2):
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                # 前两次尝试接管陈旧锁；仍冲突说明确实有实例在跑
                if attempt == 2:
                    return None
                raw = _lock_content(lock_path)
                if raw is None:
                    # 锁文件存在却读不出内容，通常是「读」这一下被打断（杀软扫描、
                    # 另一实例正在写），而不是锁已被释放。此时若直接 continue，下一次
                    # O_EXCL 必然又撞上同一个文件、把这次接管机会白白耗光——两个实例
                    # 同时进来就会「谁都没拿到锁」。确认文件还在就稍等后重读一次。
                    if os.path.exists(lock_path):
                        time.sleep(0.05)
                        raw = _lock_content(lock_path)
                    if raw is None:
                        continue  # 确实读不到（或已被释放）：交给下一轮
                if not _raw_is_stale(raw, lock_path):
                    return None
                # 原子接管：把锁 rename 到本进程独占的名字。rename 是原子操作，同一
                # 把锁只有一个进程能移走，避免「remove + 重建」之间两个实例都以为
                # 拿到了锁。移走后**复核内容**：确认移走的确实是刚才判定为陈旧的
                # 那把。若内容已变（判陈旧之后、rename 之前，别的实例已接管并写入
                # 了自己的活 PID），说明抢到的是别人的锁 —— 放回原位并放弃。
                stale_path = "%s.stale.%d.%s" % (lock_path, os.getpid(), uuid.uuid4().hex)
                try:
                    os.rename(lock_path, stale_path)
                except OSError:
                    # 别人先一步接管了（或锁已被正常释放）：不抢，退出
                    return None
                if _lock_content(stale_path) != raw:
                    # 移走的不是刚才那把陈旧锁（判陈旧之后被别人换成了活锁）：
                    # 放回原位并放弃；放不回（原位已被别人重建）就留着不管。
                    try:
                        os.rename(stale_path, lock_path)
                    except OSError:
                        pass
                    return None
                try:
                    os.remove(stale_path)
                except OSError:
                    pass
                logger.info("接管陈旧单实例锁：原持有者 PID=%s 已不在", raw)
                continue
            except OSError:
                return True  # 数据目录不可写等：不阻塞启动
            try:
                os.write(fd, str(os.getpid()).encode("ascii"))
            finally:
                os.close(fd)
            _sweep_abandoned_stale(lock_path)
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

from app.main import asgi_app  # noqa: E402
from hypercorn.asyncio import serve  # noqa: E402
from hypercorn.config import Config  # noqa: E402


def server_url(host=HOST, port=None):
    """本机访问地址；port 省略时用本次启动解析出的端口。"""
    return "http://%s:%d/" % (host, PORT if port is None else port)


def build_config(host=HOST, port=None):
    """hypercorn 配置：只监听本机地址，默认不输出请求日志（保持控制台干净）。"""
    config = Config()
    config.bind = ["%s:%d" % (host, PORT if port is None else port)]
    config.accesslog = None
    config.graceful_timeout = 5.0
    return config


async def _serve(host=HOST, port=None):
    url = server_url(host, port)
    print("Pixiv Archive running at " + url, flush=True)
    if OPEN_BROWSER:
        threading.Timer(1.0, webbrowser.open, args=[url]).start()
    await serve(asgi_app, build_config(host, port))


def main():
    log_path = logging_setup.setup_logging()
    logger.info("启动 Pixiv Archive（数据目录 %s，日志 %s）", paths.DATA_DIR, log_path)

    lock = acquire_single_instance()
    if lock is None:
        logger.warning("检测到已有实例在运行，拒绝重复启动")
        print("Pixiv Archive 已在运行中，请勿重复启动。")
        print("如无响应，请先结束现有进程后重试。")
        return 1

    try:
        try:
            asyncio.run(_serve())
        except KeyboardInterrupt:
            print("Server stopped")
            logger.info("收到中断信号，服务停止")
        except OSError as exc:
            # 端口被占用等：给一句可读的提示，而不是抛一串 traceback
            logger.error("无法监听 %s:%d —— %s", HOST, PORT, exc)
            print("启动失败：无法监听 %s:%d —— %s" % (HOST, PORT, exc))
            print("端口可能已被占用：可用 --port 指定其它端口，或在设置里改 PA_PORT。")
            return 1
        return 0
    finally:
        release_single_instance(lock)
        logger.info("已退出并释放单实例锁")


if __name__ == "__main__":
    sys.exit(main())