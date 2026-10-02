"""Frozen-exe entry point.

Forces browser mode (auto-opens the default system browser) and runs the
local server from run.py. 桌面窗口模式已移除，exe 始终走浏览器。
"""
import os

os.environ["PA_BROWSER"] = "1"

import run  # noqa: E402

if __name__ == "__main__":
    # 用 os._exit 强制结束：确保不残留 onefile 子进程、不占用端口。
    rc = run.main()
    os._exit(rc if isinstance(rc, int) else 0)