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

# 派生数据目录（可用环境变量改名，默认 thumbnails / metadata）
THUMBNAIL_DIR = os.getenv("THUMBNAIL_DIR", "thumbnails")
METADATA_DIR = os.getenv("METADATA_DIR", "metadata")


def thumbnail_path(pixiv_id):
    return os.path.join(DATA_DIR, THUMBNAIL_DIR, f"{pixiv_id}.jpg")


def metadata_path(pixiv_id):
    return os.path.join(DATA_DIR, METADATA_DIR, f"{pixiv_id}.json")


def remove_side_files(pixiv_id):
    """删除某作品派生的边车文件：缩略图与元数据 JSON。

    作品从库中移除后这两个文件即失去引用，必须一并清理，
    否则会残留无用缩略图与孤儿 metadata/*.json。
    返回实际被删除的路径列表。
    """
    removed = []
    for path in (thumbnail_path(pixiv_id), metadata_path(pixiv_id)):
        try:
            if os.path.exists(path):
                os.remove(path)
                removed.append(path)
        except OSError:
            pass
    return removed
