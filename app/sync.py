import os
import json
import time
from datetime import datetime
from dotenv import load_dotenv
from app import paths
from app.database import get_db, init_db
from app.pixiv import (
    get_pixiv_client,
    PixivDeletedError,
    PixivAuthError,
    PixivNetworkError,
)

load_dotenv(paths.ENV_FILE)

METADATA_DIR = os.getenv("METADATA_DIR", "metadata")

# 已删除作品的复查间隔（天）：标记 deleted 时写入了 last_synced，
# 只有距上次确认超过该天数的 deleted 作品才会被再次请求。
_DELETED_RECHECK_DAYS = 30


def _select_pending_rows(conn, specific_pixiv_id=None):
    """选出本轮需要同步的作品行（id, pixiv_id）。

    - 指定 specific_pixiv_id：只取该作品，无条件同步。
    - 否则取：从未同步（last_synced IS NULL）、缺标题（title IS NULL）、
      上次同步失败（sync_error IS NOT NULL，每轮都重试），以及已删除但
      距上次确认已超过 _DELETED_RECHECK_DAYS 天的作品。
    """
    if specific_pixiv_id:
        return conn.execute(
            "SELECT id, pixiv_id FROM artworks WHERE pixiv_id = ?",
            (specific_pixiv_id,),
        ).fetchall()

    return conn.execute(
        "SELECT id, pixiv_id FROM artworks "
        "WHERE title IS NULL OR last_synced IS NULL "
        "   OR sync_error IS NOT NULL "
        "   OR (pixiv_status = 'deleted' AND last_synced < datetime('now', ?))",
        (f"-{_DELETED_RECHECK_DAYS} days",),
    ).fetchall()


def sync_metadata(specific_pixiv_id=None, progress_callback=None, cancel_event=None):
    init_db()
    client = get_pixiv_client()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 风控限速：默认 800ms，设置里可调（SYNC_DELAY_MS，0=不限速）
    _d_str = (os.getenv("SYNC_DELAY_MS", "") or "").strip()
    delay_ms = int(_d_str) if _d_str.isdigit() else 800
    delay_ms = max(0, min(delay_ms, 10000))

    with get_db() as conn:
        rows = _select_pending_rows(conn, specific_pixiv_id)

        total = len(rows)
        results = {
            "synced": 0,
            "failed": 0,
            "deleted": 0,
            "cancelled": False,
            "auth_error": None,
            "details": [],
        }

        for idx, row in enumerate(rows):
            artwork_id = row["id"]
            pixiv_id = row["pixiv_id"]

            if cancel_event and cancel_event.is_set():
                results["cancelled"] = True
                break

            if progress_callback:
                progress_callback(
                    "sync", idx + 1, total,
                    f"同步元数据…{idx + 1}/{total}（PID {pixiv_id}）",
                )

            # 每次请求前按间隔限速（0.1s 分片，随时可取消）
            if delay_ms > 0:
                remain = delay_ms / 1000.0
                while remain > 1e-9:
                    if cancel_event is not None and cancel_event.is_set():
                        results["cancelled"] = True
                        break
                    time.sleep(0.1 if remain >= 0.1 else remain)
                    remain -= 0.1
                if results["cancelled"]:
                    break

            try:
                illust_data = client.get_illust_detail(pixiv_id)
            except PixivAuthError as e:
                # 认证失败：继续请求也会失败，中止整批并回传原因
                results["auth_error"] = str(e)
                results["failed"] += 1
                results["details"].append(
                    {"pixiv_id": pixiv_id, "status": "failed", "error": str(e)}
                )
                conn.execute(
                    "UPDATE artworks SET sync_error = ? WHERE id = ?",
                    (str(e)[:300], artwork_id),
                )
                break
            except PixivNetworkError as e:
                # 网络失败：不改作品状态，记录原因，下次同步自动重试
                results["failed"] += 1
                results["details"].append(
                    {"pixiv_id": pixiv_id, "status": "failed", "error": str(e)}
                )
                conn.execute(
                    "UPDATE artworks SET sync_error = ? WHERE id = ?",
                    (str(e)[:300], artwork_id),
                )
                continue
            except PixivDeletedError as e:
                # 仅当 Pixiv 明确返回“作品已删除/不存在”时才标记 deleted
                conn.execute(
                    "UPDATE artworks SET pixiv_status = 'deleted', last_synced = ?, sync_error = NULL WHERE id = ?",
                    (now_str, artwork_id),
                )
                results["deleted"] += 1
                results["details"].append(
                    {"pixiv_id": pixiv_id, "status": "deleted", "error": str(e)}
                )
                continue

            author_id = _upsert_author(conn, illust_data)
            _upsert_tags(conn, artwork_id, illust_data.get("tags", []))

            conn.execute(
                """UPDATE artworks SET
                    title = ?, description = ?, author_id = ?, author_name = ?,
                    create_date = ?, page_count = ?, width = ?, height = ?,
                    pixiv_status = 'active', last_synced = ?, sync_error = NULL
                WHERE id = ?""",
                (
                    illust_data["title"],
                    illust_data["description"],
                    author_id,
                    illust_data["author_name"],
                    illust_data["create_date"],
                    illust_data["page_count"],
                    illust_data["width"],
                    illust_data["height"],
                    now_str,
                    artwork_id,
                ),
            )

            _save_metadata_json(pixiv_id, illust_data)
            results["synced"] += 1
            results["details"].append({"pixiv_id": pixiv_id, "status": "synced"})

        return results


def _upsert_author(conn, illust_data):
    existing = conn.execute(
        "SELECT id FROM authors WHERE pixiv_user_id = ?",
        (illust_data["author_id"],),
    ).fetchone()

    if existing:
        return existing["id"]

    cursor = conn.execute(
        "INSERT INTO authors (pixiv_user_id, name, profile_image) VALUES (?, ?, ?)",
        (
            illust_data["author_id"],
            illust_data["author_name"],
            illust_data["author_profile_image"],
        ),
    )
    return cursor.lastrowid


def _upsert_tags(conn, artwork_id, tags):
    conn.execute("DELETE FROM artwork_tags WHERE artwork_id = ?", (artwork_id,))
    for tag_data in tags:
        name = tag_data["name"]
        translated = tag_data.get("translated_name") or ""
        existing = conn.execute(
            "SELECT id FROM tags WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            tag_id = existing["id"]
        else:
            cursor = conn.execute(
                "INSERT INTO tags (name, translated_name) VALUES (?, ?)",
                (name, translated if translated else None),
            )
            tag_id = cursor.lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO artwork_tags (artwork_id, tag_id) VALUES (?, ?)",
            (artwork_id, tag_id),
        )


def _save_metadata_json(pixiv_id, illust_data):
    metadata_dir = os.path.join(paths.DATA_DIR, METADATA_DIR)
    os.makedirs(metadata_dir, exist_ok=True)
    filepath = os.path.join(metadata_dir, f"{pixiv_id}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(illust_data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    result = sync_metadata()
    print(f"Synced: {result['synced']}, Failed: {result['failed']}, Deleted: {result['deleted']}")