"""AI 生成标识：解析 / 落库 / 迁移 / 筛选 / 角标。

需求要点：
- AI 标识复用 R18 已有的「缩略图角标 + 筛选」形态，但**绝不能覆盖 R18**；
- **存量数据先统一置为「非 AI」**。

Pixiv 语义：`illust_ai_type` 1 = 非 AI，2 = AI 生成。

全部离线：临时库 + 直调端点函数 + 读模板/CSS，不联网、不起服务。
"""
import os
import sqlite3
import tempfile

import harness as h

h.bootstrap()
from app import database  # noqa: E402
from app import main as app_main  # noqa: E402
from app import pixiv  # noqa: E402
from app import sync  # noqa: E402

TITLE = "AI 标识（解析 / 落库 / 存量迁移 / 筛选 / 角标）"
NEEDS_SERVER = False


def _read(*parts):
    with open(os.path.join(h.ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


class _FakeClient:
    def __init__(self, detail):
        self._detail = detail

    def get_illust_detail(self, pixiv_id):
        return self._detail


def _illust(ai_type):
    return {
        "title": "T",
        "description": "D",
        "author_id": 1,
        "author_name": "作者",
        "author_profile_image": "",
        "create_date": "2020-01-01T00:00:00+09:00",
        "page_count": 1,
        "width": 10,
        "height": 10,
        "ai_type": ai_type,
        "tags": [],
    }


# ---------------- 解析层 ----------------

def _parse(t):
    parse = pixiv.PixivClient.__dict__["_parse_illust"]
    t.equal("AI 解析：illust_ai_type=2 → ai_type=2",
            parse(None, {"id": 1, "illust_ai_type": 2})["ai_type"], 2)
    t.equal("AI 解析：illust_ai_type=1 → ai_type=1",
            parse(None, {"id": 1, "illust_ai_type": 1})["ai_type"], 1)
    t.equal("AI 解析：字段缺失 → 按 1（非 AI）处理，不误标",
            parse(None, {"id": 1})["ai_type"], 1)


# ---------------- 存量迁移：老库补列并统一置为非 AI ----------------

_OLD_SCHEMA = """
CREATE TABLE artworks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pixiv_id INTEGER UNIQUE NOT NULL,
    title TEXT,
    description TEXT,
    author_id INTEGER,
    author_name TEXT,
    create_date TEXT,
    page_count INTEGER DEFAULT 1,
    width INTEGER,
    height INTEGER,
    pixiv_status TEXT DEFAULT 'active',
    first_seen TEXT DEFAULT (datetime('now')),
    last_synced TEXT,
    local_path TEXT
);
"""


def _migration(t):
    root = tempfile.mkdtemp(prefix="pa-ai-mig-")
    db_path = os.path.join(root, "old.db")
    # 造一个「老版本」库：artworks 没有 ai_type 列，且已有存量行
    raw = sqlite3.connect(db_path)
    raw.executescript(_OLD_SCHEMA)
    raw.execute("INSERT INTO artworks (pixiv_id, title) VALUES (111, '存量1')")
    raw.execute("INSERT INTO artworks (pixiv_id, title) VALUES (222, '存量2')")
    raw.commit()
    raw.close()

    saved = database.DB_PATH
    database.DB_PATH = db_path
    try:
        database.init_db()  # 触发迁移
        with database.get_db() as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(artworks)")]
            t.check("AI 迁移：老库补上了 ai_type 列", "ai_type" in cols, "cols=%r" % cols)
            values = [r[0] for r in conn.execute(
                "SELECT ai_type FROM artworks ORDER BY pixiv_id")]
            t.equal("AI 迁移：存量行统一置为 1（非 AI）", values, [1, 1])
        # 幂等：再跑一次不应报错、也不应改变数据
        database.init_db()
        with database.get_db() as conn:
            again = [r[0] for r in conn.execute(
                "SELECT ai_type FROM artworks ORDER BY pixiv_id")]
            t.equal("AI 迁移：重复执行幂等", again, [1, 1])
    finally:
        database.DB_PATH = saved


# ---------------- 同步写回 ----------------

def _sync_write(t):
    root = tempfile.mkdtemp(prefix="pa-ai-sync-")
    saved_db, saved_dir = database.DB_PATH, None
    from app import paths
    saved_dir = paths.DATA_DIR
    database.DB_PATH = os.path.join(root, "s.db")
    paths.DATA_DIR = root
    saved_client = sync.get_pixiv_client
    saved_delay = os.environ.get("SYNC_DELAY_MS")
    try:
        database.init_db()
        with database.get_db() as conn:
            conn.execute("INSERT INTO artworks (pixiv_id, author_name, first_seen) "
                         "VALUES (9001, 'a', '2020-01-01 00:00:00')")
        for ai_type, expect, label in ((2, 2, "AI 作品"), (1, 1, "非 AI 作品")):
            sync.get_pixiv_client = lambda _t=ai_type: _FakeClient(_illust(_t))
            os.environ["SYNC_DELAY_MS"] = "0"
            sync.sync_metadata(specific_pixiv_id=9001)
            with database.get_db() as conn:
                got = conn.execute(
                    "SELECT ai_type FROM artworks WHERE pixiv_id = 9001").fetchone()[0]
            t.equal("AI 同步：%s 写回 ai_type=%d" % (label, expect), got, expect)
    finally:
        sync.get_pixiv_client = saved_client
        database.DB_PATH = saved_db
        paths.DATA_DIR = saved_dir
        if saved_delay is None:
            os.environ.pop("SYNC_DELAY_MS", None)
        else:
            os.environ["SYNC_DELAY_MS"] = saved_delay


# ---------------- 端点筛选（含与 R18 组合） ----------------

def _seed(conn):
    """4 件作品：A(非AI/非R18) B(AI/非R18) C(非AI/R18) D(AI/R18)。"""
    rows = [(1, "A", 1, False), (2, "B", 2, False), (3, "C", 1, True), (4, "D", 2, True)]
    for pixiv_id, title, ai_type, is_r18 in rows:
        conn.execute(
            "INSERT INTO artworks (pixiv_id, title, author_name, ai_type) VALUES (?, ?, 'a', ?)",
            (pixiv_id, title, ai_type))
    conn.execute("INSERT INTO tags (id, name) VALUES (1, 'R-18')")
    for pixiv_id in (3, 4):
        aid = conn.execute("SELECT id FROM artworks WHERE pixiv_id = ?",
                           (pixiv_id,)).fetchone()[0]
        conn.execute("INSERT INTO artwork_tags (artwork_id, tag_id) VALUES (?, 1)", (aid,))


def _call(ai="", r18=""):
    res = app_main.api_artworks(page=1, per_page=50, sort="id", order="asc",
                                author="", tag="", favorite_id=0,
                                r18=r18, ai=ai, status="", seed=0)
    return [w["pixiv_id"] for w in res["artworks"]]


def _endpoint_filter(t):
    root = tempfile.mkdtemp(prefix="pa-ai-filter-")
    saved = database.DB_PATH
    database.DB_PATH = os.path.join(root, "f.db")
    try:
        database.init_db()
        with database.get_db() as conn:
            _seed(conn)

        t.equal("AI 筛选 all：返回全部", _call(ai=""), [1, 2, 3, 4])
        t.equal("AI 筛选 hide：排除 AI 作品", _call(ai="hide"), [1, 3])
        t.equal("AI 筛选 only：只留 AI 作品", _call(ai="only"), [2, 4])

        # R18 必须仍然可用（回归护栏）
        t.equal("R18 筛选 only 仍正常（未被 AI 覆盖）", _call(r18="only"), [3, 4])
        t.equal("R18 筛选 hide 仍正常", _call(r18="hide"), [1, 2])

        # 两者组合：必须 AND，而不是互相覆盖
        t.equal("AI=only 且 R18=only → 取交集（既 AI 又 R18）",
                _call(ai="only", r18="only"), [4])
        t.equal("AI=hide 且 R18=only → 取交集（非 AI 且 R18）",
                _call(ai="hide", r18="only"), [3])
        t.equal("AI=only 且 R18=hide → 取交集（AI 且非 R18）",
                _call(ai="only", r18="hide"), [2])

        # is_ai 字段（角标数据源）
        res = app_main.api_artworks(page=1, per_page=50, sort="id", order="asc",
                                    author="", tag="", favorite_id=0,
                                    r18="", ai="", status="", seed=0)
        flags = {w["pixiv_id"]: w["is_ai"] for w in res["artworks"]}
        t.equal("AI 端点返回 is_ai 布尔字段（角标数据源）",
                flags, {1: False, 2: True, 3: False, 4: True})
        t.check("AI 端点同时保留 is_r18 字段（未互相挤掉）",
                "is_r18" in res["artworks"][0])
    finally:
        database.DB_PATH = saved


# ---------------- 前端接线 ----------------

def _frontend(t):
    index = _read("app", "templates", "index.html")
    common = _read("app", "templates", "partials", "common_js.html")
    css = _read("app", "static", "css", "style.css")

    t.check("AI 前端：index 有 AI 角标渲染",
            'if (a.is_ai) badges += `<span class="ai">AI</span>`;' in index)
    t.check("AI 前端：index 有筛选按钮", 'id="btnAI"' in index and "cycleAI()" in index)
    t.check("AI 前端：index 有 initAI/updateAIBtn",
            "function initAI()" in index and "function updateAIBtn()" in index)
    t.check("AI 前端：启动时调用 initAI()", "\ninitAI();" in index)
    t.check("AI 前端：取数 URL 带 ai 参数",
            "url += '&ai=' + aip;" in index)
    t.check("AI 前端：作者/收藏视图也带 ai 参数",
            "'&ai=' + aiParam()" in index)
    t.check("AI 前端：common_js 提供 aiParam()", "function aiParam()" in common)
    t.check("AI 前端：tag.html 取数带 ai 参数",
            "&ai=${aiParam()}" in _read("app", "templates", "tag.html"))
    t.check("AI 前端：author.html 取数带 ai 参数",
            "&ai=${aiParam()}" in _read("app", "templates", "author.html"))

    t.check("AI 前端：CSS 有 .tile-badges span.ai", ".tile-badges span.ai {" in css)
    t.check("AI 角标颜色与 R18 不同（不复用 --danger）",
            "background: var(--ai);" in css and "--ai:" in css)

    # R18 一条都不能少
    t.check("R18 未被覆盖：按钮/角标/模式函数仍在",
            'id="btnR18"' in index and 'class="r18">R18</span>' in index
            and "function cycleR18()" in index and "function initR18()" in index)
    t.check("R18 未被覆盖：common_js 的 r18Param 仍在",
            "function r18Param()" in common and "localStorage.getItem('pa-r18')" in common)
    t.check("R18 与 AI 用不同的 localStorage 键",
            "localStorage.getItem('pa-ai')" in index and "localStorage.getItem('pa-r18')" in index)
    t.check("R18 未被覆盖：后端 _R18_TAG_SET 与 _r18_where 仍在",
            app_main._R18_TAG_SET == "('r-18','r18','18r')"
            and app_main._r18_where("a", "only") is not None)


def run(t):
    _parse(t)
    _migration(t)
    _sync_write(t)
    _endpoint_filter(t)
    _frontend(t)
