"""`.env` 的读写助手：解析成 dict、原地更新给定键。

为什么不用「读成 dict → 全量重写」：那样会丢掉用户写在 .env 里的注释、空行与
原有顺序，而这些正是用户手工维护过的信息。这里改为在原始行序列上就地替换，只动
需要更新的键，其余内容原样保留（含原有换行风格）。

写入用「同目录临时文件 + os.replace」原子替换，避免写到一半被中断而留下残缺的
.env；同进程内的并发调用由模块级锁串行化（本应用是单进程多线程模型）。
"""
import os
import tempfile
import threading

_LOCK = threading.Lock()


def read_env(path):
    """把 .env 解析成 {键: 值}；文件不存在则返回空 dict。"""
    result = {}
    if not os.path.exists(path):
        return result
    with open(path, "r", encoding="utf-8", newline="") as f:
        for line in f:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key, _, value = stripped.partition("=")
                result[key.strip()] = value.strip()
    return result


def _preferred_newline(lines):
    """沿用文件既有换行风格（Windows 上手工编辑的 .env 通常是 CRLF）。"""
    return "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"


def _upsert_lines(lines, updates):
    """在原始行序列上替换/追加键，返回新行序列（注释、空行与顺序均保留）。"""
    pending = dict(updates)
    newline = _preferred_newline(lines)
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in pending:
                out.append("%s=%s%s" % (key, pending.pop(key), newline))
                continue
        out.append(line if line.endswith("\n") else line + newline)
    if out and not out[-1].endswith("\n"):
        out[-1] += newline
    for key, value in pending.items():
        out.append("%s=%s%s" % (key, value, newline))
    return out


def update_env(path, updates):
    """更新 .env 中给定的键并原子写回，返回实际更新的键列表。

    未在 updates 中出现的键、注释、空行与顺序全部保持不变。
    """
    updates = dict(updates or {})
    if not updates:
        return []
    with _LOCK:
        lines = []
        if os.path.exists(path):
            # newline="" 必须带上：否则通用换行模式会把 CRLF 归一成 LF，
            # 写回时就把用户的换行风格悄悄改掉了
            with open(path, "r", encoding="utf-8", newline="") as f:
                lines = f.readlines()
        text = "".join(_upsert_lines(lines, updates))
        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".env.", suffix=".tmp")
        try:
            # newline="" 表示不做换行转换，写出的就是 text 里的原样字节
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    return list(updates)