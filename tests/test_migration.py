"""数据迁移：导出 / 导入数据包 + zip-slip 防护 + 图片路径重关联。

需求要点：
- 「只搬工具刮削后的元数据」：数据库 + 缩略图 + 元数据 JSON 打包成一个 zip，
  在新工具目录导入即可还原，无需重新扫描或同步；
- 导入必须防 zip-slip（拒绝绝对路径 / `..` / 白名单外条目）；
- 图片库换目录后，能按文件名把失效的绝对路径重新关联。

全部离线：临时数据目录 + 临时库 + 手写 zip，不联网、不起服务。
"""
import contextlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile

import harness as h

h.bootstrap()
from app import database  # noqa: E402
from app import migration  # noqa: E402
from app import paths  # noqa: E402

TITLE = "数据迁移（导出 / 导入 / zip-slip / 重关联）"
NEEDS_SERVER = False


@contextlib.contextmanager
def _env():
    """把 DB 与数据目录指到临时目录，退出时还原。"""
    root = tempfile.mkdtemp(prefix="pa-migrate-")
    saved_db = database.DB_PATH
    saved_dir = paths.DATA_DIR
    saved_src = os.environ.get("IMAGE_SOURCE_DIR")
    database.DB_PATH = os.path.join(root, "archive.db")
    paths.DATA_DIR = root
    os.environ.pop("IMAGE_SOURCE_DIR", None)
    try:
        database.init_db()
        yield root
    finally:
        database.DB_PATH = saved_db
        paths.DATA_DIR = saved_dir
        if saved_src is None:
            os.environ.pop("IMAGE_SOURCE_DIR", None)
        else:
            os.environ["IMAGE_SOURCE_DIR"] = saved_src


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def _seed(root, source_dir):
    """造一份最小数据：1 个作品 / 1 张图片 + 缩略图 + 元数据（含头像子目录）。"""
    _write(os.path.join(root, "thumbnails", "1001.jpg"), b"THUMB")
    with open(_write(os.path.join(root, "metadata", "1001.json"), b"{}"), "w", encoding="utf-8") as f:
        json.dump({"pixiv_id": 1001, "title": "标题"}, f, ensure_ascii=False)
    _write(os.path.join(root, "metadata", "avatars", "a.png"), b"AVATAR")

    img = os.path.join(source_dir, "作者", "[作者] 1001_p0.jpg")
    _write(img, b"IMAGE-DATA")
    with database.get_db() as conn:
        cur = conn.execute(
            "INSERT INTO artworks (pixiv_id, title, author_name, page_count, local_path, first_seen) "
            "VALUES (1001, '标题', '作者', 1, ?, '2020-01-01 00:00:00')", (img,))
        conn.execute(
            "INSERT INTO images (artwork_id, page, path, sha256) VALUES (?, 0, ?, 'sha')",
            (cur.lastrowid, img))
    return img


def _wipe_library(root):
    """清空库与边车文件，模拟一个「全新的工具文件夹」。"""
    for suffix in ("", "-wal", "-shm"):
        p = database.DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)
    shutil.rmtree(os.path.join(root, "thumbnails"), ignore_errors=True)
    shutil.rmtree(os.path.join(root, "metadata"), ignore_errors=True)
    database.init_db()


def _expect_error(t, name, zip_path, code):
    got = None
    try:
        migration.import_data(zip_path)
    except migration.MigrationError as exc:
        got = exc.code
    t.equal(name, got, code)


# ---------------- 导出 / 导入往返 ----------------

def _roundtrip(t):
    with _env() as root:
        src = os.path.join(root, "src")
        _seed(root, src)

        dest = os.path.join(root, "out")
        res = migration.export_data(dest)
        t.check("导出生成 zip", bool(res.get("path")) and os.path.exists(res["path"]),
                "path=%r" % res.get("path"))
        t.equal("导出统计作品数", res["counts"]["artworks"], 1)
        t.equal("导出统计缩略图数", res["counts"]["thumbnails"], 1)
        t.equal("导出统计元数据数", res["counts"]["metadata"], 1)

        _wipe_library(root)
        with database.get_db() as conn:
            t.equal("清空后库为空", conn.execute("SELECT COUNT(*) FROM artworks").fetchone()[0], 0)

        # 图片仍在原处：导入 + 重关联后应完全可用
        os.environ["IMAGE_SOURCE_DIR"] = src
        result = migration.import_data(res["path"], remap=True)
        t.check("导入未取消", not result.get("cancelled"))

        with database.get_db() as conn:
            t.equal("导入还原作品", conn.execute("SELECT COUNT(*) FROM artworks").fetchone()[0], 1)
            t.equal("导入还原图片", conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 1)
            t.equal("导入还原标题",
                    conn.execute("SELECT title FROM artworks WHERE pixiv_id = 1001").fetchone()[0], "标题")
        t.check("导入还原缩略图", os.path.exists(os.path.join(root, "thumbnails", "1001.jpg")))
        t.check("导入还原元数据 JSON", os.path.exists(os.path.join(root, "metadata", "1001.json")))
        t.check("导入还原头像缓存", os.path.exists(os.path.join(root, "metadata", "avatars", "a.png")))
        t.check("导入后原库自动备份",
                bool(result.get("backup")) and os.path.exists(result["backup"]),
                "backup=%r" % result.get("backup"))
        if result.get("backup"):
            conn_b = sqlite3.connect(result["backup"])
            try:
                has_table = conn_b.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='artworks'"
                ).fetchone()[0] == 1
            finally:
                conn_b.close()
            t.check("备份是可独立打开的完整库（在线快照，非裸拷贝）", has_table)

        # 幂等：同一数据包重复导入应再次成功（覆盖式还原）
        result2 = migration.import_data(res["path"], remap=False)
        t.check("重复导入成功", not result2.get("cancelled"))


# ---------------- zip-slip 防护 ----------------

def _zipslip(t):
    with _env() as root:
        bad = os.path.join(root, "bad.zip")
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"app": "pixiv-archive", "format": 1}))
            zf.writestr("archive.db", b"")
            zf.writestr("../evil.txt", b"pwned")
        _expect_error(t, "zip-slip（../）被拒绝", bad, "UNSAFE_PACKAGE")
        t.check("zip-slip 未写出越界文件",
                not os.path.exists(os.path.join(root, "evil.txt")))

        for name, member in (
            ("绝对路径被拒绝", "/etc/passwd"),
            ("盘符路径被拒绝", "C:/Windows/system32/x.dll"),
            ("白名单外顶层被拒绝", "other/secret.txt"),
        ):
            p = os.path.join(root, "bad2.zip")
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("manifest.json", json.dumps({"app": "pixiv-archive", "format": 1}))
                zf.writestr("archive.db", b"")
                zf.writestr(member, b"x")
            _expect_error(t, name, p, "UNSAFE_PACKAGE")


# ---------------- 清单校验 ----------------

def _manifest_checks(t):
    with _env() as root:
        p1 = os.path.join(root, "no-manifest.zip")
        with zipfile.ZipFile(p1, "w") as zf:
            zf.writestr("archive.db", b"")
        _expect_error(t, "缺清单被拒绝", p1, "INVALID_PACKAGE")

        p2 = os.path.join(root, "no-db.zip")
        with zipfile.ZipFile(p2, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"app": "pixiv-archive", "format": 1}))
            zf.writestr("thumbnails/a.jpg", b"x")
        _expect_error(t, "缺数据库被拒绝", p2, "INVALID_PACKAGE")

        p3 = os.path.join(root, "ver.zip")
        with zipfile.ZipFile(p3, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"app": "pixiv-archive", "format": 999}))
            zf.writestr("archive.db", b"")
        _expect_error(t, "版本不匹配被拒绝", p3, "VERSION_MISMATCH")

        p4 = _write(os.path.join(root, "notzip.zip"), b"not a zip at all")
        _expect_error(t, "非 zip 文件被拒绝", p4, "NOT_A_ZIP")

        _expect_error(t, "文件不存在报错", os.path.join(root, "nope.zip"), "PACKAGE_NOT_FOUND")


# ---------------- 解压大小上限（防 zip 炸弹） ----------------

def _size_limit(t):
    with _env() as root:
        src = os.path.join(root, "src2")
        _seed(root, src)
        z = migration.export_data(os.path.join(root, "out2"))["path"]

        saved = migration._MAX_TOTAL_BYTES
        migration._MAX_TOTAL_BYTES = 16  # 远小于包内实际内容
        try:
            _expect_error(t, "解压超限被拒绝（按实际字节）", z, "PACKAGE_TOO_LARGE")
        finally:
            migration._MAX_TOTAL_BYTES = saved


# ---------------- 换库前的数据库校验（失败必须不破坏当前库） ----------------

def _db_guard(t):
    with _env() as root:
        src = os.path.join(root, "src3")
        _seed(root, src)
        with database.get_db() as conn:
            before = conn.execute("SELECT COUNT(*) FROM artworks").fetchone()[0]
        t.equal("起始库有 1 个作品", before, 1)

        p1 = os.path.join(root, "empty-db.zip")
        with zipfile.ZipFile(p1, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"app": "pixiv-archive", "format": 1}))
            zf.writestr("archive.db", b"")
        _expect_error(t, "空数据库被拒绝", p1, "INVALID_DATABASE")

        p2 = os.path.join(root, "corrupt-db.zip")
        with zipfile.ZipFile(p2, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"app": "pixiv-archive", "format": 1}))
            zf.writestr("archive.db", b"SQLite format 3\x00" + b"\x00" * 2000)
        _expect_error(t, "损坏数据库被拒绝", p2, "INVALID_DATABASE")

        foreign = os.path.join(root, "foreign.db")
        c = sqlite3.connect(foreign)
        c.execute("CREATE TABLE other (x)")
        c.commit()
        c.close()
        p3 = os.path.join(root, "foreign.zip")
        with zipfile.ZipFile(p3, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"app": "pixiv-archive", "format": 1}))
            zf.write(foreign, "archive.db")
        _expect_error(t, "异库（非本工具的库）被拒绝", p3, "INVALID_DATABASE")

        with database.get_db() as conn:
            after = conn.execute("SELECT COUNT(*) FROM artworks").fetchone()[0]
        t.equal("导入失败后当前库原封不动", after, before)


# ---------------- 统计以实际库为准（不采信清单） ----------------

def _counts_truth(t):
    with _env() as root:
        src = os.path.join(root, "src4")
        _seed(root, src)
        z = migration.export_data(os.path.join(root, "out4"))["path"]

        tampered = os.path.join(root, "tampered.zip")
        with zipfile.ZipFile(z, "r") as zin, zipfile.ZipFile(tampered, "w") as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "manifest.json":
                    m = json.loads(data.decode("utf-8"))
                    m["counts"] = {"artworks": 999, "images": 999,
                                   "thumbnails": 999, "metadata": 999}
                    data = json.dumps(m).encode("utf-8")
                zout.writestr(item, data)

        _wipe_library(root)
        res = migration.import_data(tampered, remap=False)
        t.equal("统计以实际还原的库为准（不采信清单）", res["counts"]["artworks"], 1)


# ---------------- 图片路径重关联 ----------------

def _relink(t):
    with _env() as root:
        old_src = os.path.join(root, "old")
        img_old = os.path.join(old_src, "[作者] 2001_p0.jpg")
        _write(img_old, b"IMG")
        with database.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO artworks (pixiv_id, title, page_count, local_path, first_seen) "
                "VALUES (2001, 't', 1, ?, '2020-01-01 00:00:00')", (img_old,))
            conn.execute(
                "INSERT INTO images (artwork_id, page, path) VALUES (?, 0, ?)",
                (cur.lastrowid, img_old))

        # 模拟「图片库搬到新目录」：旧路径失效，新目录下有同名文件
        os.remove(img_old)
        new_src = os.path.join(root, "new")
        img_new = os.path.join(new_src, "[作者] 2001_p0.jpg")
        _write(img_new, b"IMG")
        os.environ["IMAGE_SOURCE_DIR"] = new_src

        r = migration.relink_images()
        t.check("重关联返回 ok", r.get("ok") is True)
        t.equal("重关联成功数", r["relinked"], 1)
        with database.get_db() as conn:
            t.equal("图片路径指向新目录",
                    conn.execute("SELECT path FROM images").fetchone()[0], img_new)
            t.equal("作品 local_path 同步更新",
                    conn.execute("SELECT local_path FROM artworks WHERE pixiv_id = 2001").fetchone()[0],
                    img_new)

        os.environ.pop("IMAGE_SOURCE_DIR", None)
        r2 = migration.relink_images()
        t.equal("无图片目录时不误报", r2.get("reason"), "no_source_dir")


def _relink_ambiguous(t):
    with _env() as root:
        old = os.path.join(root, "gone", "[作者] 3001_p0.jpg")
        with database.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO artworks (pixiv_id, title, page_count, local_path, first_seen) "
                "VALUES (3001, 't', 1, ?, '2020-01-01 00:00:00')", (old,))
            conn.execute("INSERT INTO images (artwork_id, page, path) VALUES (?, 0, ?)",
                         (cur.lastrowid, old))

        new_src = os.path.join(root, "amb")
        _write(os.path.join(new_src, "a", "[作者] 3001_p0.jpg"), b"A")
        _write(os.path.join(new_src, "b", "[作者] 3001_p0.jpg"), b"B")
        os.environ["IMAGE_SOURCE_DIR"] = new_src

        r = migration.relink_images()
        t.equal("同名多份不重关联（避免指错图）", r["relinked"], 0)
        t.equal("同名多份记为歧义", r["ambiguous"], 1)


def run(t):
    _roundtrip(t)
    _zipslip(t)
    _manifest_checks(t)
    _size_limit(t)
    _db_guard(t)
    _counts_truth(t)
    _relink(t)
    _relink_ambiguous(t)
