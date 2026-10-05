"""P3 纵深防御与卫生：

- 安全响应头（nosniff / X-Frame-Options / Referrer-Policy / CSP）
- getaddrinfo patch 收窄到 urllib3 连接创建路径（不再动全局 socket）
- 扫描 / 同步分批提交（不再整任务独占写锁）
- 单实例锁接管原子化（不留 .stale 临时文件）
- 依赖锁 requirements.lock + 移除 --trusted-host

真实服务上的安全响应头断言见 `test_http_smoke.py`。
"""
import os
import re
import socket
import tempfile

import urllib3.util.connection as urllib3_connection

import harness as h

h.bootstrap()
from app import database  # noqa: E402
from app import direct_connect  # noqa: E402
from app import main as app_main  # noqa: E402
from app import paths  # noqa: E402
from app import scanner  # noqa: E402
from app import sync  # noqa: E402
import run as run_mod  # noqa: E402

TITLE = "P3 纵深防御与卫生"
NEEDS_SERVER = False

DEAD_PID = 15000000


def run(t):
    _security_headers(t)
    _direct_patch(t)
    _scanner_batching(t)
    _sync_batching(t)
    _lock_atomic(t)
    _deps_lock(t)
    _harness_integrity(t)


# ---------------- 安全响应头 ----------------

def _security_headers(t):
    headers = app_main._SECURITY_HEADERS
    t.equal("P3 响应头含 X-Content-Type-Options", headers.get("X-Content-Type-Options"), "nosniff")
    t.equal("P3 响应头含 X-Frame-Options", headers.get("X-Frame-Options"), "DENY")
    t.equal("P3 响应头含 Referrer-Policy", headers.get("Referrer-Policy"), "no-referrer")
    csp = headers.get("Content-Security-Policy", "")
    t.check("P3 CSP 默认同源", "default-src 'self'" in csp, csp)
    t.check("P3 CSP 禁止 object/embed", "object-src 'none'" in csp, csp)
    t.check("P3 CSP 禁止被嵌框", "frame-ancestors 'none'" in csp, csp)
    t.check("P3 CSP 限制表单外发", "form-action 'self'" in csp, csp)
    # 不能只断言函数对象存在——必须确认它真的接进了 ASGI 中间件栈，
    # 且位于本机访问守卫的外层（这样守卫直接返回的 403 也会带上安全头）。
    dispatches = [mw.kwargs.get("dispatch") for mw in app_main.app.user_middleware]
    t.check("P3 安全头中间件已接入 ASGI 中间件栈",
            app_main._security_headers_middleware in dispatches,
            "stack=%r" % [getattr(d, "__name__", d) for d in dispatches])
    if (app_main._security_headers_middleware in dispatches
            and app_main._local_access_guard in dispatches):
        t.check("P3 安全头中间件在守卫外层（403 也带头）",
                dispatches.index(app_main._security_headers_middleware)
                < dispatches.index(app_main._local_access_guard))
    else:
        t.check("P3 安全头中间件在守卫外层（403 也带头）", False,
                "缺少中间件：%r" % (dispatches,))


# ---------------- getaddrinfo patch 收窄 ----------------

def _direct_patch(t):
    original_gai = socket.getaddrinfo
    saved_cc = urllib3_connection.create_connection
    saved_patched = direct_connect._patched
    try:
        direct_connect.enable_direct()
        t.check("P3 enable_direct 不再替换全局 socket.getaddrinfo",
                socket.getaddrinfo is original_gai)
        t.check("P3 enable_direct 改为 patch urllib3 的连接创建函数",
                urllib3_connection.create_connection is direct_connect._patched_create_connection)

        mapping = direct_connect.get_ip_map()
        captured = {}

        def fake_original(address, *args, **kwargs):
            captured["address"] = address
            raise RuntimeError("stop-before-connect")

        real_original = direct_connect._create_connection_original
        direct_connect._create_connection_original = fake_original
        try:
            try:
                direct_connect._patched_create_connection(("app-api.pixiv.net", 443))
            except RuntimeError:
                pass
            t.check("P3 映射中的主机被替换为配置 IP",
                    captured.get("address") == (mapping["app-api.pixiv.net"], 443),
                    "captured=%r" % (captured,))

            captured.clear()
            try:
                direct_connect._patched_create_connection(("example.com", 80))
            except RuntimeError:
                pass
            t.check("P3 非映射主机原样透传",
                    captured.get("address") == ("example.com", 80), "captured=%r" % (captured,))
        finally:
            direct_connect._create_connection_original = real_original
    finally:
        # 还原：避免影响同进程内其它测试模块
        urllib3_connection.create_connection = saved_cc
        direct_connect._patched = saved_patched


# ---------------- 扫描分批提交 ----------------

class _CountingConnection:
    """包一层连接，统计 commit() 调用次数。"""

    def __init__(self, conn, counter):
        self._conn = conn
        self._counter = counter

    def commit(self):
        self._counter["n"] += 1
        return self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _scanner_batching(t):
    t.check("P3 scanner 定义了分批提交大小",
            isinstance(scanner.COMMIT_BATCH_SIZE, int) and scanner.COMMIT_BATCH_SIZE >= 1)

    src = tempfile.mkdtemp(prefix="pa-p3-src-")
    for i in range(5):
        open(os.path.join(src, "12345%02d_p0.jpg" % i), "wb").close()

    database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="pa-p3-db-"), "scan.db")
    database.init_db()

    counter = {"n": 0}
    real_get_connection = scanner.get_connection
    old_batch = scanner.COMMIT_BATCH_SIZE
    scanner.get_connection = lambda: _CountingConnection(real_get_connection(), counter)
    scanner.COMMIT_BATCH_SIZE = 2
    try:
        result = scanner.scan_directory(src)
    finally:
        scanner.get_connection = real_get_connection
        scanner.COMMIT_BATCH_SIZE = old_batch

    t.equal("P3 扫描导入 5 个作品", result.get("new_artworks"), 5)
    t.check("P3 扫描发生多次提交（分批而非单一大事务）",
            counter["n"] >= 3, "commit 次数=%d" % counter["n"])


# ---------------- 同步分批提交 ----------------

class _FakePixivClient:
    def get_illust_detail(self, _pid):
        return {
            "title": "t", "description": "d",
            "author_id": 1, "author_name": "a", "author_profile_image": "",
            "create_date": "2020-01-01 00:00:00", "page_count": 1,
            "width": 1, "height": 1, "tags": [],
        }


def _sync_batching(t):
    t.check("P3 sync 定义了分批提交大小",
            isinstance(sync.SYNC_COMMIT_BATCH_SIZE, int) and sync.SYNC_COMMIT_BATCH_SIZE >= 1)

    database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="pa-p3-db-"), "sync.db")
    database.init_db()
    with database.get_db() as conn:
        for i in range(5):
            conn.execute("INSERT INTO artworks (pixiv_id, author_name) VALUES (?, 'a')",
                         (7000000 + i,))

    counter = {"n": 0}
    real_get_connection = sync.get_connection
    real_client = sync.get_pixiv_client
    real_save = sync._save_metadata_json
    old_batch = sync.SYNC_COMMIT_BATCH_SIZE
    old_delay = os.environ.get("SYNC_DELAY_MS")
    sync.get_connection = lambda: _CountingConnection(real_get_connection(), counter)
    sync.get_pixiv_client = lambda: _FakePixivClient()
    sync._save_metadata_json = lambda *a, **k: None
    sync.SYNC_COMMIT_BATCH_SIZE = 2
    os.environ["SYNC_DELAY_MS"] = "0"
    try:
        results = sync.sync_metadata()
    finally:
        sync.get_connection = real_get_connection
        sync.get_pixiv_client = real_client
        sync._save_metadata_json = real_save
        sync.SYNC_COMMIT_BATCH_SIZE = old_batch
        if old_delay is None:
            os.environ.pop("SYNC_DELAY_MS", None)
        else:
            os.environ["SYNC_DELAY_MS"] = old_delay

    t.equal("P3 同步处理 5 件作品", results.get("synced"), 5)
    t.check("P3 同步发生多次提交（分批而非单一大事务）",
            counter["n"] >= 2, "commit 次数=%d" % counter["n"])
    with database.get_db() as conn:
        synced_rows = conn.execute(
            "SELECT COUNT(*) FROM artworks WHERE last_synced IS NOT NULL").fetchone()[0]
    t.equal("P3 同步结果已落盘", synced_rows, 5)


# ---------------- 锁接管原子化 ----------------

def _lock_atomic(t):
    tmp = tempfile.mkdtemp(prefix="pa-p3-lock-")
    original_dir = run_mod.paths.DATA_DIR
    run_mod.paths.DATA_DIR = tmp
    try:
        lock_path = os.path.join(tmp, run_mod.LOCK_FILE_NAME)
        with open(lock_path, "w", encoding="ascii") as handle:
            handle.write(str(DEAD_PID))
        taken = run_mod.acquire_single_instance()
        t.equal("P3 陈旧锁可被接管", taken, lock_path)
        leftovers = [name for name in os.listdir(tmp) if ".stale." in name]
        t.equal("P3 接管后不留 .stale 临时文件", leftovers, [])
        with open(lock_path, encoding="ascii") as handle:
            t.check("P3 接管后锁内容换成自己",
                    handle.read().strip() == str(os.getpid()))
        run_mod.release_single_instance(taken)

        _lock_takeover_verification(t, lock_path)
        _lock_concurrent_takeover(t, tmp, lock_path)
    finally:
        run_mod.paths.DATA_DIR = original_dir


def _lock_takeover_verification(t, lock_path):
    """判陈旧之后、接管之前，锁被别人换成活锁 → 必须放弃并放回。

    这是原子接管的核心安全属性：老实现（os.remove + 重建）会删掉别人的活锁并
    自己上位；「rename + 内容复核」能识别出「移走的不是刚才那把陈旧锁」而放弃。
    """
    real_raw_is_stale = run_mod._raw_is_stale
    try:
        with open(lock_path, "w", encoding="ascii") as handle:
            handle.write(str(DEAD_PID))

        def fake_raw_is_stale(raw, path):
            # 模拟另一个实例在「判陈旧」之后抢先把锁换成自己的活 PID
            with open(path, "w", encoding="ascii") as handle:
                handle.write(str(os.getpid()))
            return True

        run_mod._raw_is_stale = fake_raw_is_stale
        result = run_mod.acquire_single_instance()
        run_mod._raw_is_stale = real_raw_is_stale
        t.check("P3 接管时若锁已被他人换成活锁则放弃（不抢别人的锁）",
                result is None, "result=%r" % (result,))
        t.check("P3 放弃时把别人的锁放回原位",
                run_mod._lock_content(lock_path) == str(os.getpid()),
                "content=%r" % run_mod._lock_content(lock_path))
    finally:
        run_mod._raw_is_stale = real_raw_is_stale


def _lock_concurrent_takeover(t, tmp, lock_path, rounds=15):
    """多线程并发抢同一把陈旧锁：每一轮都只能有一个赢家。"""
    import threading

    real_raw_is_stale = run_mod._raw_is_stale
    try:
        ok = True
        detail = ""
        for i in range(rounds):
            for name in list(os.listdir(tmp)):
                try:
                    os.remove(os.path.join(tmp, name))
                except OSError:
                    pass
            with open(lock_path, "w", encoding="ascii") as handle:
                handle.write(str(DEAD_PID))

            barrier = threading.Barrier(2, timeout=10)

            def synced_raw_is_stale(raw, path, _real=real_raw_is_stale, _bar=barrier):
                result = _real(raw, path)
                try:
                    _bar.wait()  # 让两个线程都完成「判陈旧」后再各自接管
                except threading.BrokenBarrierError:
                    pass
                return result

            run_mod._raw_is_stale = synced_raw_is_stale
            winners = []
            guard = threading.Lock()

            def worker():
                got = run_mod.acquire_single_instance()
                with guard:
                    winners.append(got)

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=15)

            won = [w for w in winners if w is not None]
            if len(won) != 1:
                ok = False
                detail = "第 %d 轮赢家数=%d（应为 1）" % (i + 1, len(won))
                break
            run_mod.release_single_instance(won[0])
        t.check("P3 并发抢同一把陈旧锁：每轮只有一个赢家", ok, detail)
    finally:
        run_mod._raw_is_stale = real_raw_is_stale


# ---------------- 依赖锁 + TLS 校验 ----------------

def _deps_lock(t):
    lock_path = os.path.join(h.ROOT, "requirements.lock")
    t.check("P3 存在 requirements.lock", os.path.exists(lock_path))
    if not os.path.exists(lock_path):
        return
    with open(lock_path, encoding="utf-8") as handle:
        text = handle.read()
    body = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
    joined = body.replace("\\", " ")
    reqs = re.findall(r"([A-Za-z0-9_.\-]+==[^\s]+)", joined)
    hashes = joined.count("--hash=sha256:")
    t.check("P3 锁文件覆盖完整依赖闭包（>= 20 个包）", len(reqs) >= 20, "包数=%d" % len(reqs))
    t.check("P3 锁文件每个包都有哈希", hashes >= len(reqs),
            "哈希数=%d 包数=%d" % (hashes, len(reqs)))

    bat = open(os.path.join(h.ROOT, "install.bat"), encoding="utf-8", errors="replace").read()
    # 只看真正的命令行，跳过 rem 注释（注释里可能解释性地提到 --trusted-host）
    bat_cmds = "\n".join(
        line for line in bat.splitlines() if not line.strip().lower().startswith("rem"))
    t.check("P3 install.bat 不再关闭 PyPI 证书校验（无 --trusted-host）",
            "--trusted-host" not in bat_cmds)
    t.check("P3 install.bat 使用 --require-hashes", "--require-hashes" in bat)
    readme = open(os.path.join(h.ROOT, "README.md"), encoding="utf-8", errors="replace").read()
    t.check("P3 README 说明 requirements.lock", "requirements.lock" in readme)


# ---------------- 测试入口可信度 ----------------

def _harness_integrity(t):
    """真实服务起不来时，live 用例必须记为失败，而不是静默跳过。

    否则「全部通过 + 退出码 0」会掩盖「冒烟用例根本没跑」（例如已有实例持锁）。
    """
    import run_all
    from types import SimpleNamespace

    dummy = SimpleNamespace(TITLE="dummy", __name__="dummy")
    failed = run_all.failed_module(dummy, "服务未就绪")
    t.check("P3 服务起不来时模块记为失败而非跳过",
            bool(failed.failed) and failed.total >= 1, "rows=%r" % (failed.rows,))
    offline = run_all.skipped_module(dummy, "--offline")
    t.check("P3 --offline 仍按跳过处理（不误报失败）", not offline.failed)

    src = open(os.path.join(h.ROOT, "tests", "run_all.py"), encoding="utf-8").read()
    t.check("P3 run_all 服务启动失败分支改用 failed_module",
            "reports.append(failed_module(module, message))" in src)
