"""M5：已删除作品按 30 天复查；M6：多页下载完成度精确判定；M2：IP 可用性判定收窄。"""
import os
import tempfile

import harness as h

h.bootstrap()
from app import database  # noqa: E402
from app import direct_connect  # noqa: E402
from app import download  # noqa: E402
from app import sync  # noqa: E402

TITLE = "M5 已删除复查 / M6 下载完成度 / M2 IP 可用性"
NEEDS_SERVER = False


class FakeResponse:
    def __init__(self, status, content_type=None):
        self.status_code = status
        self.headers = {"Content-Type": content_type} if content_type else {}


def run(t):
    # ---------- M6：下载完成度 ----------
    tmp = tempfile.mkdtemp(prefix="pa-dl-")
    ext = sorted(download.IMAGE_EXTENSIONS)[0]
    for name in ("[作者] 123456" + ext, "[作者] 123456_p0" + ext, "[作者] 1234567" + ext):
        open(os.path.join(tmp, name), "wb").close()
    t.check("M6 单页作品已完整则判定完成",
            download._work_exists(tmp, "[作者] 123456", 1) is True)
    t.check("M6 前缀不会误命中更长的作品名",
            download._work_exists(tmp, "[作者] 1234567", 1) is True)
    t.check("M6 多页作品只下到第 1 页不算完成",
            download._work_exists(tmp, "[作者] 123456_p", 3) is False)
    t.check("M6 多页作品缺页时不算完成",
            download._work_exists(tmp, "[作者] 123456_p", 2) is False)
    t.check("M6 目录不存在返回 False",
            download._work_exists(os.path.join(tmp, "nope"), "x", 1) is False)

    # ---------- M5：待同步行筛选 ----------
    database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="pa-sync-"), "m5.db")
    database.init_db()
    rows = [
        # (pixiv_id, title, last_synced, sync_error, pixiv_status)
        (1, None, "2026-01-01 00:00:00", None, "active"),   # 缺标题 → 选中
        (2, "t", None, None, "active"),                     # 从未同步 → 选中
        (3, "t", "2026-01-01 00:00:00", "boom", "active"),  # 上次失败 → 选中
        (4, "t", "2026-01-01 00:00:00", None, "active"),    # 正常 → 不选
        (5, "t", "2020-01-01 00:00:00", None, "deleted"),   # 删除超 30 天 → 选中
        (6, "t", "2099-01-01 00:00:00", None, "deleted"),   # 刚确认删除 → 不选
    ]
    with database.get_db() as conn:
        for pixiv_id, title, last_synced, sync_error, status in rows:
            conn.execute(
                "INSERT INTO artworks (pixiv_id, title, author_name, last_synced, sync_error, "
                "pixiv_status) VALUES (?, ?, 'a', ?, ?, ?)",
                (pixiv_id, title, last_synced, sync_error, status))
        selected = {row[1] for row in sync._select_pending_rows(conn)}
        t.equal("M5 默认筛选选中的作品", selected, {1, 2, 3, 5})
        specific = {row[1] for row in sync._select_pending_rows(conn, 4)}
        t.equal("M5 指定 pixiv_id 时无条件同步", specific, {4})
    t.equal("M5 复查间隔为 30 天", sync._DELETED_RECHECK_DAYS, 30)

    # ---------- M2：IP 可用性判定 ----------
    cases = [
        ("200 视为可用", FakeResponse(200), True),
        ("404 JSON 视为可用（Pixiv 对未鉴权 / 的常态）",
         FakeResponse(404, "application/json"), True),
        ("403 HTML 视为被拦截", FakeResponse(403, "text/html"), False),
        ("403 JSON 仍视为可用", FakeResponse(403, "application/json"), True),
        ("451 视为被拦截", FakeResponse(451), False),
        ("407 视为被拦截", FakeResponse(407), False),
        ("500 视为不可用", FakeResponse(500), False),
        ("503 视为不可用", FakeResponse(503), False),
        ("拿不到状态码视为不可用", FakeResponse(None), False),
    ]
    for name, resp, expected in cases:
        t.equal("M2 " + name, direct_connect._response_is_usable(resp), expected)