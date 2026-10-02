"""单实例锁：陈旧锁接管、活锁拒绝、释放（全部在临时数据目录里做）。

真实双实例行为（第二个进程拒绝启动、不抢端口）见 `test_http_smoke.py`。
"""
import os
import tempfile
import time

import harness as h

h.bootstrap()
import run as run_mod  # noqa: E402

TITLE = "单实例锁（陈旧接管 / 活锁拒绝 / 释放）"
NEEDS_SERVER = False

# 一个几乎不可能存在的 PID：OpenProcess 会以 ERROR_INVALID_PARAMETER 失败，
# 即被判为「进程已不在」。
DEAD_PID = 15000000


def run(t):
    tmp = tempfile.mkdtemp(prefix="pa-lock-")
    original_dir = run_mod.paths.DATA_DIR
    run_mod.paths.DATA_DIR = tmp
    lock_path = os.path.join(tmp, run_mod.LOCK_FILE_NAME)
    try:
        t.check("当前进程的 PID 判为存活", run_mod._pid_is_alive(os.getpid()) is True)
        t.check("不存在的 PID 判为已退出", run_mod._pid_is_alive(DEAD_PID) is False)

        lock = run_mod.acquire_single_instance()
        t.equal("首次申请返回锁文件路径", lock, lock_path)
        t.check("锁文件写入自己的 PID",
                open(lock_path, encoding="ascii").read().strip() == str(os.getpid()))

        t.equal("已有实例持锁时第二个申请返回 None", run_mod.acquire_single_instance(), None)
        t.check("被拒绝时不会动别人的锁文件",
                open(lock_path, encoding="ascii").read().strip() == str(os.getpid()))

        run_mod.release_single_instance(lock)
        t.check("释放后锁文件消失", not os.path.exists(lock_path))
        run_mod.release_single_instance(None)
        run_mod.release_single_instance(True)
        t.check("释放 None/True 是安全的空操作", not os.path.exists(lock_path))

        with open(lock_path, "w", encoding="ascii") as handle:
            handle.write(str(DEAD_PID))
        t.check("死进程留下的锁判为陈旧", run_mod._lock_file_is_stale(lock_path) is True)
        taken = run_mod.acquire_single_instance()
        t.equal("陈旧锁可被接管", taken, lock_path)
        t.check("接管后锁内容换成自己",
                open(lock_path, encoding="ascii").read().strip() == str(os.getpid()))
        run_mod.release_single_instance(taken)

        with open(lock_path, "w", encoding="ascii") as handle:
            handle.write("")
        t.check("刚写入的空锁不算陈旧（给写入 PID 留的宽限期）",
                run_mod._lock_file_is_stale(lock_path) is False)
        t.equal("空锁期间第二个实例被拒绝", run_mod.acquire_single_instance(), None)
        old = time.time() - run_mod.LOCK_FILE_GRACE_SECONDS - 5
        os.utime(lock_path, (old, old))
        t.check("超过宽限期的空锁判为陈旧", run_mod._lock_file_is_stale(lock_path) is True)
        t.equal("陈旧空锁同样可接管", run_mod.acquire_single_instance(), lock_path)
        run_mod.release_single_instance(lock_path)
        t.check("非法内容 + 新鲜 mtime 不判陈旧",
                not os.path.exists(lock_path))
    finally:
        run_mod.paths.DATA_DIR = original_dir