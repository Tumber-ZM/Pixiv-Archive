"""P1-7：统一日志（README 承诺的 `pixiv_archive.log` 必须真的生成）。

此前项目零 `logging`：README 说运行后会在数据目录生成 `pixiv_archive.log`，但该
文件根本不会被创建；后台任务的 traceback 只存在内存里，重启即丢。本模块验证：

1. `setup_logging()` 真的把记录写进 `DATA_DIR/pixiv_archive.log`；
2. 幂等（重复调用不重复挂 handler）、数据目录不可写时不抛异常；
3. 关键「吞异常」处确实落了日志（后台任务失败含 traceback）；
4. `run.py` 启动链路会调用它。

全部离线；用临时目录当 DATA_DIR，并在用例结束时把根 logger 的 handler 还原，
避免污染同一进程里的其它测试模块。
"""
import contextlib
import logging
import os
import tempfile
import time

import harness as h

h.bootstrap()
from app import jobs  # noqa: E402
from app import logging_setup  # noqa: E402
from app import paths  # noqa: E402

TITLE = "P1-7 统一日志（pixiv_archive.log / 吞异常处留痕）"
NEEDS_SERVER = False


def _read(*parts):
    with open(os.path.join(h.ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


@contextlib.contextmanager
def _temp_logging():
    """把 DATA_DIR 指到临时目录并重置 logging_setup 状态，退出时完全还原。"""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_dir = paths.DATA_DIR
    saved_configured = logging_setup._configured
    paths.DATA_DIR = tempfile.mkdtemp(prefix="pa-log-")
    logging_setup._configured = False
    try:
        yield paths.DATA_DIR
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                try:
                    handler.close()
                except Exception:  # noqa: BLE001
                    pass
                root.removeHandler(handler)
        root.setLevel(saved_level)
        paths.DATA_DIR = saved_dir
        logging_setup._configured = saved_configured


def _flush():
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:  # noqa: BLE001
            pass


def _writes_file(t):
    with _temp_logging() as tmp:
        log_path = logging_setup.setup_logging(console=False)
        t.equal("P1-7 日志路径 = DATA_DIR/pixiv_archive.log",
                log_path, os.path.join(tmp, "pixiv_archive.log"))

        logging_setup.get_logger("pixiv_archive.probe").warning("probe-%s", "xyzzy")
        _flush()
        with open(log_path, encoding="utf-8") as handle:
            content = handle.read()
        t.check("P1-7 记录真的写进了日志文件", "probe-xyzzy" in content,
                "content=%r" % content[:200])
        t.check("P1-7 记录含级别与模块名",
                "WARNING" in content and "pixiv_archive.probe" in content)
        t.check("P1-7 记录含时间戳", time.strftime("%Y-") in content)


def _idempotent(t):
    with _temp_logging():
        logging_setup.setup_logging(console=False)
        count = len(logging.getLogger().handlers)
        t.check("P1-7 首次配置至少挂了一个 handler", count >= 1)
        logging_setup.setup_logging(console=False)
        logging_setup.setup_logging(console=False)
        t.equal("P1-7 重复调用不会重复挂 handler",
                len(logging.getLogger().handlers), count)


def _unwritable_dir_does_not_raise(t):
    with _temp_logging():
        # 用一个「文件」充当 DATA_DIR：其下建文件必然失败（NotADirectoryError）
        blocker = tempfile.mkdtemp(prefix="pa-log-block-")
        blocker_file = os.path.join(blocker, "not-a-dir")
        open(blocker_file, "w", encoding="utf-8").close()
        paths.DATA_DIR = blocker_file
        logging_setup._configured = False
        raised = None
        try:
            logging_setup.setup_logging(console=False)
        except Exception as exc:  # noqa: BLE001
            raised = exc
        t.check("P1-7 数据目录不可写时不抛异常（退化为仅控制台）",
                raised is None, "raised=%r" % (raised,))


def _job_failure_is_logged(t):
    with _temp_logging():
        log_path = logging_setup.setup_logging(console=False)

        def boom(job):
            raise RuntimeError("job-boom-xyzzy")

        job_id, err = jobs.start("probe", boom)
        if err is not None:
            t.check("P1-7 测试任务能启动（无其它任务占用）", False, err)
            return
        deadline = time.time() + 10
        while time.time() < deadline and jobs.get(job_id)["status"] == "running":
            time.sleep(0.05)
        t.equal("P1-7 失败任务被标记为 error", jobs.get(job_id)["status"], "error")

        _flush()
        with open(log_path, encoding="utf-8") as handle:
            content = handle.read()
        t.check("P1-7 后台任务失败写入了日志（含异常信息）",
                "job-boom-xyzzy" in content, "content=%r" % content[:300])
        t.check("P1-7 后台任务失败日志含 traceback",
                "Traceback" in content and "RuntimeError" in content,
                "content=%r" % content[:300])


def _instrumentation_present(t):
    t.check("P1-7 后台任务失败会记日志（logger.exception）",
            "logger.exception(" in _read("app", "jobs.py"))
    t.check("P1-7 下载失败会记日志",
            "下载作品 %s 失败" in _read("app", "download.py"))
    t.check("P1-7 缩略图生成失败会记日志",
            "生成缩略图失败" in _read("app", "thumbnails.py"))
    t.check("P1-7 扫描计算 SHA256 失败会记日志",
            "计算 SHA256 失败" in _read("app", "scanner.py"))
    t.check("P1-7 刷新 IP 的结果会记日志",
            "未刷新到任何可用 IP" in _read("app", "direct_connect.py"))
    t.check("P1-7 DoH 单次失败会记日志（debug）",
            "DoH 查询失败" in _read("app", "direct_connect.py"))
    t.check("P1-7 打开资源管理器失败会记日志",
            "打开资源管理器失败" in _read("app", "main.py"))

    run_src = _read("run.py")
    t.check("P1-7 run.py 启动时调用 setup_logging",
            "logging_setup.setup_logging()" in run_src)
    t.check("P1-7 run.py 启动与退出都有日志",
            "启动 Pixiv Archive" in run_src and "释放单实例锁" in run_src)
    t.check("P1-7 接管陈旧锁会记日志",
            "接管陈旧单实例锁" in run_src)


def run(t):
    _writes_file(t)
    _idempotent(t)
    _unwritable_dir_does_not_raise(t)
    _job_failure_is_logged(t)
    _instrumentation_present(t)
