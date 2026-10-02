"""L1：`.env` 就地更新，以及直连 IP 覆盖走同一套读写。

不读真实 `.env`：全部在临时文件上验证；`direct_connect` 的路径指向也用临时文件替换。
"""
import os
import tempfile

import harness as h

h.bootstrap()
from app import direct_connect  # noqa: E402
from app import envfile  # noqa: E402

TITLE = "L1 .env 就地更新（注释/顺序/换行风格/原子写）"
NEEDS_SERVER = False

ORIGINAL = (
    "# 顶部注释\n"
    "\n"
    "PIXIV_REFRESH_TOKEN=oldtoken\n"
    "IMAGE_SOURCE_DIR=D:\\pics\r\n"
    "# 中间注释\n"
    "PA_PORT=6814\n"
)


def _read(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def run(t):
    tmp = tempfile.mkdtemp(prefix="pa-envfile-")
    path = os.path.join(tmp, ".env")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(ORIGINAL)

    envfile.update_env(path, {"PIXIV_REFRESH_TOKEN": "newtoken"})
    text = _read(path)
    t.check("注释与空行保留", "# 顶部注释" in text and "# 中间注释" in text and "\n\n" in text)
    t.check("原有顺序保留",
            text.index("PIXIV_REFRESH_TOKEN") < text.index("IMAGE_SOURCE_DIR") < text.index("PA_PORT"))
    t.check("目标键已更新", "PIXIV_REFRESH_TOKEN=newtoken" in text and "oldtoken" not in text)
    t.check("其它键原样", "IMAGE_SOURCE_DIR=D:\\pics" in text and "PA_PORT=6814" in text)
    t.check("沿用文件既有 CRLF 换行风格", "\r\n" in text)

    envfile.update_env(path, {"NEW_KEY": "中文值 with spaces"})
    text = _read(path)
    t.check("新键追加到末尾", text.rstrip("\r\n").endswith("NEW_KEY=中文值 with spaces"))
    t.check("追加后注释仍保留", "# 中间注释" in text)
    parsed = envfile.read_env(path)
    t.check("read_env 解析正确",
            parsed.get("NEW_KEY") == "中文值 with spaces"
            and parsed.get("PA_PORT") == "6814"
            and parsed.get("PIXIV_REFRESH_TOKEN") == "newtoken")
    t.equal("update_env 返回被更新的键", envfile.update_env(path, {"A": "1"}), ["A"])
    t.equal("空更新不写文件", envfile.update_env(path, {}), [])
    t.equal("临时文件不残留", sorted(os.listdir(tmp)), [".env"])

    fresh = os.path.join(tmp, "sub", "fresh.env")
    envfile.update_env(fresh, {"B": "2"})
    t.check("目标文件不存在时安全创建", "B=2" in _read(fresh))

    mixed = os.path.join(tmp, "lf.env")
    with open(mixed, "w", encoding="utf-8", newline="") as handle:
        handle.write("X=1\nY=2\n")
    envfile.update_env(mixed, {"X": "9"})
    t.check("纯 LF 文件不会被动成 CRLF", "\r" not in _read(mixed))

    ip_env = os.path.join(tmp, "ip.env")
    with open(ip_env, "w", encoding="utf-8") as handle:
        handle.write("# 直连 IP 覆盖\nPIXIV_IP_APP_API=1.1.1.1\n")
    original_path = direct_connect._env_path
    direct_connect._env_path = lambda: ip_env
    try:
        direct_connect.save_ip_override("app-api.pixiv.net", "2.2.2.2")
        ip_text = _read(ip_env)
        t.check("direct_connect 写入也保留注释",
                "PIXIV_IP_APP_API=2.2.2.2" in ip_text and "# 直连 IP 覆盖" in ip_text)
        t.equal("改后仍能读回 IP",
                direct_connect.get_ip_map().get("app-api.pixiv.net"), "2.2.2.2")
    finally:
        direct_connect._env_path = original_path