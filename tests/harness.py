"""回归测试的最小框架：不依赖 pytest 等第三方库。

每个 `test_*.py` 模块约定暴露：

    TITLE = "人类可读的标题"
    NEEDS_SERVER = False | True          # 是否需要真实服务
    def run(t):                          # t 是 Report 实例

运行入口见 `tests/run_all.py`。断言全部在临时文件 / 临时目录 / 临时库上执行，
除只读请求外不触碰真实归档数据。
"""
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)

# 本机沙箱下 fork/exec 子进程可能被拖到 40 秒以上，启动等待要给足余量
SERVER_READY_TIMEOUT = 180.0
DEFAULT_PORT = 7801

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Report:
    """一个测试模块的结果收集器。"""

    def __init__(self, title):
        self.title = title
        self.rows = []          # [(status, name, detail)]
        self.failed = []

    def check(self, name, ok, detail=""):
        ok = bool(ok)
        self.rows.append(("OK" if ok else "FAIL", name, detail))
        if not ok:
            self.failed.append(name)
        return ok

    def skip(self, name, detail=""):
        self.rows.append(("SKIP", name, detail))

    def equal(self, name, got, want):
        return self.check(name, got == want, "实际=%r 期望=%r" % (got, want))

    @property
    def total(self):
        return len(self.rows)

    @property
    def passed(self):
        return sum(1 for status, _, _ in self.rows if status == "OK")


def bootstrap():
    """测试模块的统一初始化：把项目根目录加进 sys.path，并放一个占位 token。

    测试不联网：占位 token 只是让 import 阶段不必依赖真实 `.env`；
    `load_dotenv` 默认不覆盖已存在的环境变量，因此真实 token 不会被用上。
    """
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    os.environ.setdefault("PIXIV_REFRESH_TOKEN", "test-token-not-used")


def request(url, method="GET", body=None, timeout=20):
    """发一个请求并返回 (status, text, headers)；连接失败返回 status=0。"""
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers)
    except Exception as exc:  # 连接被拒 / 超时
        return 0, str(exc), {}


def get(base, path, timeout=20):
    """对一个服务基地址发 GET，返回 (status, text)。"""
    status, text, _headers = request(base + path, timeout=timeout)
    return status, text


def post_json(base, path, payload, timeout=20):
    import json

    body = payload if isinstance(payload, str) else json.dumps(payload)
    status, text, headers = request(base + path, method="POST", body=body, timeout=timeout)
    return status, text, headers


def header(headers, name):
    """HTTP 头名大小写不敏感地取值。"""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return str(value).strip()
    return ""


def load_json(text):
    import json

    try:
        return json.loads(text)
    except Exception:
        return None


class Server:
    """在指定端口上跑一份 run.py，供需要真实服务的测试使用。"""

    def __init__(self, port=DEFAULT_PORT):
        self.port = port
        self.base = "http://127.0.0.1:%d" % port
        self.proc = None
        self.log_path = os.path.join(TESTS_DIR, "_server.log")

    def _other_instance_running(self):
        """数据目录里是否已有**存活**的实例；有就不抢，直接报错退出。"""
        sys.path.insert(0, ROOT)
        import run as run_mod  # noqa: PLC0415

        lock_path = os.path.join(run_mod.paths.DATA_DIR, run_mod.LOCK_FILE_NAME)
        if not os.path.exists(lock_path):
            return None
        try:
            with open(lock_path, encoding="ascii", errors="ignore") as handle:
                raw = handle.read().strip()
        except OSError:
            return None
        if raw.isdigit() and not run_mod._lock_file_is_stale(lock_path):
            return raw
        return None

    def start(self):
        """返回 (ok, message)。"""
        holder = self._other_instance_running()
        if holder:
            return False, ("检测到 Pixiv Archive 已在运行（.instance.lock 属于存活进程 %s）。"
                           "请先关闭它再跑测试，否则本进程会因单实例锁拒绝启动。" % holder)
        log = open(self.log_path, "wb")
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "run.py"), "--port", str(self.port)],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        self._log = log
        deadline = time.time() + SERVER_READY_TIMEOUT
        while time.time() < deadline:
            if get(self.base, "/api/tags", timeout=5)[0] == 200:
                return True, "服务就绪：%s" % self.base
            if self.proc.poll() is not None:
                log.flush()
                tail = open(self.log_path, encoding="utf-8", errors="replace").read()[-2000:]
                return False, "服务进程提前退出（退出码 %s）：\n%s" % (self.proc.returncode, tail)
            time.sleep(0.5)
        return False, "等待服务启动超时（%.0f 秒）" % SERVER_READY_TIMEOUT

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        try:
            self._log.close()
        except Exception:
            pass
        try:
            os.remove(self.log_path)
        except OSError:
            pass


def discover():
    """按文件名顺序加载 tests/test_*.py。"""
    import importlib.util

    modules = []
    for name in sorted(os.listdir(TESTS_DIR)):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        path = os.path.join(TESTS_DIR, name)
        spec = importlib.util.spec_from_file_location(name[:-3], path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name[:-3]] = module
        spec.loader.exec_module(module)
        modules.append(module)
    return modules