"""集中式路径管理。

- DATA_DIR：可写数据目录。打包后（PyInstaller）= exe 同级目录（便携模式）；
  源码运行时 = 项目根目录。
- RES_DIR：只读打包资源根目录。打包后 = sys._MEIPASS；源码运行时 = 项目根目录。
- APP_DIR：app 包所在目录（内含 static/ 与 templates/）。
- ENV_FILE：位于 DATA_DIR 内的 .env 路径。
"""
import os
import sys


def _is_frozen():
    return bool(getattr(sys, "frozen", False))


def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_data_dir():
    env_data_dir = (os.getenv("PA_DATA_DIR", "") or "").strip()
    if env_data_dir:
        return os.path.abspath(env_data_dir)
    if _is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return _project_root()


def get_res_dir():
    if _is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return meipass
        return os.path.dirname(os.path.abspath(sys.executable))
    return _project_root()


def get_app_dir():
    return os.path.join(get_res_dir(), "app")


DATA_DIR = get_data_dir()
RES_DIR = get_res_dir()
APP_DIR = get_app_dir()
ENV_FILE = os.path.join(DATA_DIR, ".env")
