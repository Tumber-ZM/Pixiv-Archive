import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
from urllib.parse import unquote, parse_qs
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import paths
from app import jobs
from app.database import get_db, init_db
from app.scanner import scan_directory, _normalize_image_pages
from app.thumbnails import generate_all_thumbnails
from app.sync import sync_metadata
from app.download import check_bookmark_subscription
from app.pixiv import reset_pixiv_client, fetch_profile_image, get_pixiv_client
from app.watcher import FolderWatcher
from app.events import publish, stream as event_stream

load_dotenv(paths.ENV_FILE)

logging.basicConfig(
    level=getattr(logging, (os.getenv("PA_LOG_LEVEL", "INFO") or "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("pixiv_archive")

# ---- 局域网访问控制 ----
# PA_HOST 由 run.py / launcher.py 在导入本模块前写入环境。
DEFAULT_PORT = 6814
_SYNC_DELAY_DEFAULT = 800
_SYNC_DELAY_MAX = 10000


def _parse_sync_delay(raw):
    s = (raw or "").strip()
    if not s:
        return _SYNC_DELAY_DEFAULT
    if s.isdigit():
        v = int(s)
        return v if v <= _SYNC_DELAY_MAX else _SYNC_DELAY_MAX
    return _SYNC_DELAY_DEFAULT
HOST = os.getenv("PA_HOST", "127.0.0.1").strip() or "127.0.0.1"
LAN_MODE = HOST not in ("127.0.0.1", "localhost", "::1")
ACCESS_TOKEN = os.getenv("PA_ACCESS_TOKEN", "").strip()
if LAN_MODE and not ACCESS_TOKEN:
    ACCESS_TOKEN = secrets.token_urlsafe(12)

_PUBLIC_PREFIXES = ("/static/", "/thumbnails/")
_LOCAL_CLIENTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
_SETTING_ENV_KEYS = (
    "PIXIV_REFRESH_TOKEN",
    "IMAGE_SOURCE_DIR",
    "IMAGE_SOURCE_DIRS",
    "PIXIV_PROXY",
    "PIXIV_MODE",
    "PIXIV_IMAGE_MIRROR",
    "PA_PORT",
    "PA_ACCESS_TOKEN",
    "SYNC_DELAY_MS",
    "AUTO_WATCH_ENABLED",
)


def is_local_client(host):
    return host in _LOCAL_CLIENTS


def get_lan_ip():
    """尽力获取本机局域网 IP（用于打印访问地址）。"""
    if not LAN_MODE:
        return "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "127.0.0.1"


def lan_access_url():
    token_part = f"?token={ACCESS_TOKEN}" if LAN_MODE else ""
    return f"http://{get_lan_ip()}:{os.getenv('PA_PORT') or DEFAULT_PORT}/{token_part}"


class LANGuardMiddleware:
    """LAN 模式下，非本机请求必须携带访问令牌（?token= 或 X-Access-Token）。

    静态资源与缩略图放行（避免静态资源请求无法带 header），
    页面 / API / 原图接口一律校验。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not LAN_MODE:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path.startswith(_PUBLIC_PREFIXES):
            await self.app(scope, receive, send)
            return

        client = (scope.get("client") or ("", 0))[0]
        if is_local_client(client):
            await self.app(scope, receive, send)
            return

        ok = False
        try:
            qs = parse_qs(scope.get("query_string", b"").decode("utf-8", "ignore"))
            ok = qs.get("token", [""])[0] == ACCESS_TOKEN
        except Exception:
            ok = False
        if not ok:
            for k, v in scope.get("headers", []):
                if k.lower() == b"x-access-token" and v.decode("utf-8", "ignore") == ACCESS_TOKEN:
                    ok = True
                    break

        if ok:
            await self.app(scope, receive, send)
            return

        accept = b""
        for k, v in scope.get("headers", []):
            if k.lower() == b"accept":
                accept = v
                break
        if b"text/html" in accept:
            body = (
                "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
                "<title>需要访问令牌</title></head><body style='font-family:sans-serif;"
                "background:#101218;color:#e9eaf0;padding:40px;'>"
                "<h2>需要访问令牌</h2>"
                "<p>此服务已开启局域网访问保护。请在浏览器地址栏末尾追加："
                "<code>?token=你的令牌</code></p></body></html>"
            ).encode("utf-8")
        else:
            body = json.dumps({
                "error": {
                    "code": "FORBIDDEN",
                    "message": "需要访问令牌",
                    "hint": "请在 URL 后附加 ?token=... 或携带 X-Access-Token 请求头",
                }
            }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", (b"text/html; charset=utf-8" if b"text/html" in accept else b"application/json")),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


app = FastAPI(title="Pixiv Archive")
app.add_middleware(LANGuardMiddleware)

THUMBNAIL_DIR = os.getenv("THUMBNAIL_DIR", "thumbnails")
THUMBNAIL_PATH = os.path.join(paths.DATA_DIR, THUMBNAIL_DIR)
SOURCE_DIR_SEPARATOR = "|"

# 自定义 ASGI 服务器不会触发 FastAPI 的 lifespan/startup 事件，
# 这里在模块加载时显式建库建表，确保全新环境可直接使用。
init_db()

os.makedirs(THUMBNAIL_PATH, exist_ok=True)
app.mount("/thumbnails", StaticFiles(directory=THUMBNAIL_PATH), name="thumbnails")

static_dir = os.path.join(paths.APP_DIR, "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

templates_dir = os.path.join(paths.APP_DIR, "templates")
templates = Jinja2Templates(directory=templates_dir)


def _split_source_dirs(raw):
    return [(p or "").strip() for p in (raw or "").split(SOURCE_DIR_SEPARATOR) if (p or "").strip()]


def _get_source_dirs():
    dirs = _split_source_dirs(os.getenv("IMAGE_SOURCE_DIRS", ""))
    legacy = (os.getenv("IMAGE_SOURCE_DIR", "") or "").strip()
    if legacy:
        dirs.insert(0, legacy)
    seen = set()
    result = []
    for directory in dirs:
        key = os.path.normcase(os.path.abspath(directory))
        if key not in seen:
            seen.add(key)
            result.append(directory)
    return result


def _primary_source_dir():
    dirs = _get_source_dirs()
    return dirs[0] if dirs else ""


def _auto_watch_enabled():
    return (os.getenv("AUTO_WATCH_ENABLED", "") or "").strip().lower() in ("1", "true", "yes", "on")


def _scan_source_dirs(source_dirs, progress_callback=None, cancel_event=None):
    log.info("scan started for %d source director%s: %s",
             len(source_dirs), "y" if len(source_dirs) == 1 else "ies", source_dirs)
    combined = {
        "total_files_scanned": 0,
        "pixiv_artworks_found": 0,
        "new_artworks": 0,
        "new_images": 0,
        "skipped": 0,
        "duplicates": 0,
        "pruned_duplicates": 0,
        "pruned_images": 0,
        "pruned_artworks": 0,
        "source_dirs": source_dirs,
    }
    for idx, source_dir in enumerate(source_dirs):
        if cancel_event and cancel_event.is_set():
            combined["cancelled"] = True
            break
        if progress_callback:
            progress_callback("scan", idx + 1, len(source_dirs), f"扫描图片目录 {idx + 1}/{len(source_dirs)}")
        result = scan_directory(source_dir, progress_callback, cancel_event)
        log.info(
            "scan directory finished: %s; files=%s artworks=%s new_artworks=%s new_images=%s skipped=%s duplicates=%s",
            source_dir,
            result.get("total_files_scanned", 0),
            result.get("pixiv_artworks_found", 0),
            result.get("new_artworks", 0),
            result.get("new_images", 0),
            result.get("skipped", 0),
            result.get("duplicates", 0),
        )
        if result.get("error"):
            raise RuntimeError(result["error"])
        if result.get("cancelled"):
            combined["cancelled"] = True
        for key in (
            "total_files_scanned", "pixiv_artworks_found", "new_artworks", "new_images",
            "skipped", "duplicates", "pruned_duplicates", "pruned_images", "pruned_artworks",
        ):
            combined[key] += result.get(key, 0) or 0
    log.info("scan finished: %s", combined)
    return combined


def _run_scan_for_sources(job, source_dirs):
    job.update(phase="scan", message="开始扫描…")
    result = _scan_source_dirs(source_dirs, job.update, job.cancel_event)
    if not result.get("cancelled"):
        job.update(phase="thumb", message="扫描完成，正在生成缩略图…")
        log.info("thumbnail refresh started")
        with get_db() as conn:
            thumb = generate_all_thumbnails(conn, job.update, job.cancel_event)
            result["thumbnails"] = thumb
        log.info("thumbnail refresh finished: %s", thumb)
        _sync_after_scan_if_needed(job, result)
    job.state["result"] = result


def _start_auto_scan():
    global _last_auto_job_id
    source_dirs = [d for d in _get_source_dirs() if os.path.isdir(d)]
    if not source_dirs:
        log.warning("auto scan skipped: no accessible source directories from configured paths %s", _get_source_dirs())
        return

    def run_scan(job):
        publish("auto_scan_started", {"job_id": job.job_id, "source_dirs": source_dirs})
        try:
            _run_scan_for_sources(job, source_dirs)
            publish("auto_scan_done", {"job_id": job.job_id, "result": job.state.get("result") or {}})
        except Exception as e:
            publish("auto_scan_failed", {"job_id": job.job_id, "error": str(e)})
            raise

    job_id, error = jobs.start("auto_scan", run_scan)
    if error:
        log.info("auto scan delayed because another job is running: %s", error)
        _folder_watcher.schedule_scan()
    else:
        _last_auto_job_id = job_id
        log.info("auto scan job started: id=%s", job_id)
        publish("auto_scan_job_created", {"job_id": job_id})


_folder_watcher = FolderWatcher(_start_auto_scan)
_last_auto_job_id = None


def _restart_folder_watcher():
    if _auto_watch_enabled():
        result = _folder_watcher.restart(_get_source_dirs())
        log.info("auto watch enabled; restart result: %s", result)
        return result
    _folder_watcher.stop()
    log.info("auto watch disabled")
    return {"ok": True, "paths": []}


def _normalize_existing_pages():
    log.info("normalizing existing image pages")
    with get_db() as conn:
        _normalize_image_pages(conn)
    log.info("existing image pages normalized")


def _sync_after_scan_if_needed(job, result):
    changed = (result.get("new_artworks", 0) or 0) > 0 or (result.get("new_images", 0) or 0) > 0
    if not changed or job.cancel_event.is_set():
        return
    if not os.getenv("PIXIV_REFRESH_TOKEN", ""):
        log.warning("metadata sync skipped after scan: PIXIV_REFRESH_TOKEN is not set")
        result["sync_skipped"] = "NO_TOKEN"
        return
    job.update(phase="sync", current=0, total=None, message="扫描完成，正在同步新作品元数据…")
    log.info("metadata sync after scan started")
    sync_result = sync_metadata(progress_callback=job.update, cancel_event=job.cancel_event)
    result["synced"] = sync_result.get("synced", 0)
    result["sync_failed"] = sync_result.get("failed", 0)
    result["sync_deleted"] = sync_result.get("deleted", 0)
    log.info("metadata sync after scan finished: %s", sync_result)


@app.on_event("startup")
def startup():
    init_db()
    _normalize_existing_pages()
    _restart_folder_watcher()


_normalize_existing_pages()
_restart_folder_watcher()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ---- R18 / 状态筛选 ----
# 严格判定：#R18 标签（R-18 / R18 / 18R，大小写不敏感），不含 R-18G。
_R18_TAG_SET = "('r-18','r18','18r')"


def _r18_exists(alias="a"):
    return (
        f"{alias}.id IN (SELECT rj.artwork_id FROM artwork_tags rj "
        f"JOIN tags rt ON rj.tag_id = rt.id "
        f"WHERE (LOWER(COALESCE(rt.name, '')) IN {_R18_TAG_SET} "
        f"OR LOWER(COALESCE(rt.translated_name, '')) IN {_R18_TAG_SET}))"
    )


def _r18_where(alias, value):
    mode = (value or "").strip().lower()
    if mode == "hide":
        return "NOT " + _r18_exists(alias)
    if mode == "only":
        return "(" + _r18_exists(alias) + ")"
    return None


def _cover_image_join(alias="a"):
    return (
        f"LEFT JOIN images i ON i.id = ("
        f"SELECT i2.id FROM images i2 "
        f"WHERE i2.artwork_id = {alias}.id "
        f"ORDER BY CASE WHEN i2.page = 1 THEN 0 ELSE 1 END, "
        f"i2.page ASC, i2.id ASC LIMIT 1)"
    )


def _gallery_row_payload(row, expand_pages=False):
    item = {
        "id": row["id"],
        "pixiv_id": row["pixiv_id"],
        "title": row["title"] or f"Pixiv ID: {row['pixiv_id']}",
        "author_name": row["author_name"] or "",
        "page_count": row["page_count"],
        "create_date": row["create_date"] or "",
        "pixiv_status": row["pixiv_status"],
        "thumb_path": row["thumb_path"] or "",
        "is_favorited": bool(row["is_favorited"]),
        "is_r18": bool(row["is_r18"]),
        "sync_error": row["sync_error"] or "",
    }
    if expand_pages:
        item.update({
            "image_id": row["image_id"],
            "image_page": row["image_page"],
            "image_path": row["image_path"] or "",
        })
    return item


@app.get("/api/artworks")
def api_artworks(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    sort: str = Query("id", pattern="^(id|pixiv_id|title|create_date|first_seen|random)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    author: str = Query(""),
    tag: str = Query(""),
    favorite_id: int = Query(0, ge=0),
    r18: str = Query(""),
    status: str = Query(""),
    expand_pages: bool = Query(False),
):
    with get_db() as conn:
        where_clauses = []
        params = []

        if author:
            where_clauses.append("a.author_name LIKE ?")
            params.append(f"%{author}%")
        if tag:
            where_clauses.append(
                "a.id IN (SELECT artwork_id FROM artwork_tags at2 "
                "JOIN tags t ON at2.tag_id = t.id WHERE t.name LIKE ?)"
            )
            params.append(f"%{tag}%")
        if favorite_id:
            where_clauses.append(
                "a.id IN (SELECT artwork_id FROM favorite_artworks "
                "WHERE favorite_id = ?)"
            )
            params.append(favorite_id)
        if status == "deleted":
            where_clauses.append("a.pixiv_status = 'deleted'")
        _r18c = _r18_where("a", r18)
        if _r18c:
            where_clauses.append(_r18c)

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)
        if expand_pages:
            count_sql = f"SELECT COUNT(*) FROM artworks a JOIN images i ON i.artwork_id = a.id {where_sql}"
        else:
            count_sql = f"SELECT COUNT(*) FROM artworks a {where_sql}"
        count_row = conn.execute(count_sql, params).fetchone()
        total = count_row[0]

        offset = (page - 1) * per_page
        if sort == "random":
            order_sql = "ORDER BY RANDOM()"
        else:
            sort_column = {
                "id": "a.id", "pixiv_id": "a.pixiv_id",
                "title": "a.title", "create_date": "a.create_date",
                "first_seen": "a.first_seen",
            }[sort]
            order_direction = "DESC" if order == "desc" else "ASC"
            order_sql = f"ORDER BY {sort_column} {order_direction}"

        if expand_pages:
            if sort != "random":
                order_sql += ", i.page ASC, i.id ASC"
            rows = conn.execute(
                f"""SELECT a.*, i.id AS image_id, i.page AS image_page,
                    i.path AS image_path, i.path AS thumb_path,
                    a.id IN (SELECT fa.artwork_id FROM favorite_artworks fa) AS is_favorited,
                    {_r18_exists()} AS is_r18
                    FROM artworks a
                    JOIN images i ON i.artwork_id = a.id
                    {where_sql}
                    {order_sql}
                    LIMIT ? OFFSET ?""",
                params + [per_page, offset],
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT a.*, i.path AS thumb_path,
                    a.id IN (SELECT fa.artwork_id FROM favorite_artworks fa) AS is_favorited,
                    {_r18_exists()} AS is_r18
                    FROM artworks a
                    {_cover_image_join()}
                    {where_sql}
                    {order_sql}
                    LIMIT ? OFFSET ?""",
                params + [per_page, offset],
            ).fetchall()

        artworks = [_gallery_row_payload(row, expand_pages) for row in rows]

        return {
            "artworks": artworks,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page,
        }


@app.get("/api/artworks/{artwork_id}")
def api_artwork_detail(artwork_id: int):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM artworks WHERE id = ?", (artwork_id,)
        ).fetchone()
        if not row:
            return JSONResponse({"error": "Not found"}, status_code=404)

        images = conn.execute(
            "SELECT * FROM images WHERE artwork_id = ? ORDER BY page",
            (artwork_id,),
        ).fetchall()

        tags = conn.execute(
            """SELECT t.* FROM tags t
               JOIN artwork_tags at2 ON t.id = at2.tag_id
               WHERE at2.artwork_id = ?""",
            (artwork_id,),
        ).fetchall()

        favorites = conn.execute(
            """SELECT f.id, f.name FROM favorites f
               JOIN favorite_artworks fa ON fa.favorite_id = f.id
               WHERE fa.artwork_id = ?
               ORDER BY f.name""",
            (artwork_id,),
        ).fetchall()

        return {
            "id": row["id"],
            "pixiv_id": row["pixiv_id"],
            "title": row["title"] or f"Pixiv ID: {row['pixiv_id']}",
            "description": row["description"] or "",
            "author_id": row["author_id"],
            "author_name": row["author_name"] or "",
            "create_date": row["create_date"] or "",
            "page_count": row["page_count"],
            "width": row["width"],
            "height": row["height"],
            "pixiv_status": row["pixiv_status"],
            "first_seen": row["first_seen"],
            "last_synced": row["last_synced"],
            "sync_error": row["sync_error"] or "",
            "images": [dict(img) for img in images],
            "tags": [dict(tag) for tag in tags],
            "favorites": [dict(f) for f in favorites],
        }


@app.delete("/api/artworks/{artwork_id}")
def api_delete_artwork(artwork_id: int, delete_files: bool = Query(False)):
    with get_db() as conn:
        images = conn.execute(
            "SELECT path FROM images WHERE artwork_id = ?", (artwork_id,)
        ).fetchall()

        pixiv_id = conn.execute(
            "SELECT pixiv_id FROM artworks WHERE id = ?", (artwork_id,)
        ).fetchone()

        if not pixiv_id:
            return JSONResponse({"error": "Not found"}, status_code=404)

        conn.execute("DELETE FROM artwork_tags WHERE artwork_id = ?", (artwork_id,))
        conn.execute("DELETE FROM images WHERE artwork_id = ?", (artwork_id,))
        conn.execute("DELETE FROM artworks WHERE id = ?", (artwork_id,))

        if delete_files:
            for img in images:
                try:
                    os.remove(img["path"])
                except Exception:
                    pass
            thumb_path = os.path.join(THUMBNAIL_PATH, f"{pixiv_id['pixiv_id']}.jpg")
            try:
                os.remove(thumb_path)
            except Exception:
                pass

        return {"status": "deleted", "artwork_id": artwork_id, "files_deleted": delete_files}


@app.post("/api/artworks/batch-delete")
async def api_batch_delete_artworks(request: Request):
    from pydantic import BaseModel

    class BatchDelete(BaseModel):
        artwork_ids: list[int]
        delete_files: bool = False

    body = await request.json()
    data = BatchDelete(**body)
    deleted = 0
    with get_db() as conn:
        for artwork_id in data.artwork_ids:
            images = conn.execute(
                "SELECT path FROM images WHERE artwork_id = ?", (artwork_id,)
            ).fetchall()
            pixiv_id = conn.execute(
                "SELECT pixiv_id FROM artworks WHERE id = ?", (artwork_id,)
            ).fetchone()
            if not pixiv_id:
                continue
            conn.execute("DELETE FROM artwork_tags WHERE artwork_id = ?", (artwork_id,))
            conn.execute("DELETE FROM images WHERE artwork_id = ?", (artwork_id,))
            conn.execute("DELETE FROM artworks WHERE id = ?", (artwork_id,))
            if data.delete_files:
                for img in images:
                    try:
                        os.remove(img["path"])
                    except Exception:
                        pass
                thumb_path = os.path.join(THUMBNAIL_PATH, f"{pixiv_id['pixiv_id']}.jpg")
                try:
                    os.remove(thumb_path)
                except Exception:
                    pass
            deleted += 1
    return {"status": "ok", "deleted": deleted, "files_deleted": data.delete_files}


@app.post("/api/artworks/batch-favorites-clear")
async def api_batch_favorites_clear(request: Request):
    from pydantic import BaseModel

    class BatchClear(BaseModel):
        artwork_ids: list[int]

    body = await request.json()
    data = BatchClear(**body)
    with get_db() as conn:
        if data.artwork_ids:
            placeholders = ",".join("?" * len(data.artwork_ids))
            conn.execute(
                f"DELETE FROM favorite_artworks WHERE artwork_id IN ({placeholders})",
                data.artwork_ids,
            )
    return {"status": "ok", "cleared": len(data.artwork_ids)}


@app.get("/api/search")
def api_search(
    q: str = Query(""),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    sort: str = Query("id", pattern="^(id|pixiv_id|title|create_date|first_seen|random)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    author: str = Query(""),
    tag: str = Query(""),
    favorite_id: int = Query(0, ge=0),
    r18: str = Query(""),
    status: str = Query(""),
    expand_pages: bool = Query(False),
):
    query = q.strip()
    if not query:
        return api_artworks(page=page, per_page=per_page, sort=sort, order=order,
                            author=author, tag=tag, favorite_id=favorite_id,
                            r18=r18, status=status, expand_pages=expand_pages)

    tokens = [t for t in query.split() if t]

    with get_db() as conn:
        token_conditions = []
        params = []
        for token in tokens:
            like = f"%{token}%"
            cond = (
                "(a.title LIKE ? OR a.description LIKE ? OR a.author_name LIKE ? "
                "OR CAST(a.pixiv_id AS TEXT) LIKE ? "
                "OR EXISTS (SELECT 1 FROM artwork_tags at2 "
                "JOIN tags t ON at2.tag_id = t.id "
                "WHERE at2.artwork_id = a.id "
                "AND (t.name LIKE ? OR t.translated_name LIKE ?)))"
            )
            token_conditions.append(cond)
            params.extend([like, like, like, like, like, like])

        if author:
            token_conditions.append("a.author_name LIKE ?")
            params.append(f"%{author}%")
        if tag:
            token_conditions.append(
                "a.id IN (SELECT artwork_id FROM artwork_tags at2 "
                "JOIN tags t ON at2.tag_id = t.id WHERE t.name LIKE ?)"
            )
            params.append(f"%{tag}%")
        if favorite_id:
            token_conditions.append(
                "a.id IN (SELECT artwork_id FROM favorite_artworks "
                "WHERE favorite_id = ?)"
            )
            params.append(favorite_id)
        if status == "deleted":
            token_conditions.append("a.pixiv_status = 'deleted'")
        _r18c = _r18_where("a", r18)
        if _r18c:
            token_conditions.append(_r18c)

        where_sql = " WHERE " + " AND ".join(token_conditions)

        if expand_pages:
            count_sql = f"SELECT COUNT(*) FROM artworks a JOIN images i ON i.artwork_id = a.id{where_sql}"
        else:
            count_sql = f"SELECT COUNT(*) FROM artworks a{where_sql}"
        count_row = conn.execute(count_sql, params).fetchone()
        total = count_row[0]

        offset = (page - 1) * per_page
        if sort == "random":
            order_sql = "ORDER BY RANDOM()"
        else:
            sort_column = {
                "id": "a.id", "pixiv_id": "a.pixiv_id",
                "title": "a.title", "create_date": "a.create_date",
                "first_seen": "a.first_seen",
            }[sort]
            order_direction = "DESC" if order == "desc" else "ASC"
            order_sql = f"ORDER BY {sort_column} {order_direction}"

        if expand_pages:
            if sort != "random":
                order_sql += ", i.page ASC, i.id ASC"
            rows = conn.execute(
                f"""SELECT a.*, i.id AS image_id, i.page AS image_page,
                    i.path AS image_path, i.path AS thumb_path,
                    a.id IN (SELECT fa.artwork_id FROM favorite_artworks fa) AS is_favorited,
                    {_r18_exists()} AS is_r18
                    FROM artworks a
                    JOIN images i ON i.artwork_id = a.id
                    {where_sql}
                    {order_sql}
                    LIMIT ? OFFSET ?""",
                params + [per_page, offset],
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT a.*, i.path AS thumb_path,
                    a.id IN (SELECT fa.artwork_id FROM favorite_artworks fa) AS is_favorited,
                    {_r18_exists()} AS is_r18
                    FROM artworks a
                    {_cover_image_join()}
                    {where_sql}
                    {order_sql}
                    LIMIT ? OFFSET ?""",
                params + [per_page, offset],
            ).fetchall()

        artworks = [_gallery_row_payload(row, expand_pages) for row in rows]

        return {
            "artworks": artworks,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page,
        }


@app.get("/api/tags")
def api_tags(q: str = Query("")):
    with get_db() as conn:
        q = q.strip()
        where = ""
        params = []
        if q:
            where = " WHERE t.name LIKE ?"
            params.append(f"%{q}%")
        rows = conn.execute(
            f"""SELECT t.*, COUNT(at2.artwork_id) AS artwork_count
               FROM tags t
               LEFT JOIN artwork_tags at2 ON t.id = at2.tag_id
               {where}
               GROUP BY t.id
               ORDER BY artwork_count DESC
               LIMIT 200""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


@app.get("/api/favorites")
def api_favorites(r18: str = Query("")):
    with get_db() as conn:
        favorites = conn.execute(
            """SELECT f.*, COUNT(fa.artwork_id) AS artwork_count
               FROM favorites f
               LEFT JOIN favorite_artworks fa ON fa.favorite_id = f.id
               GROUP BY f.id
               ORDER BY f.create_date DESC, f.id DESC"""
        ).fetchall()

        result = []
        for fav in favorites:
            works_clauses = ["fa.favorite_id = ?"]
            _r18c = _r18_where("a", r18)
            if _r18c:
                works_clauses.append(_r18c)
            works = conn.execute(
                f"""SELECT a.id, a.pixiv_id, a.title, a.page_count, i.path AS thumb_path
                    FROM artworks a
                    {_cover_image_join()}
                    JOIN favorite_artworks fa ON fa.artwork_id = a.id
                    WHERE {" AND ".join(works_clauses)}
                    ORDER BY fa.added_date DESC
                    LIMIT 12""",
                (fav["id"],),
            ).fetchall()
            result.append({
                "id": fav["id"],
                "name": fav["name"],
                "create_date": fav["create_date"],
                "artwork_count": fav["artwork_count"],
                "works": [dict(w) for w in works],
            })
        return result


@app.post("/api/favorites")
async def api_create_favorite(request: Request):
    from pydantic import BaseModel

    class FavoriteCreate(BaseModel):
        name: str

    body = await request.json()
    data = FavoriteCreate(**body)
    name = data.name.strip()
    if not name:
        return JSONResponse({"error": "名称不能为空"}, status_code=400)
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM favorites WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            return JSONResponse({"error": "收藏夹已存在"}, status_code=400)
        cursor = conn.execute(
            "INSERT INTO favorites (name) VALUES (?)", (name,)
        )
        return {"status": "ok", "id": cursor.lastrowid, "name": name}


@app.delete("/api/favorites/{favorite_id}")
def api_delete_favorite(favorite_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM favorites WHERE id = ?", (favorite_id,))
        return {"status": "deleted"}


@app.post("/api/favorites/{favorite_id}/artworks")
async def api_add_favorite_artworks(favorite_id: int, request: Request):
    from pydantic import BaseModel

    class FavoriteAdd(BaseModel):
        artwork_ids: list[int]

    body = await request.json()
    data = FavoriteAdd(**body)
    with get_db() as conn:
        fav = conn.execute(
            "SELECT id FROM favorites WHERE id = ?", (favorite_id,)
        ).fetchone()
        if not fav:
            return JSONResponse({"error": "收藏夹不存在"}, status_code=404)
        added = 0
        for aid in data.artwork_ids:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO favorite_artworks (favorite_id, artwork_id) VALUES (?, ?)",
                (favorite_id, aid),
            )
            if cursor.rowcount:
                added += 1
        return {"status": "ok", "added": added}


@app.delete("/api/favorites/{favorite_id}/artworks/{artwork_id}")
def api_remove_favorite_artwork(favorite_id: int, artwork_id: int):
    with get_db() as conn:
        conn.execute(
            "DELETE FROM favorite_artworks WHERE favorite_id = ? AND artwork_id = ?",
            (favorite_id, artwork_id),
        )
        return {"status": "ok"}


@app.post("/api/favorites/{favorite_id}/artworks/batch-remove")
async def api_batch_remove_favorite_artworks(favorite_id: int, request: Request):
    from pydantic import BaseModel

    class BatchRemove(BaseModel):
        artwork_ids: list[int]

    body = await request.json()
    data = BatchRemove(**body)
    with get_db() as conn:
        if data.artwork_ids:
            placeholders = ",".join("?" * len(data.artwork_ids))
            conn.execute(
                f"DELETE FROM favorite_artworks WHERE favorite_id = ? AND artwork_id IN ({placeholders})",
                [favorite_id] + data.artwork_ids,
            )
    return {"status": "ok", "removed": len(data.artwork_ids)}


@app.get("/api/authors")
def api_authors():
    with get_db() as conn:
        rows = conn.execute(
            """SELECT au.*, COUNT(a.id) AS artwork_count
               FROM authors au
               LEFT JOIN artworks a ON a.author_id = au.id
               GROUP BY au.id
               ORDER BY artwork_count DESC
               LIMIT 200"""
        ).fetchall()
        return [dict(row) for row in rows]


@app.get("/api/authors/works")
def api_authors_works(
    limit: int = Query(20, ge=1, le=100),
    q: str = Query(""),
    r18: str = Query(""),
):
    with get_db() as conn:
        q = q.strip()
        where = ""
        params = []
        if q:
            where = " WHERE au.name LIKE ?"
            params.append(f"%{q}%")
        authors = conn.execute(
            f"""SELECT au.*, COUNT(a.id) AS artwork_count
               FROM authors au
               LEFT JOIN artworks a ON a.author_id = au.id
               {where}
               GROUP BY au.id
               ORDER BY artwork_count DESC
               LIMIT 200""", params
        ).fetchall()

        result = []
        for au in authors:
            works_where = "a.author_id = ?"
            _r18c = _r18_where("a", r18)
            if _r18c:
                works_where += " AND " + _r18c
            works = conn.execute(
                f"""SELECT a.id, a.pixiv_id, a.title, a.page_count, i.path AS thumb_path
                   FROM artworks a
                   {_cover_image_join()}
                   WHERE {works_where}
                   ORDER BY a.create_date DESC, a.id DESC
                   LIMIT ?""",
                (au["id"], limit),
            ).fetchall()
            result.append({
                "id": au["id"],
                "pixiv_user_id": au["pixiv_user_id"],
                "name": au["name"],
                "artwork_count": au["artwork_count"],
                "works": [dict(w) for w in works],
            })
        return result


@app.get("/api/authors/{author_id}/avatar")
def api_author_avatar(author_id: int):
    """Author avatar: serve from local cache, fetch via direct connect on miss."""
    avatars_dir = os.path.join(paths.DATA_DIR, "metadata", "avatars")
    cache_path = os.path.join(avatars_dir, f"{author_id}.jpg")
    if os.path.exists(cache_path):
        return FileResponse(cache_path, media_type="image/jpeg")

    with get_db() as conn:
        row = conn.execute(
            "SELECT profile_image FROM authors WHERE id = ?", (author_id,)
        ).fetchone()
    if not row or not row["profile_image"]:
        return JSONResponse({"error": "no avatar"}, status_code=404)

    data = fetch_profile_image(row["profile_image"])
    if not data:
        return JSONResponse({"error": "fetch failed"}, status_code=404)
    os.makedirs(avatars_dir, exist_ok=True)
    with open(cache_path, "wb") as f:
        f.write(data)
    return FileResponse(cache_path, media_type="image/jpeg")


@app.post("/api/open-folder")
async def api_open_folder(request: Request):
    from pydantic import BaseModel

    class OpenFolder(BaseModel):
        image_id: int

    body = await request.json()
    data = OpenFolder(**body)
    with get_db() as conn:
        row = conn.execute(
            "SELECT path FROM images WHERE id = ?", (data.image_id,)
        ).fetchone()
    if not row:
        return JSONResponse({"error": "Not found"}, status_code=404)
    path = row["path"]
    if not path or not os.path.isabs(path) or not os.path.exists(path):
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    try:
        # 用资源管理器打开并选中文件
        subprocess.Popen(["explorer", "/select,", path])
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _error_response(code, message, hint=None, detail=None, status_code=400):
    return JSONResponse(
        {"error": {"code": code, "message": message, "hint": hint, "detail": detail}},
        status_code=status_code,
    )


def _busy_error():
    return _error_response(
        "BUSY", "已有任务进行中",
        "请等待当前任务完成，或在进度窗中点击关闭以停止",
    )


@app.get("/api/scan")
def api_scan():
    if jobs.is_busy():
        return _busy_error()

    source_dirs = _get_source_dirs()
    if not source_dirs:
        return _error_response("NO_SOURCE_DIR", "未设置本地图片目录",
                               "请到 设置 → 本地图片目录 填写后重试")
    missing = [d for d in source_dirs if not os.path.isdir(d)]
    if missing:
        return _error_response("SOURCE_DIR_NOT_FOUND", f"图片目录不存在：{missing[0]}",
                               "请检查设置中的目录路径是否正确")

    def run_scan(job):
        _run_scan_for_sources(job, source_dirs)

    job_id, error = jobs.start("scan", run_scan)
    if error:
        return _busy_error()
    return {"job_id": job_id, "kind": "scan"}


# ===== 收藏订阅（订阅某用户的公开收藏列表）=====

def _bookmark_source_dir_check():
    """收藏订阅任务共用前置校验，返回 (source_dir, error_response)。"""
    if jobs.is_busy():
        return None, _busy_error()
    source_dir = _primary_source_dir()
    if not source_dir:
        return None, _error_response("NO_SOURCE_DIR", "未设置本地图片目录",
                                     "请到 设置 → 本地图片目录 填写后重试")
    if not os.path.isdir(source_dir):
        return None, _error_response("SOURCE_DIR_NOT_FOUND", f"图片目录不存在：{source_dir}",
                                     "请检查设置中的目录路径是否正确")
    if not os.getenv("PIXIV_REFRESH_TOKEN", ""):
        return None, _error_response("NO_TOKEN", "未设置 Pixiv Refresh Token",
                                     "请到 设置 → Pixiv Refresh Token 填写后重试")
    return source_dir, None


def _run_bookmark_check(job, subs, source_dir):
    """收藏订阅检查核心：逐订阅增量下载新收藏 → 有新文件则扫描入库+缩略图+元数据同步。

    subs: [(pixiv_user_id, last_pid)]。与扫描/同步共用 jobs 单任务锁。auth 错误向上抛，
    由 jobs 置为 error。落盘目录按每条收藏自身画师分组（download_single_illust 处理）。
    """
    from datetime import datetime
    from app.pixiv import _build_session_with_referer

    delay = _parse_sync_delay(os.getenv("SYNC_DELAY_MS", ""))
    client = get_pixiv_client()
    client._ensure_auth()
    session = _build_session_with_referer(
        "https://www.pixiv.net/", use_proxy=(client.mode == "proxy")
    )

    total_downloaded = 0
    per_sub = []
    for i, (uid, last_pid) in enumerate(subs):
        if job.cancel_event.is_set():
            break
        job.update("check", i + 1, len(subs), f"检查收藏订阅 {i + 1}/{len(subs)}…")
        r = check_bookmark_subscription(
            client, session, uid, last_pid, source_dir,
            delay_ms=delay, progress_callback=job.update,
            cancel_event=job.cancel_event,
        )
        per_sub.append(r)
        total_downloaded += r["downloaded"]
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        brief = json.dumps(
            {"new": r["new_found"], "downloaded": r["downloaded"],
             "failed": r["failed"]}, ensure_ascii=False)
        with get_db() as conn:
            conn.execute(
                "UPDATE bookmark_subs SET last_pid = ?, last_checked = ?, last_result = ? "
                "WHERE pixiv_user_id = ?",
                (r["max_pid_seen"], now_str, brief, uid),
            )

    result = {
        "checked": len(per_sub),
        "downloaded": total_downloaded,
        "new_found": sum(s["new_found"] for s in per_sub),
        "failed": sum(s["failed"] for s in per_sub),
        "cancelled": job.cancel_event.is_set(),
        "subs": [
            {"pixiv_user_id": s["pixiv_user_id"], "new": s["new_found"],
             "downloaded": s["downloaded"], "failed": s["failed"]}
            for s in per_sub if s["new_found"]
        ],
    }
    # 有新文件才走入库三件套（scan 幂等；sync 设计上只处理未同步作品）
    if total_downloaded > 0 and not job.cancel_event.is_set():
        job.update("scan", 0, None, "下载完成，正在扫描入库…")
        scan_result = scan_directory(source_dir, job.update, job.cancel_event)
        result["scan"] = {k: scan_result.get(k) for k in
                          ("new_artworks", "new_images") if k in scan_result}
        if not job.cancel_event.is_set():
            job.update("thumb", 0, None, "正在生成缩略图…")
            with get_db() as conn:
                thumb = generate_all_thumbnails(conn, job.update, job.cancel_event)
                result["thumbnails"] = thumb
        if not job.cancel_event.is_set():
            job.update("sync", 0, None, "正在同步新作品元数据…")
            sync_result = sync_metadata(
                progress_callback=job.update, cancel_event=job.cancel_event
            )
            result["synced"] = sync_result.get("synced", 0)
    job.state["result"] = result


@app.get("/api/bookmark-subs")
def api_list_bookmark_subs():
    with get_db() as conn:
        rows = conn.execute(
            """SELECT s.*, a.id AS author_id
               FROM bookmark_subs s
               LEFT JOIN authors a ON a.pixiv_user_id = s.pixiv_user_id
               ORDER BY s.created_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/bookmark-subs")
async def api_add_bookmark_sub(request: Request):
    body = await request.json()
    uid = str(body.get("user_id", "")).strip()
    if not uid.isdigit() or int(uid) <= 0:
        return _error_response("INVALID_UID", "用户 ID 无效",
                               "请输入 Pixiv 用户主页 URL 中的数字 ID")
    uid = int(uid)
    name = (body.get("name") or "").strip()
    if not name:
        name = get_pixiv_client().get_user_display_name(uid)
    with get_db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO bookmark_subs (pixiv_user_id, name) VALUES (?, ?)",
            (uid, name),
        )
        exists = cur.rowcount == 0
    return {"status": "ok", "exists": exists, "name": name}


def _get_bookmark_sub_or_none(pixiv_user_id: int):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM bookmark_subs WHERE pixiv_user_id = ?", (pixiv_user_id,)
        ).fetchone()


@app.post("/api/bookmark-subs/{pixiv_user_id}/check")
def api_check_one_bookmark_sub(pixiv_user_id: int):
    source_dir, err = _bookmark_source_dir_check()
    if err:
        return err
    row = _get_bookmark_sub_or_none(pixiv_user_id)
    if not row:
        return _error_response("NOT_FOUND", "未订阅该用户的收藏", "请先添加订阅")

    def run(job):
        _run_bookmark_check(job, [(pixiv_user_id, row["last_pid"])], source_dir)

    job_id, error = jobs.start("bookmark_check", run)
    if error:
        return _busy_error()
    return {"job_id": job_id, "kind": "bookmark_check"}


@app.get("/api/bookmark-subs/preview/{pixiv_user_id}")
def api_preview_bookmark_sub(pixiv_user_id: int):
    """订阅前预览：该用户公开收藏数与首屏新增数量估算（不占任务锁）。"""
    if not os.getenv("PIXIV_REFRESH_TOKEN", ""):
        return _error_response("NO_TOKEN", "未设置 Pixiv Refresh Token",
                               "请到 设置 → Pixiv Refresh Token 填写后重试")
    client = get_pixiv_client()
    try:
        client._ensure_auth()
        name = client.get_user_display_name(pixiv_user_id)
        # 首屏：不带游标取一页，统计非空条数
        illusts, _ = client.list_user_bookmarks(pixiv_user_id)
        sample = len([i for i in illusts if i.get("id")])
    except Exception as e:
        return _error_response("PREVIEW_FAILED", f"预览失败：{str(e)[:150]}",
                               "请确认用户 ID 正确且其收藏为公开")
    return {"name": name, "first_page": sample}


@app.post("/api/bookmark-subs/{pixiv_user_id}/toggle")
def api_toggle_bookmark_sub(pixiv_user_id: int):
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE bookmark_subs SET auto_download = 1 - auto_download "
            "WHERE pixiv_user_id = ?",
            (pixiv_user_id,),
        )
        if not cur.rowcount:
            return _error_response("NOT_FOUND", "未订阅该用户的收藏", "")
        row = conn.execute(
            "SELECT auto_download FROM bookmark_subs WHERE pixiv_user_id = ?",
            (pixiv_user_id,),
        ).fetchone()
    return {"status": "ok", "auto_download": bool(row["auto_download"])}


@app.delete("/api/bookmark-subs/{pixiv_user_id}")
def api_delete_bookmark_sub(pixiv_user_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM bookmark_subs WHERE pixiv_user_id = ?",
                     (pixiv_user_id,))
    return {"status": "ok"}


@app.get("/api/bookmark-subs/check")
def api_check_all_bookmark_subs():
    """一键检查全部启用中的收藏订阅并自动下载新收藏（完成后入库+缩略图+同步元数据）。"""
    source_dir, err = _bookmark_source_dir_check()
    if err:
        return err
    with get_db() as conn:
        rows = conn.execute(
            "SELECT pixiv_user_id, last_pid FROM bookmark_subs WHERE auto_download = 1"
        ).fetchall()
    if not rows:
        return _error_response("NO_SUBS", "还没有订阅任何收藏列表",
                               "在「收藏订阅」视图中添加用户 ID")
    subs = [(r["pixiv_user_id"], r["last_pid"]) for r in rows]

    def run(job):
        _run_bookmark_check(job, subs, source_dir)

    job_id, error = jobs.start("bookmark_check", run)
    if error:
        return _busy_error()
    return {"job_id": job_id, "kind": "bookmark_check"}


@app.get("/api/sync")
def api_sync(pixiv_id: int = Query(0)):
    if jobs.is_busy():
        return _busy_error()

    if not os.getenv("PIXIV_REFRESH_TOKEN", ""):
        return _error_response("NO_TOKEN", "未设置 Pixiv Refresh Token",
                               "请到 设置 → Pixiv Refresh Token 填写后重试")

    def run_sync(job):
        job.update(phase="sync", message="开始同步…")
        result = sync_metadata(
            specific_pixiv_id=pixiv_id if pixiv_id else None,
            progress_callback=job.update,
            cancel_event=job.cancel_event,
        )
        job.state["result"] = result

    job_id, error = jobs.start("sync", run_sync)
    if error:
        return _busy_error()
    return {"job_id": job_id, "kind": "sync"}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    snap = jobs.get(job_id)
    if not snap:
        return _error_response("JOB_NOT_FOUND", "任务不存在或已过期", None, None, 404)
    return snap


@app.post("/api/jobs/{job_id}/cancel")
def api_job_cancel(job_id: str):
    snap = jobs.cancel(job_id)
    if not snap:
        return _error_response("JOB_NOT_FOUND", "任务不存在或已过期", None, None, 404)
    return snap


@app.get("/api/watch/status")
def api_watch_status():
    return {
        "enabled": _auto_watch_enabled(),
        "configured_dirs": _get_source_dirs(),
        "watcher": _folder_watcher.status(),
        "busy": jobs.is_busy(),
        "last_auto_job_id": _last_auto_job_id,
        "last_auto_job": jobs.get(_last_auto_job_id) if _last_auto_job_id else None,
    }


@app.get("/api/events")
def api_events():
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _read_env_file():
    env_path = paths.ENV_FILE
    result = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    result[key.strip()] = value.strip()
    for key in _SETTING_ENV_KEYS:
        if key not in result and key in os.environ:
            result[key] = os.environ.get(key, "")
    return result


def _write_env_file(settings):
    env_path = paths.ENV_FILE
    existing = _read_env_file()
    existing.update(settings)
    with open(env_path, "w", encoding="utf-8") as f:
        for key, value in existing.items():
            f.write(f"{key}={value}\n")


@app.get("/api/settings")
def api_get_settings():
    settings = _read_env_file()
    token = settings.get("PIXIV_REFRESH_TOKEN", "")
    port_str = (settings.get("PA_PORT", "") or "").strip()
    access_token_auto = LAN_MODE and not (settings.get("PA_ACCESS_TOKEN", "") or "").strip()
    source_dirs = _split_source_dirs(settings.get("IMAGE_SOURCE_DIRS", ""))
    if not source_dirs and settings.get("IMAGE_SOURCE_DIR", ""):
        source_dirs = [settings.get("IMAGE_SOURCE_DIR", "")]
    watcher_status = _folder_watcher.status()
    return {
        "has_token": bool(token),
        "token_preview": token[:8] + "..." if len(token) > 8 else token,
        "image_source_dir": source_dirs[0] if source_dirs else "",
        "image_source_dirs": source_dirs,
        "proxy": settings.get("PIXIV_PROXY", ""),
        "connection_mode": settings.get("PIXIV_MODE", "auto"),
        "image_mirror": settings.get("PIXIV_IMAGE_MIRROR", ""),
        "server_port": int(port_str) if port_str.isdigit() else DEFAULT_PORT,
        "access_token": ACCESS_TOKEN,
        "access_token_auto": access_token_auto,
        "sync_delay_ms": _parse_sync_delay(settings.get("SYNC_DELAY_MS", "")),
        "auto_watch_enabled": (settings.get("AUTO_WATCH_ENABLED", "") or "").strip() == "1",
        "auto_watch_running": watcher_status["running"],
        "auto_watch_available": watcher_status["available"],
    }


@app.post("/api/settings")
async def api_update_settings(request: Request):
    from pydantic import BaseModel

    class SettingsUpdate(BaseModel):
        refresh_token: str = ""
        image_source_dir: str = ""
        image_source_dirs: list[str] = []
        proxy: str = ""
        connection_mode: str = ""
        server_port: str = ""
        access_token: str = ""
        sync_delay_ms: str = ""
        image_mirror: str = ""
        auto_watch_enabled: bool = False

    body = await request.json()
    data = SettingsUpdate(**body)
    updates = {}
    if data.refresh_token:
        updates["PIXIV_REFRESH_TOKEN"] = data.refresh_token
        os.environ["PIXIV_REFRESH_TOKEN"] = data.refresh_token
    if "image_source_dirs" in body or "image_source_dir" in body:
        source_dirs = data.image_source_dirs if "image_source_dirs" in body else [data.image_source_dir]
        source_dirs = [d.strip() for d in source_dirs if (d or "").strip()]
        joined_dirs = SOURCE_DIR_SEPARATOR.join(source_dirs)
        primary_dir = source_dirs[0] if source_dirs else ""
        updates["IMAGE_SOURCE_DIR"] = primary_dir
        updates["IMAGE_SOURCE_DIRS"] = joined_dirs
        os.environ["IMAGE_SOURCE_DIR"] = primary_dir
        os.environ["IMAGE_SOURCE_DIRS"] = joined_dirs
    if "proxy" in body:
        updates["PIXIV_PROXY"] = data.proxy
        os.environ["PIXIV_PROXY"] = data.proxy
        reset_pixiv_client()
    if "connection_mode" in body and data.connection_mode in ("direct", "proxy", "auto"):
        updates["PIXIV_MODE"] = data.connection_mode
        os.environ["PIXIV_MODE"] = data.connection_mode
        reset_pixiv_client()
    if "server_port" in body:
        port_str = (data.server_port or "").strip()
        if port_str:
            if port_str.isdigit() and 1 <= int(port_str) <= 65535:
                updates["PA_PORT"] = port_str
                os.environ["PA_PORT"] = port_str
            else:
                return _error_response("INVALID_PORT", "端口需为 1-65535 的整数",
                                       "请检查端口填写是否正确")
    if "access_token" in body:
        token_str = (data.access_token or "").strip()
        updates["PA_ACCESS_TOKEN"] = token_str
        os.environ["PA_ACCESS_TOKEN"] = token_str
        # 立即生效：留空 + 局域网模式则自动生成新令牌
        if token_str:
            globals()["ACCESS_TOKEN"] = token_str
        elif LAN_MODE:
            globals()["ACCESS_TOKEN"] = secrets.token_urlsafe(12)
        else:
            globals()["ACCESS_TOKEN"] = ""
    if "sync_delay_ms" in body:
        delay_str = (data.sync_delay_ms or "").strip()
        if delay_str:
            if delay_str.isdigit() and 0 <= int(delay_str) <= _SYNC_DELAY_MAX:
                updates["SYNC_DELAY_MS"] = str(int(delay_str))
                os.environ["SYNC_DELAY_MS"] = str(int(delay_str))
            else:
                return _error_response("INVALID_DELAY", "同步间隔需为 0-10000 的整数（毫秒）",
                                       "请检查填写是否正确，0 表示不限速")
    if "image_mirror" in body:
        mirror = (data.image_mirror or "").strip()
        if mirror:
            mirror = re.sub(r"^https?://", "", mirror).strip().rstrip("/")
            if re.search(r"\s", mirror) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9.-]*(/[^\s]*)?$", mirror):
                return _error_response("INVALID_MIRROR", "镜像域名无效",
                                       "请填写形如 i.pixiv.re 的域名（可带路径前缀），不要包含空格")
        updates["PIXIV_IMAGE_MIRROR"] = mirror
        os.environ["PIXIV_IMAGE_MIRROR"] = mirror
    if "auto_watch_enabled" in body:
        auto_watch = "1" if data.auto_watch_enabled else "0"
        updates["AUTO_WATCH_ENABLED"] = auto_watch
        os.environ["AUTO_WATCH_ENABLED"] = auto_watch
    _write_env_file(updates)
    watcher_result = _restart_folder_watcher()
    return {"status": "ok", "watcher": watcher_result}


@app.post("/api/settings/refresh-ips")
def api_refresh_ips():
    from app.pixiv import refresh_direct_ips
    updated = refresh_direct_ips()
    return {"status": "ok", "updated": updated}


@app.get("/image/{filepath:path}")
def serve_image(filepath: str):
    decoded = unquote(filepath)
    # 只允许读取已登记在库的作品图片，防止局域网模式下被利用读取任意本地文件
    if not _is_registered_image(decoded):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(decoded)


def _is_registered_image(path):
    try:
        with get_db() as conn:
            row = conn.execute("SELECT 1 FROM images WHERE path = ?", (path,)).fetchone()
            return row is not None
    except Exception:
        return False


@app.get("/artwork/{artwork_id}", response_class=HTMLResponse)
def artwork_page(request: Request, artwork_id: int):
    return templates.TemplateResponse(
        request, "artwork.html", {"artwork_id": artwork_id}
    )


@app.get("/author/{author_id}", response_class=HTMLResponse)
def author_page(request: Request, author_id: int):
    return templates.TemplateResponse(
        request, "author.html", {"author_id": author_id}
    )


@app.get("/tag/{tag_name:path}", response_class=HTMLResponse)
def tag_page(request: Request, tag_name: str):
    return templates.TemplateResponse(
        request, "tag.html", {"tag_name": tag_name}
    )


if __name__ == "__main__":
    print("Use run.py in the project root to start the server.")
