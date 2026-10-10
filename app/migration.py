"""数据迁移：把工具「刮削后的元数据」导出为可移植数据包，并在新工具目录导入还原。

导出内容（zip）：
    manifest.json   迁移清单：格式版本、导出时间、条目统计、原本地图片目录
    archive.db      数据库：标题 / 描述 / 标签 / 作者 / 收藏 / 图片索引等全部刮削结果
    thumbnails/     缩略图
    metadata/       每作品的元数据 JSON（含 avatars/ 头像缓存）

导入流程：校验清单与版本 → 严格校验 zip 成员路径（防 zip-slip）→ 备份现有库 →
解压到数据目录下的临时目录后原子替换 → 重建库 →（可选）把失效的图片绝对路径
按文件名重新关联到当前本地图片目录。

设计取舍：
- 图片文件本身不打包（体积大，且通常与原库分开存放）；导入后按需重新关联路径，
  这样「只搬元数据」也能在新目录直接看到完整图库，无需重新扫描或同步。
- 所有落盘路径在**调用时**从 paths / database 读取，便于测试替换数据目录。
"""
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime

from app import database
from app import logging_setup
from app import paths

logger = logging_setup.get_logger("pixiv_archive.migration")

# 数据包格式版本：导入时校验，避免误读未来版本或损坏的包
MIGRATION_FORMAT = 1
_MANIFEST = "manifest.json"
_DB_NAME = "archive.db"
# zip 内固定使用的顶层目录名（与运行时可配置的 THUMBNAIL_DIR / METADATA_DIR 解耦）
_THUMB_TOP = "thumbnails"
_META_TOP = "metadata"
# zip 内允许出现的顶层条目（其余一律拒绝，收紧攻击面）
_ALLOWED_TOP = {_MANIFEST, _DB_NAME, _THUMB_TOP, _META_TOP}
# 解压总大小上限，防 zip 炸弹（元数据/缩略图都是小文件，256MB 足够宽裕）
_MAX_TOTAL_BYTES = 256 * 1024 * 1024
# 清单上限：manifest 在校验解压总量之前读取，必须先单独限长，否则超大清单可绕过防护
_MANIFEST_MAX_BYTES = 1024 * 1024
# 数据包内数据库的最小合理体积（合法空库也有一个 4K 页），用于识别 0 字节/截断文件
_MIN_DB_BYTES = 512


class MigrationError(Exception):
    """带结构化信息的迁移错误，供接口层转成 {code, message, hint}。"""

    def __init__(self, code, message, hint=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


class _Cancelled(Exception):
    """用户中途取消（内部信号，不对外暴露）。"""


# ---------- 路径与统计 ----------

def _thumbnail_dir():
    return os.path.join(paths.DATA_DIR, paths.THUMBNAIL_DIR)


def _metadata_dir():
    return os.path.join(paths.DATA_DIR, paths.METADATA_DIR)


def _iter_files(root):
    """递归列出 root 下所有文件，返回相对路径（正斜杠分隔）。"""
    out = []
    if not os.path.isdir(root):
        return out
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            out.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return out


def summary():
    """当前库的条目统计与数据目录，供设置页展示。"""
    meta_files = [p for p in _iter_files(_metadata_dir()) if p.lower().endswith(".json")]
    result = {
        "data_dir": paths.DATA_DIR,
        "artworks": 0,
        "images": 0,
        "thumbnails": len(_iter_files(_thumbnail_dir())),
        "metadata": len(meta_files),
        "image_source_dir": (os.getenv("IMAGE_SOURCE_DIR", "") or "").strip(),
    }
    try:
        with database.get_db() as conn:
            result["artworks"] = conn.execute("SELECT COUNT(*) FROM artworks").fetchone()[0]
            result["images"] = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    except Exception as exc:  # 库还没建好时不应让设置页整体失败
        logger.warning("统计库信息失败：%s", exc)
    return result


# ---------- 导出 ----------

def _snapshot_db(dest_path):
    """用 SQLite 在线备份 API 生成一份一致快照（含 WAL 中尚未 checkpoint 的事务）。

    直接拷贝 archive.db 会漏掉还留在 `-wal` 里的已提交写入（导出的库、以及导入前的
    备份都会缺最新数据）；`Connection.backup()` 无需独占写锁即可把这些数据一并写进
    目标文件，是唯一稳妥的做法。
    """
    try:
        src = database.get_connection()
    except Exception as exc:
        raise MigrationError("DB_OPEN_FAILED", f"打开数据库失败：{exc}", "请确认数据目录可访问")
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    except sqlite3.Error as exc:
        raise MigrationError("DB_SNAPSHOT_FAILED", f"生成数据库快照失败：{exc}",
                             "请确认目标目录可写、磁盘空间充足")
    finally:
        src.close()


def export_data(dest_dir, progress_callback=None, cancel_event=None):
    """把数据库 + 缩略图 + 元数据打包成一个 zip，返回结果字典。"""
    dest_dir = (dest_dir or "").strip().strip('"') or paths.DATA_DIR
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as exc:
        raise MigrationError("EXPORT_DIR_INVALID", f"导出目录不可用：{dest_dir}", str(exc))

    db_path = database.DB_PATH
    if not os.path.exists(db_path):
        raise MigrationError("NO_DATABASE", "数据库不存在，暂无可导出的数据", "请先扫描图片并同步元数据")

    counts = {"artworks": 0, "images": 0}
    try:
        with database.get_db() as conn:
            counts["artworks"] = conn.execute("SELECT COUNT(*) FROM artworks").fetchone()[0]
            counts["images"] = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    except Exception as exc:
        logger.warning("导出前统计失败：%s", exc)

    thumb_rel = _iter_files(_thumbnail_dir())
    meta_rel = _iter_files(_metadata_dir())
    counts["thumbnails"] = len(thumb_rel)
    counts["metadata"] = len([p for p in meta_rel if p.lower().endswith(".json")])

    manifest = {
        "format": MIGRATION_FORMAT,
        "app": "pixiv-archive",
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_dir": (os.getenv("IMAGE_SOURCE_DIR", "") or "").strip(),
        "counts": counts,
    }

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_path = os.path.join(dest_dir, f"pixiv-archive-data-{ts}.zip")
    # 数据库快照写在目标目录（与 zip 同盘）：生成后随即打包，最后删除
    snapshot_path = os.path.join(dest_dir, f".pa-snapshot-{ts}.db")

    total = 2 + len(thumb_rel) + len(meta_rel)
    done = 0

    def step(message):
        nonlocal done
        done += 1
        if progress_callback:
            progress_callback("export", done, total, message)
        return bool(cancel_event and cancel_event.is_set())

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if step("写入清单…"):
                raise _Cancelled()
            zf.writestr(_MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))

            if step("写入数据库…"):
                raise _Cancelled()
            _snapshot_db(snapshot_path)
            zf.write(snapshot_path, _DB_NAME)

            thumb_root = _thumbnail_dir()
            for rel in thumb_rel:
                if step(f"打包缩略图…{rel}"):
                    raise _Cancelled()
                zf.write(os.path.join(thumb_root, rel.replace("/", os.sep)), f"{_THUMB_TOP}/{rel}")

            meta_root = _metadata_dir()
            for rel in meta_rel:
                if step(f"打包元数据…{rel}"):
                    raise _Cancelled()
                zf.write(os.path.join(meta_root, rel.replace("/", os.sep)), f"{_META_TOP}/{rel}")
    except _Cancelled:
        _safe_remove(zip_path)
        return {"cancelled": True, "path": None, "counts": counts}
    except MigrationError:
        _safe_remove(zip_path)
        raise
    except OSError as exc:
        _safe_remove(zip_path)
        raise MigrationError("EXPORT_FAILED", f"导出失败：{exc}", "请检查目标目录是否可写、磁盘是否已满")
    finally:
        _safe_remove(snapshot_path)

    return {
        "cancelled": False,
        "path": zip_path,
        "size": os.path.getsize(zip_path),
        "counts": counts,
    }


# ---------- 导入 ----------

def import_data(zip_path, remap=True, progress_callback=None, cancel_event=None):
    """从 zip 数据包还原数据库、缩略图与元数据；可选重新关联本地图片路径。"""
    zip_path = (zip_path or "").strip().strip('"')
    if not zip_path:
        raise MigrationError("NO_PACKAGE", "未提供数据包路径", "请选择由本工具导出的 .zip 文件")
    if not os.path.isfile(zip_path):
        raise MigrationError("PACKAGE_NOT_FOUND", f"找不到数据包：{zip_path}", "请确认路径是否正确")
    if not zipfile.is_zipfile(zip_path):
        raise MigrationError("NOT_A_ZIP", "所选文件不是有效的 zip 数据包", "请选择由本工具导出的 .zip 文件")

    os.makedirs(paths.DATA_DIR, exist_ok=True)
    # 暂存目录放在数据目录内：与目标同盘，后续 os.replace 才是原子操作
    staging = tempfile.mkdtemp(prefix=".pa-import-", dir=paths.DATA_DIR)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            _read_manifest(zf)  # 校验清单存在性、来源与格式版本
            members = _validated_members(zf)
            total = len(members)
            total_bytes = 0
            for i, (info, rel) in enumerate(members):
                if cancel_event and cancel_event.is_set():
                    raise _Cancelled()
                if progress_callback:
                    progress_callback("import", i + 1, total, f"解压数据…{i + 1}/{total}")
                total_bytes += _extract(zf, info, staging, rel, _MAX_TOTAL_BYTES - total_bytes)

        # 换库前先校验暂存库合法、且确是本工具的库：避免换上去之后才发现不可用
        _validate_staged_db(os.path.join(staging, _DB_NAME))
        applied = _apply(staging)
        try:
            # 还原的库可能来自旧版本：跑一遍建表 / 列迁移，确保结构最新
            database.init_db()
        except Exception as exc:
            _restore_backup(applied.get("backup"))
            raise MigrationError("DB_MIGRATE_FAILED", f"数据库结构迁移失败：{exc}",
                                 "该数据包可能来自不兼容的版本，已回滚到导入前的库")
    except _Cancelled:
        return {"cancelled": True}
    except zipfile.BadZipFile as exc:
        raise MigrationError("CORRUPT_PACKAGE", "数据包已损坏，无法读取", str(exc))
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    result = {
        "cancelled": False,
        # 统计以**实际还原后的库**为准，不采信清单里可能被篡改的 counts
        "counts": _db_counts(),
        "backup": applied.get("backup"),
    }
    if remap:
        if progress_callback:
            progress_callback("remap", None, None, "重新关联本地图片…")
        result["remap"] = relink_images()
    return result


def _validate_staged_db(path):
    """换库前校验数据包内的 archive.db：非空、是合法 SQLite、且是本工具的库。

    这一步是导入安全的关键闸门：若不先校验就替换，损坏 / 空 / 异库的数据包会把
    可用性良好的当前库换掉，落得「导入报成功但图库空了」或工具直接起不来。
    """
    if not os.path.exists(path) or os.path.getsize(path) < _MIN_DB_BYTES:
        raise MigrationError("INVALID_DATABASE", "数据包内的数据库为空或过小，已中止导入",
                             "请重新导出数据包后重试")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise MigrationError("INVALID_DATABASE", "数据包内的数据库已损坏，已中止导入",
                                     "请重新导出数据包后重试")
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artworks'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise MigrationError("INVALID_DATABASE", f"数据包内的数据库无法读取：{exc}",
                             "请确认数据包来源是否可信")
    if not has_table:
        raise MigrationError("INVALID_DATABASE", "数据包内的数据库不是本工具的库，已中止导入",
                             "请选择由本工具导出的数据包")


def _restore_backup(backup):
    """用备份回滚当前库（导入中途失败时使用）。"""
    if not backup or not os.path.exists(backup):
        return
    try:
        os.replace(backup, database.DB_PATH)
    except OSError as exc:
        logger.error("回滚数据库失败（备份 %s）：%s", backup, exc)


def _db_counts():
    """读取当前库的条目统计（作品 / 图片 / 缩略图 / 元数据）。"""
    counts = {"artworks": 0, "images": 0, "thumbnails": 0, "metadata": 0}
    try:
        with database.get_db() as conn:
            counts["artworks"] = conn.execute("SELECT COUNT(*) FROM artworks").fetchone()[0]
            counts["images"] = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    except Exception as exc:
        logger.warning("统计还原后的库失败：%s", exc)
    counts["thumbnails"] = len(_iter_files(_thumbnail_dir()))
    counts["metadata"] = len([p for p in _iter_files(_metadata_dir()) if p.lower().endswith(".json")])
    return counts


def _read_manifest(zf):
    try:
        info = zf.getinfo(_MANIFEST)
    except KeyError:
        raise MigrationError("INVALID_PACKAGE", "数据包缺少清单文件，可能不是本工具导出的",
                             "请选择由本工具导出的 .zip 数据包")
    # 清单在校验解压总量之前读取，必须先单独限长，否则超大清单可绕过 zip 炸弹防护
    if info.file_size > _MANIFEST_MAX_BYTES:
        raise MigrationError("PACKAGE_TOO_LARGE", "数据包清单异常过大，已中止",
                             "请确认数据包来源是否可信")
    with zf.open(info) as fh:
        raw = fh.read(_MANIFEST_MAX_BYTES + 1)
    if len(raw) > _MANIFEST_MAX_BYTES:
        raise MigrationError("PACKAGE_TOO_LARGE", "数据包清单异常过大，已中止",
                             "请确认数据包来源是否可信")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise MigrationError("INVALID_MANIFEST", "数据包清单损坏，无法解析", str(exc))
    if not isinstance(manifest, dict) or manifest.get("app") != "pixiv-archive":
        raise MigrationError("INVALID_PACKAGE", "数据包不是本工具导出的数据包",
                             "请选择由本工具导出的 .zip 数据包")
    fmt = manifest.get("format")
    if fmt != MIGRATION_FORMAT:
        raise MigrationError(
            "VERSION_MISMATCH",
            f"数据包格式版本不兼容（数据包 {fmt}，当前支持 {MIGRATION_FORMAT}）",
            "请使用匹配版本的工具重新导出数据包",
        )
    return manifest


def _safe_member(name):
    """把 zip 内条目名规范成安全的相对路径；不安全则返回 None。

    拒绝：空名、NUL、绝对路径、盘符、`..` 上跳、不在白名单内的顶层条目。
    """
    if not name or "\x00" in name:
        return None
    n = name.replace("\\", "/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return None
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    # 顶层名按小写规范化：Windows 文件系统不区分大小写，避免 `Archive.db` 绕过白名单
    top = parts[0].lower()
    if top not in _ALLOWED_TOP:
        return None
    parts[0] = top
    return "/".join(parts)


def _validated_members(zf):
    members = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        rel = _safe_member(info.filename)
        if rel is None:
            raise MigrationError("UNSAFE_PACKAGE", f"数据包内含有非法路径条目：{info.filename}",
                                 "数据包可能被篡改，已中止导入")
        members.append((info, rel))
    if not any(rel == _DB_NAME for _info, rel in members):
        raise MigrationError("INVALID_PACKAGE", "数据包内缺少数据库文件",
                             "请选择由本工具导出的完整数据包")
    return members


def _within(root, target):
    root = os.path.abspath(root)
    target = os.path.abspath(target)
    return target == root or target.startswith(root + os.sep)


def _extract(zf, info, staging, rel, limit):
    """解压单个成员到暂存目录，返回实际写入字节数。

    `limit` 是本文件允许写入的上限（已扣除此前累计量）。刻意按**实际解压字节**
    计数，而不是相信 zip 里声明的 `file_size`——声明值可被伪造，据此设限会被 zip
    炸弹绕过。
    """
    dest = os.path.join(staging, *rel.split("/"))
    # 二次确认落点仍在暂存目录内（纵深防御，防极端路径绕过）
    if not _within(staging, dest):
        raise MigrationError("UNSAFE_PACKAGE", f"数据包路径越界：{info.filename}",
                             "数据包可能被篡改，已中止导入")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    written = 0
    with zf.open(info) as src, open(dest, "wb") as out:
        while True:
            chunk = src.read(65536)
            if not chunk:
                break
            out.write(chunk)
            written += len(chunk)
            if written > limit:
                raise MigrationError("PACKAGE_TOO_LARGE", "数据包解压后过大，已中止",
                                     "请确认数据包来源是否可信")
    return written


def _apply(staging):
    """把暂存目录中的库与边车文件替换到数据目录（先对旧库做一致快照备份）。"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    db_target = database.DB_PATH
    backup = None
    if os.path.exists(db_target):
        backup = f"{db_target}.bak-{ts}"
        # 用在线备份 API 生成一致快照（含 WAL 中未 checkpoint 的事务）
        _snapshot_db(backup)
    # 清掉与新库不匹配的 WAL/SHM，避免 SQLite 把旧日志套到新库上
    for suffix in ("-wal", "-shm"):
        _safe_remove(db_target + suffix)

    # 直接 os.replace 覆盖：Windows 上是原子替换，没有「目标文件缺失」的窗口，
    # 并发读请求最多读到旧库或新库，不会读到「没有库」。
    try:
        os.replace(os.path.join(staging, _DB_NAME), db_target)
    except OSError as exc:
        # 替换失败：用备份回滚，避免只剩一个 .bak 而没有可用库
        _restore_backup(backup)
        raise MigrationError("APPLY_FAILED", f"写入数据库失败：{exc}",
                             "请关闭可能占用 archive.db 的程序后重试")

    _merge_tree(os.path.join(staging, _THUMB_TOP), _thumbnail_dir())
    _merge_tree(os.path.join(staging, _META_TOP), _metadata_dir())
    return {"backup": backup}


def _merge_tree(src_root, dst_root):
    """把 src_root 下的文件并入 dst_root（覆盖同名文件，保留目标已有文件）。"""
    if not os.path.isdir(src_root):
        return
    for rel in _iter_files(src_root):
        src = os.path.join(src_root, rel.replace("/", os.sep))
        dst = os.path.join(dst_root, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            os.replace(src, dst)
        except OSError:
            shutil.copy2(src, dst)


# ---------- 重新关联图片路径 ----------

def relink_images(source_dir=None, progress_callback=None):
    """把库中失效的图片绝对路径，按文件名重新关联到当前本地图片目录。

    迁移到新目录/新机器后，数据库里存的旧绝对路径可能全部失效；只要文件名
    （Pixiv 下载命名通常唯一）能在当前图片目录里找到，就无需重新扫描。
    """
    source_dir = (source_dir or os.getenv("IMAGE_SOURCE_DIR", "") or "").strip().strip('"')
    if not source_dir or not os.path.isdir(source_dir):
        return {"ok": False, "reason": "no_source_dir", "relinked": 0, "missing": 0,
                "source_dir": source_dir}

    # basename(小写) -> [路径...]：同名多份时无法确定是哪一张，宁可不动也不猜错
    index = {}
    for dirpath, _dirnames, filenames in os.walk(source_dir):
        for name in filenames:
            index.setdefault(name.lower(), []).append(os.path.join(dirpath, name))

    relinked = 0
    missing = 0
    ambiguous = 0
    with database.get_db() as conn:
        rows = conn.execute("SELECT id, path FROM images").fetchall()
        total = len(rows)
        for i, row in enumerate(rows):
            if progress_callback and i % 200 == 0:
                progress_callback("remap", i + 1, total, f"重新关联…{i + 1}/{total}")
            path = row["path"] or ""
            if path and os.path.exists(path):
                continue
            matches = index.get(os.path.basename(path).lower())
            if not matches:
                missing += 1
            elif len(matches) > 1:
                ambiguous += 1
            else:
                conn.execute("UPDATE images SET path = ? WHERE id = ?", (matches[0], row["id"]))
                relinked += 1
        if relinked:
            # 作品的 local_path 同步指向其第 0 页（用于「在文件夹中打开」）
            conn.execute(
                """UPDATE artworks SET local_path = (
                       SELECT i.path FROM images i
                       WHERE i.artwork_id = artworks.id
                       ORDER BY i.page LIMIT 1)
                   WHERE id IN (SELECT artwork_id FROM images)"""
            )
    return {"ok": True, "relinked": relinked, "missing": missing,
            "ambiguous": ambiguous, "source_dir": source_dir}


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
