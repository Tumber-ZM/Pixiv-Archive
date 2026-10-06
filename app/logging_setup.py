"""统一日志配置（README 承诺的 `pixiv_archive.log`）。

此前项目零 `logging`：README 说运行后会在数据目录生成 `pixiv_archive.log`，但该
文件根本不会被创建；后台任务的 traceback 也只存在内存里，重启即丢。这里统一配置
一个轮转文件日志（外加控制台），由 `run.py` 在启动时调用一次。
"""
import logging
import os
from logging.handlers import RotatingFileHandler

from app import paths

LOG_FILE_NAME = "pixiv_archive.log"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 3
_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def get_log_path():
    """日志文件路径（位于数据目录；源码运行时即项目根目录）。"""
    return os.path.join(paths.DATA_DIR, LOG_FILE_NAME)


def setup_logging(level=logging.INFO, console=True):
    """幂等配置根 logger，返回日志文件路径。

    数据目录不可写时只挂控制台 handler —— 日志配置本身绝不能把服务拖垮。
    """
    global _configured
    if _configured:
        return get_log_path()

    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    try:
        file_handler = RotatingFileHandler(
            get_log_path(), maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
            encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except (OSError, ValueError):
        # 数据目录不存在/不可写/路径非法：退化为仅控制台，绝不让日志配置把服务拖垮
        pass

    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    # 第三方库的 INFO 噪音（每建一次连接就一条）压到 WARNING
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    _configured = True
    return get_log_path()


def get_logger(name):
    return logging.getLogger(name)
