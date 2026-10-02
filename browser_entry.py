"""Browser-only entry for the frozen exe distribution.

Forces the launcher into browser mode (auto-opens the default system
browser) instead of the pywebview window.
"""
import os

os.environ["PA_BROWSER"] = "1"

import sys  # noqa: E402

import launcher  # noqa: E402

if __name__ == "__main__":
    # 用 os._exit 强制结束：托盘“退出”后确保不残留 onefile 子进程、不占用端口。
    rc = launcher.main()
    os._exit(rc if isinstance(rc, int) else 0)