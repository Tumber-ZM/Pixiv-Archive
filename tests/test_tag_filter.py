"""L4：标签筛选与 `/api/tags?q=` 都按「原名 + 中文译名」匹配。

在隔离的临时库上验证：真实归档里恰好没有一个标签带译名，直接查真实库证明不了
这条修复，所以临时插一条带译名的标签。
"""
import os
import tempfile

import harness as h

h.bootstrap()
from app import database  # noqa: E402
from app import main as app_main  # noqa: E402

TITLE = "L4 标签按原名与译名匹配"
NEEDS_SERVER = False

TAG_NAME = "原題タグ"
TAG_TRANSLATED = "中文译名XYZ"

# 直接调用路由函数时，FastAPI 的 Query 默认值不会自动生效，必须把所有参数给全
ARTWORK_DEFAULTS = dict(page=1, per_page=50, sort="id", order="desc", author="",
                        tag="", favorite_id=0, r18="", status="", seed=0)


def artworks(**overrides):
    kwargs = dict(ARTWORK_DEFAULTS)
    kwargs.update(overrides)
    return app_main.api_artworks(**kwargs)


def run(t):
    database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="pa-tags-"), "tags.db")
    database.init_db()
    with database.get_db() as conn:
        conn.execute("INSERT INTO artworks (pixiv_id, title, author_name) VALUES (999001, 'T', 'A')")
        artwork_id = conn.execute("SELECT id FROM artworks WHERE pixiv_id = 999001").fetchone()[0]
        conn.execute("INSERT INTO tags (name, translated_name) VALUES (?, ?)",
                     (TAG_NAME, TAG_TRANSLATED))
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (TAG_NAME,)).fetchone()[0]
        conn.execute("INSERT INTO artwork_tags (artwork_id, tag_id) VALUES (?, ?)",
                     (artwork_id, tag_id))

    by_original = artworks(tag=TAG_NAME)
    t.equal("按原名筛到 1 件", by_original["total"], 1)
    by_translated = artworks(tag=TAG_TRANSLATED)
    t.equal("按中文译名也能筛到同一件（修复点）", by_translated["total"], 1)
    t.equal("筛出的确实是那件作品", by_translated["artworks"][0]["pixiv_id"], 999001)
    t.equal("无关词筛不到", artworks(tag="毫不相关的词")["total"], 0)

    listing = app_main.api_tags(q="中文译名")
    names = [row["name"] for row in listing]
    t.check("/api/tags?q= 用译名也能搜到该标签", TAG_NAME in names)
    t.check("/api/tags?q= 用原名同样搜到",
            TAG_NAME in [row["name"] for row in app_main.api_tags(q="原題")])