"""L2：删除失败必须上报；M3：错误分类（状态码优先，绝不误标删除）。"""
import os
import tempfile

import harness as h

h.bootstrap()
from app import main as app_main  # noqa: E402
from app import pixiv  # noqa: E402

TITLE = "L2 删除失败上报 / M3 错误分类"
NEEDS_SERVER = False


def run(t):
    # ---------- L2 ----------
    tmp = tempfile.mkdtemp(prefix="pa-del-")
    real = os.path.join(tmp, "a.jpg")
    with open(real, "wb") as handle:
        handle.write(b"x")
    missing = os.path.join(tmp, "nope.jpg")
    removed, failed = app_main._remove_files([real, missing])
    t.equal("成功项计入 removed", removed, [real])
    t.check("已删除的文件确实不在", not os.path.exists(real))
    t.equal("失败项数量", len(failed), 1)
    t.check("失败项带路径与原因",
            bool(failed) and failed[0]["path"] == missing and bool(failed[0]["error"]))
    t.equal("空输入安全", app_main._remove_files([]), ([], []))

    source = open(os.path.join(h.ROOT, "app", "main.py"), encoding="utf-8").read()
    t.equal("删除逻辑收敛到 _remove_files（定义 1 处 + 调用 2 处）",
            source.count("_remove_files("), 3)

    # ---------- M3 ----------
    t.equal("_fmt_error 取 message 字段", pixiv._fmt_error({"message": "NotFound"}), "NotFound")
    t.check("_fmt_error 空 dict 时退化但不抛错", bool(pixiv._fmt_error({})))
    t.check("_error_is_empty：空 dict 为真", pixiv._error_is_empty({}) is True)
    t.check("_error_is_empty：有 message 为假",
            pixiv._error_is_empty({"message": "x"}) is False)

    # 与 pixiv.py 的真实调用一致：msg 是 _fmt_error(err) 的结果
    cases = [
        ("404 视为不存在", (404, {"message": ""}), True),
        ("404 且错误体为空仍视为不存在", (404, {}), True),
        ("拿不到状态码但错误对象为空，兜底为不存在", (None, {}), True),
        ("关键词命中（英文）", (None, {"message": "Illust not found"}), True),
        ("关键词命中（日文）", (None, {"message": "作品が見つかりません"}), True),
        ("429 判为临时故障", (429, {"message": "rate limit"}), False),
        ("500 判为临时故障", (500, {"message": "server error"}), False),
        ("503 判为临时故障", (503, {}), False),
        ("401 判为认证问题", (401, {"message": "invalid token"}), False),
        ("403 判为风控/认证", (403, {"message": "forbidden"}), False),
    ]
    for name, (status, err), expected in cases:
        msg = pixiv._fmt_error(err) if err else ""
        t.equal("M3 " + name, pixiv._missing_illust(status, err, msg), expected)