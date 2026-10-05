import os
import re
import hashlib
from datetime import datetime
from dotenv import load_dotenv
from app import paths
from app.database import get_connection, init_db

load_dotenv(paths.ENV_FILE)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
# 长扫描按作品数分批提交，避免整个任务期间独占 SQLite 写锁、卡住 UI 的并发写
COMMIT_BATCH_SIZE = 200
PIXIV_ID_MULTI_PATTERN = re.compile(r"(\d{7,10})_p(\d+)", re.IGNORECASE)
PIXIV_ID_MULTI_NO_UNDERSCORE_PATTERN = re.compile(r"(\d{7,10})p(\d+)", re.IGNORECASE)
PIXIV_ID_SEARCH_PATTERN = re.compile(r"\d{7,10}")


def extract_pixiv_info(filename):
    match = PIXIV_ID_MULTI_PATTERN.search(filename)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = PIXIV_ID_MULTI_NO_UNDERSCORE_PATTERN.search(filename)
    if match:
        return int(match.group(1)), int(match.group(2))
    name_no_ext = os.path.splitext(filename)[0]
    matches = PIXIV_ID_SEARCH_PATTERN.findall(name_no_ext)
    if matches:
        return int(matches[-1]), 0
    return None, None


def compute_sha256(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_directory(source_dir, progress_callback=None, cancel_event=None):
    init_db()

    if not os.path.isdir(source_dir):
        return {"error": f"Directory not found: {source_dir}"}

    new_artworks = 0
    new_images = 0
    skipped = 0
    duplicates = 0

    image_files = []
    for root, dirs, files in os.walk(source_dir):
        if cancel_event and cancel_event.is_set():
            return {"cancelled": True, "new_artworks": new_artworks, "new_images": new_images}
        for filename in files:
            ext = os.path.splitext(filename)[1].lower()
            if ext in IMAGE_EXTENSIONS:
                image_files.append(os.path.join(root, filename))
                if progress_callback and len(image_files) % 100 == 0:
                    progress_callback("scan", len(image_files), None,
                                      f"正在扫描目录…已发现 {len(image_files)} 张图片")

    if cancel_event and cancel_event.is_set():
        return {"cancelled": True, "new_artworks": new_artworks, "new_images": new_images}

    grouped = {}
    for filepath in image_files:
        filename = os.path.basename(filepath)
        pixiv_id, page = extract_pixiv_info(filename)
        if pixiv_id is None:
            continue
        if pixiv_id not in grouped:
            grouped[pixiv_id] = []
        grouped[pixiv_id].append((filepath, page))

    conn = get_connection()
    try:
        total_works = len(grouped)
        for i, (pixiv_id, images) in enumerate(grouped.items()):
            if cancel_event and cancel_event.is_set():
                conn.commit()  # 取消时保留已完成的部分
                return {"cancelled": True, "new_artworks": new_artworks, "new_images": new_images}
            if progress_callback:
                progress_callback("import", i + 1, total_works, f"写入数据库…{i + 1}/{total_works}（PID {pixiv_id}）")
            images.sort(key=lambda x: x[1])

            existing = conn.execute(
                "SELECT id FROM artworks WHERE pixiv_id = ?", (pixiv_id,)
            ).fetchone()

            if existing:
                artwork_id = existing["id"]
            else:
                cursor = conn.execute(
                    "INSERT INTO artworks (pixiv_id, page_count, local_path, first_seen) VALUES (?, ?, ?, ?)",
                    (pixiv_id, len(images), images[0][0],
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                artwork_id = cursor.lastrowid
                new_artworks += 1

            conn.execute(
                "UPDATE artworks SET page_count = ? WHERE id = ?",
                (len(images), artwork_id),
            )

            for filepath, page in images:
                existing_img = conn.execute(
                    "SELECT id FROM images WHERE artwork_id = ? AND path = ?",
                    (artwork_id, filepath),
                ).fetchone()

                if existing_img:
                    skipped += 1
                    continue

                try:
                    sha = compute_sha256(filepath)
                except Exception:
                    sha = None

                if sha:
                    dup = conn.execute(
                        "SELECT id FROM images WHERE sha256 = ?", (sha,)
                    ).fetchone()
                    if dup:
                        duplicates += 1
                        continue

                conn.execute(
                    "INSERT INTO images (artwork_id, page, path, sha256) VALUES (?, ?, ?, ?)",
                    (artwork_id, page, filepath, sha),
                )
                new_images += 1

            # 分批提交：长扫描不再整个任务独占写锁，UI 的并发写无需等到任务结束
            if (i + 1) % COMMIT_BATCH_SIZE == 0:
                conn.commit()

        pruned_duplicates = _prune_duplicates(conn)
        prune_result = _prune_missing(conn, source_dir, image_files)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "total_files_scanned": len(image_files),
        "pixiv_artworks_found": len(grouped),
        "new_artworks": new_artworks,
        "new_images": new_images,
        "skipped": skipped,
        "duplicates": duplicates,
        "pruned_duplicates": pruned_duplicates,
        "pruned_images": prune_result["pruned_images"],
        "pruned_artworks": prune_result["pruned_artworks"],
        "source_dir": source_dir,
    }


def _prune_duplicates(conn):
    """Delete images whose content (sha256) already exists elsewhere in the DB."""
    dup_rows = conn.execute(
        """SELECT img.id, img.artwork_id
           FROM images AS img
           WHERE img.sha256 IS NOT NULL
             AND img.id > (SELECT MIN(id) FROM images AS i2
                           WHERE i2.sha256 = img.sha256)"""
    ).fetchall()
    for row in dup_rows:
        conn.execute("DELETE FROM images WHERE id = ?", (row["id"],))
    return len(dup_rows)


def _prune_missing(conn, source_dir, found_paths):
    """Delete DB records whose image files no longer exist in the source dir."""
    found_set = set(os.path.normcase(os.path.abspath(p)) for p in found_paths)
    source_root = os.path.normcase(os.path.abspath(source_dir))

    pruned_images = 0
    pruned_artworks = 0

    rows = conn.execute("SELECT id, artwork_id, path FROM images").fetchall()
    orphans = []
    for row in rows:
        path = os.path.normcase(os.path.abspath(row["path"]))
        if not path.startswith(source_root):
            continue
        if path not in found_set:
            orphans.append((row["id"], row["artwork_id"]))

    for img_id, artwork_id in orphans:
        conn.execute("DELETE FROM images WHERE id = ?", (img_id,))
        pruned_images += 1

    artworks = conn.execute(
        "SELECT id, pixiv_id FROM artworks WHERE id IN "
        "(SELECT DISTINCT artwork_id FROM images WHERE path IS NOT NULL)"
    ).fetchall()

    empty_artworks = conn.execute(
        """SELECT a.id, a.pixiv_id
           FROM artworks a
           WHERE NOT EXISTS (SELECT 1 FROM images i WHERE i.artwork_id = a.id)"""
    ).fetchall()

    for art in empty_artworks:
        conn.execute("DELETE FROM artwork_tags WHERE artwork_id = ?", (art["id"],))
        conn.execute("DELETE FROM artworks WHERE id = ?", (art["id"],))
        paths.remove_side_files(art["pixiv_id"])
        pruned_artworks += 1

    for art in artworks:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM images WHERE artwork_id = ?", (art["id"],)
        ).fetchone()[0]
        conn.execute(
            "UPDATE artworks SET page_count = ? WHERE id = ?", (remaining, art["id"])
        )

    return {"pruned_images": pruned_images, "pruned_artworks": pruned_artworks}


if __name__ == "__main__":
    source = os.getenv("IMAGE_SOURCE_DIR", "")
    if not source:
        print("Error: IMAGE_SOURCE_DIR not set in .env")
    else:
        result = scan_directory(source)
        for k, v in result.items():
            print(f"  {k}: {v}")