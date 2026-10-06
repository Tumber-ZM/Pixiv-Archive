"""P1-7 补测：scanner / sync / download 三条「写数据」路径（原几乎无测试）。

- `scanner.scan_directory`：入库、重复扫描幂等、SHA-256 去重、缺失清理
- `sync.sync_metadata`：错误分类（认证中止 / 网络失败记 `sync_error` / 已删除标记）
- `download.download_single_illust`：成功落盘（`.part` 原子改名）、重复跳过

全部离线：临时库 + 临时目录 + 假 Pixiv 客户端，不联网、不起服务。
"""
import contextlib
import io
import os
import tempfile

import harness as h

h.bootstrap()
from app import database  # noqa: E402
from app import download  # noqa: E402
from app import paths  # noqa: E402
from app import scanner  # noqa: E402
from app import sync  # noqa: E402
from app.pixiv import PixivAuthError, PixivDeletedError, PixivNetworkError  # noqa: E402

TITLE = "P1-7 写路径补测（scan / sync / download）"
NEEDS_SERVER = False


@contextlib.contextmanager
def _env():
    """把 DB 与数据目录都指到临时目录，退出时还原（避免污染其它模块）。"""
    root = tempfile.mkdtemp(prefix="pa-paths-")
    saved_db = database.DB_PATH
    saved_dir = paths.DATA_DIR
    database.DB_PATH = os.path.join(root, "test.db")
    paths.DATA_DIR = root
    try:
        database.init_db()
        yield root
    finally:
        database.DB_PATH = saved_db
        paths.DATA_DIR = saved_dir


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def _images_of(conn, pixiv_id):
    return conn.execute(
        "SELECT COUNT(*) FROM images WHERE artwork_id = "
        "(SELECT id FROM artworks WHERE pixiv_id = ?)", (pixiv_id,)).fetchone()[0]


# ---------------- scanner ----------------

def _scanner(t):
    with _env() as root:
        src = os.path.join(root, "src")
        _write(os.path.join(src, "1111111_p0.png"), b"IMG-A")
        _write(os.path.join(src, "1111111_p1.png"), b"IMG-B")
        _write(os.path.join(src, "2222222.png"), b"IMG-C")

        result = scanner.scan_directory(src)
        t.equal("scan 首次入库：2 个作品", result["new_artworks"], 2)
        t.equal("scan 首次入库：3 张图片", result["new_images"], 3)

        with database.get_db() as conn:
            t.equal("scan 多页作品记录页数", conn.execute(
                "SELECT page_count FROM artworks WHERE pixiv_id = 1111111").fetchone()[0], 2)
            t.equal("scan 图片行数正确", _images_of(conn, 1111111), 2)

        again = scanner.scan_directory(src)
        t.equal("scan 重复扫描不新增作品", again["new_artworks"], 0)
        t.equal("scan 重复扫描不新增图片", again["new_images"], 0)
        t.equal("scan 重复扫描记为已存在", again["skipped"], 3)

        # 内容相同的另一张图（不同 PID）→ 必须在入库时按 SHA-256 判定重复
        _write(os.path.join(src, "3333333.png"), b"IMG-C")
        dup = scanner.scan_directory(src)
        t.check("scan 入库时检出内容重复", dup["duplicates"] >= 1,
                "duplicates=%d" % dup["duplicates"])
        with database.get_db() as conn:
            t.equal("scan 重复内容不会写成新图片行", _images_of(conn, 3333333), 0)
            t.equal("scan 去重不会误删原图", _images_of(conn, 2222222), 1)

        # 库里遗留的历史重复行（老版本可能已写入）→ 由 _prune_duplicates 清理
        with database.get_db() as conn:
            aid = conn.execute(
                "SELECT id FROM artworks WHERE pixiv_id = 2222222").fetchone()[0]
            sha = conn.execute(
                "SELECT sha256 FROM images WHERE artwork_id = ?", (aid,)).fetchone()[0]
            conn.execute(
                "INSERT INTO images (artwork_id, page, path, sha256) VALUES (?, 9, ?, ?)",
                (aid, os.path.join(src, "legacy-dup.png"), sha))
        legacy = scanner.scan_directory(src)
        t.check("scan 清理库内遗留的重复行", legacy["pruned_duplicates"] >= 1,
                "pruned_duplicates=%d" % legacy["pruned_duplicates"])

        # 文件被删掉 → 重新扫描应清理库里的孤儿记录
        os.remove(os.path.join(src, "2222222.png"))
        pruned = scanner.scan_directory(src)
        t.check("scan 清理已消失文件的记录", pruned["pruned_images"] >= 1,
                "pruned_images=%d" % pruned["pruned_images"])
        with database.get_db() as conn:
            t.equal("scan 被删文件不再出现在库里", _images_of(conn, 2222222), 0)


# ---------------- sync ----------------

def _illust(title="标题"):
    return {
        "title": title,
        "description": "描述",
        "author_id": 555,
        "author_name": "作者",
        "author_profile_image": "https://i.pximg.net/avatar.jpg",
        "create_date": "2020-01-01T00:00:00+09:00",
        "page_count": 1,
        "width": 100,
        "height": 200,
        "tags": [{"name": "tagA", "translated_name": "标签A"}],
    }


class _FakeClient:
    """按 pixiv_id 返回预设结果；值是 Exception 就抛出。"""

    def __init__(self, behavior):
        self._behavior = behavior
        self.calls = []

    def get_illust_detail(self, pixiv_id):
        self.calls.append(pixiv_id)
        result = self._behavior.get(pixiv_id)
        if isinstance(result, Exception):
            raise result
        return result


@contextlib.contextmanager
def _patched_sync(behavior):
    client = _FakeClient(behavior)
    saved_client = sync.get_pixiv_client
    saved_delay = os.environ.get("SYNC_DELAY_MS")
    sync.get_pixiv_client = lambda: client
    os.environ["SYNC_DELAY_MS"] = "0"
    try:
        yield client
    finally:
        sync.get_pixiv_client = saved_client
        if saved_delay is None:
            os.environ.pop("SYNC_DELAY_MS", None)
        else:
            os.environ["SYNC_DELAY_MS"] = saved_delay


def _add_pending(conn, pixiv_id):
    conn.execute(
        "INSERT INTO artworks (pixiv_id, author_name, first_seen) VALUES (?, 'a', '2020-01-01 00:00:00')",
        (pixiv_id,))


def _row(conn, pixiv_id):
    return conn.execute(
        "SELECT title, pixiv_status, last_synced, sync_error FROM artworks WHERE pixiv_id = ?",
        (pixiv_id,)).fetchone()


def _sync(t):
    # 成功：写回标题/状态，清掉 sync_error，并落 metadata json
    with _env() as root:
        with database.get_db() as conn:
            _add_pending(conn, 1001)
        with _patched_sync({1001: _illust("成功标题")}):
            result = sync.sync_metadata()
        t.equal("sync 成功计数", result["synced"], 1)
        with database.get_db() as conn:
            row = _row(conn, 1001)
            t.equal("sync 写回标题", row["title"], "成功标题")
            t.equal("sync 标记为 active", row["pixiv_status"], "active")
            t.check("sync 记录了同步时间", bool(row["last_synced"]))
            t.equal("sync 成功后清空 sync_error", row["sync_error"], None)
            t.equal("sync 建立了作者", conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0], 1)
            t.equal("sync 建立了标签关联", conn.execute("SELECT COUNT(*) FROM artwork_tags").fetchone()[0], 1)
        t.check("sync 落盘 metadata json",
                os.path.exists(os.path.join(root, "metadata", "1001.json")))

    # 网络失败：记 sync_error 但不动 pixiv_status，便于下轮重试
    with _env():
        with database.get_db() as conn:
            _add_pending(conn, 2001)
        with _patched_sync({2001: PixivNetworkError("网络炸了")}):
            result = sync.sync_metadata()
        t.equal("sync 网络失败计数", result["failed"], 1)
        with database.get_db() as conn:
            row = _row(conn, 2001)
            t.check("sync 网络失败写入 sync_error", "网络炸了" in (row["sync_error"] or ""),
                    "sync_error=%r" % row["sync_error"])
            t.equal("sync 网络失败不改 pixiv_status", row["pixiv_status"], "active")
            t.equal("sync 网络失败不算已同步", row["last_synced"], None)

    # 已删除：标 deleted 并写 last_synced，清 sync_error
    with _env():
        with database.get_db() as conn:
            _add_pending(conn, 3001)
        with _patched_sync({3001: PixivDeletedError("作品不存在")}):
            result = sync.sync_metadata()
        t.equal("sync 已删除计数", result["deleted"], 1)
        with database.get_db() as conn:
            row = _row(conn, 3001)
            t.equal("sync 已删除标记 pixiv_status", row["pixiv_status"], "deleted")
            t.check("sync 已删除写了 last_synced（用于 30 天复查）", bool(row["last_synced"]))
            t.equal("sync 已删除清空 sync_error", row["sync_error"], None)

    # 认证失败：记 auth_error 并**中止整批**（后续行不再请求）
    with _env():
        with database.get_db() as conn:
            _add_pending(conn, 4001)
            _add_pending(conn, 4002)
        with _patched_sync({4001: PixivAuthError("token 失效")}) as client:
            result = sync.sync_metadata()
        t.check("sync 认证失败回传 auth_error", "token 失效" in (result["auth_error"] or ""),
                "auth_error=%r" % result["auth_error"])
        t.equal("sync 认证失败后不再请求后续作品", client.calls, [4001])
        with database.get_db() as conn:
            t.equal("sync 认证失败不给后续行写 sync_error", _row(conn, 4002)["sync_error"], None)


# ---------------- download ----------------

class _FakeRaw:
    def __init__(self, data):
        self._buf = io.BytesIO(data)

    def read(self, n=-1):
        return self._buf.read(n)


class _FakeResp:
    def __init__(self, status=200, data=b"", location=None):
        self.status_code = status
        self.raw = _FakeRaw(data)
        self.headers = {}
        self.closed = False
        if location is not None:
            self.headers["location"] = location

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self, responses=None):
        self.calls = []
        self._responses = responses or {}

    def get(self, url, timeout=None, stream=False, allow_redirects=True):
        self.calls.append(url)
        return self._responses.get(url, _FakeResp(404))


def _download(t):
    with _env() as root:
        url = "https://i.pximg.net/img/1234567.jpg"
        illust = {
            "id": 1234567,
            "title": "作品",
            "page_count": 1,
            "meta_single_page": {"original_image_url": url},
        }
        author_root = os.path.join(root, "作者")
        session = _FakeSession({url: _FakeResp(200, b"JPEGDATA")})

        status, err = download.download_single_illust(session, illust, author_root, "作者")
        t.equal("download 成功返回 downloaded", status, "downloaded")
        t.equal("download 成功无错误信息", err, "")

        dest = os.path.join(author_root, "[作者] 1234567.jpg")
        t.check("download 图片已落盘", os.path.exists(dest), dest)
        if os.path.exists(dest):
            with open(dest, "rb") as handle:
                t.equal("download 文件内容正确", handle.read(), b"JPEGDATA")
        t.check("download 不残留 .part 临时文件",
                not any(name.endswith(".part") for name in os.listdir(author_root)),
                "dir=%r" % os.listdir(author_root))
        t.equal("download 确实发起了请求", session.calls, [url])

        # 已存在 → 跳过，且不再发请求
        session2 = _FakeSession({url: _FakeResp(200, b"JPEGDATA")})
        status2, _err2 = download.download_single_illust(session2, illust, author_root, "作者")
        t.equal("download 已完整则跳过", status2, "skipped")
        t.equal("download 跳过时不再发请求", session2.calls, [])

        # 非白名单地址 → 拒绝且不发请求（与 test_p1_hardening 的行为断言互为印证）。
        # 注意用**新的 pixiv_id**：_work_exists 的白名单门之前就会短路，同 id 会被判 skipped。
        bad = dict(illust, id=7654321,
                   meta_single_page={"original_image_url": "http://127.0.0.1:9/x.jpg"})
        session3 = _FakeSession()
        status3, err3 = download.download_single_illust(session3, bad, author_root, "作者")
        t.equal("download 白名单外地址返回 failed", status3, "failed")
        t.check("download 白名单外地址给出明确原因",
                "不在允许的域名范围内" in (err3 or ""), "err=%r" % (err3,))
        t.equal("download 白名单外地址不发起请求", session3.calls, [])


def run(t):
    _scanner(t)
    _sync(t)
    _download(t)
