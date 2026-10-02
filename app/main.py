import json
import os
import re
import subprocess
import sys
from urllib.parse import unquote
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import paths
from app import jobs
from app.database import get_db, init_db
from app.scanner import scan_directory
from app.sync import sync_metadata
from app.download import check_bookmark_subscription
from app.pixiv import reset_pixiv_client, fetch_profile_image, get_pixiv_client

load_dotenv(paths.ENV_FILE)

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


async def _json_body(request: Request):
    """解析 JSON 请求体，非法输入返回 400 而不是抛异常。

    自研 ASGI 服务器在未处理异常时无法把响应发回客户端（连接被直接关闭），
    因此所有手写解析 JSON 的接口都走这里，把错误变成正常的 400 响应。
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体不是合法的 JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    return body


app = FastAPI(title="Pixiv Archive")


@app.exception_handler(ValidationError)
async def _validation_error_handler(request: Request, exc: ValidationError):
    """接口内部手工构造 pydantic 模型时的校验失败，统一返回 422。

    FastAPI 只会把请求参数校验错误转成 422；形如 `Model(**body)` 抛出的
    ValidationError 属于未处理异常，默认会被包装成 500（客户端错误被报成服务端错误）。
    """
    return JSONResponse(
        {"detail": jsonable_encoder(exc.errors())}, status_code=422
    )


THUMBNAIL_PATH = os.path.join(paths.DATA_DIR, paths.THUMBNAIL_DIR)

# 自定义 ASGI 服务器不会触发 FastAPI 的 lifespan/startup 事件，
# 这里在模块加载时显式建库建表，确保全新环境可直接使用。
init_db()

os.makedirs(THUMBNAIL_PATH, exist_ok=True)
app.mount("/thumbnails", StaticFiles(directory=THUMBNAIL_PATH), name="thumbnails")

static_dir = os.path.join(paths.APP_DIR, "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

templates_dir = os.path.join(paths.APP_DIR, "templates")
templates = Jinja2Templates(directory=templates_dir)


@app.on_event("startup")
def startup():
    init_db()


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


def _random_order_sql(seed: int) -> str:
    """按 seed 确定的伪随机排序（不用 ORDER BY RANDOM()）。

    同一 seed 下整个结果的顺序完全稳定，因此与 LIMIT/OFFSET 组合翻页不会重复或漏项。
    实现要点：先把行相关的 pixiv_id 哈希降到 [0, 2147483647)，再用 seed 派生的奇数
    乘子做二次混合。两处中间值都 < 2^31，乘积 < 2^62，不依赖 pixiv_id 的量级即可
    避免 64 位溢出。注意 seed 必须乘在行相关项上——若只做 "hash + seed" 的常量偏移，
    除极少数跨模回绕的行外整体顺序不变，换个 seed 等于没换。
    """
    mult = 2 * (seed % 1073741823) + 1
    return (
        "ORDER BY (((a.pixiv_id * 2654435761) % 2147483647) * "
        f"{mult}) % 2147483647, a.id"
    )


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
    seed: int = Query(0),
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
        count_row = conn.execute(
            f"SELECT COUNT(*) FROM artworks a {where_sql}", params
        ).fetchone()
        total = count_row[0]

        offset = (page - 1) * per_page
        if sort == "random":
            order_sql = _random_order_sql(seed)
        else:
            sort_column = {
                "id": "a.id", "pixiv_id": "a.pixiv_id",
                "title": "a.title", "create_date": "a.create_date",
                "first_seen": "a.first_seen",
            }[sort]
            order_direction = "DESC" if order == "desc" else "ASC"
            order_sql = f"ORDER BY {sort_column} {order_direction}"

        rows = conn.execute(
            f"""SELECT a.*, i.path AS thumb_path,
                a.id IN (SELECT fa.artwork_id FROM favorite_artworks fa) AS is_favorited,
                {_r18_exists()} AS is_r18
                FROM artworks a
                LEFT JOIN images i ON a.id = i.artwork_id AND i.page = 0
                {where_sql}
                {order_sql}
                LIMIT ? OFFSET ?""",
            params + [per_page, offset],
        ).fetchall()

        artworks = []
        for row in rows:
            artworks.append({
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
            })

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
        # 缩略图与元数据 JSON 是库内作品的派生物。作品记录删除后它们即失去引用，
        # 与扫描时的缺失清理保持一致一并删除，避免残留孤儿文件。
        paths.remove_side_files(pixiv_id["pixiv_id"])

        return {"status": "deleted", "artwork_id": artwork_id, "files_deleted": delete_files}


@app.post("/api/artworks/batch-delete")
async def api_batch_delete_artworks(request: Request):
    from pydantic import BaseModel

    class BatchDelete(BaseModel):
        artwork_ids: list[int]
        delete_files: bool = False

    body = await _json_body(request)
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
            paths.remove_side_files(pixiv_id["pixiv_id"])
            deleted += 1
    return {"status": "ok", "deleted": deleted, "files_deleted": data.delete_files}


@app.post("/api/artworks/batch-favorites-clear")
async def api_batch_favorites_clear(request: Request):
    from pydantic import BaseModel

    class BatchClear(BaseModel):
        artwork_ids: list[int]

    body = await _json_body(request)
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
    seed: int = Query(0),
):
    query = q.strip()
    if not query:
        return api_artworks(page=page, per_page=per_page, sort=sort, order=order,
                            author=author, tag=tag, favorite_id=favorite_id,
                            r18=r18, status=status, seed=seed)

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

        count_row = conn.execute(
            f"SELECT COUNT(*) FROM artworks a{where_sql}", params
        ).fetchone()
        total = count_row[0]

        offset = (page - 1) * per_page
        if sort == "random":
            order_sql = _random_order_sql(seed)
        else:
            sort_column = {
                "id": "a.id", "pixiv_id": "a.pixiv_id",
                "title": "a.title", "create_date": "a.create_date",
                "first_seen": "a.first_seen",
            }[sort]
            order_direction = "DESC" if order == "desc" else "ASC"
            order_sql = f"ORDER BY {sort_column} {order_direction}"

        rows = conn.execute(
            f"""SELECT a.*, i.path AS thumb_path,
                a.id IN (SELECT fa.artwork_id FROM favorite_artworks fa) AS is_favorited,
                {_r18_exists()} AS is_r18
                FROM artworks a
                LEFT JOIN images i ON a.id = i.artwork_id AND i.page = 0
                {where_sql}
                {order_sql}
                LIMIT ? OFFSET ?""",
            params + [per_page, offset],
        ).fetchall()

        artworks = []
        for row in rows:
            artworks.append({
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
            })

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
                    LEFT JOIN images i ON a.id = i.artwork_id AND i.page = 0
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

    body = await _json_body(request)
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

    body = await _json_body(request)
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

    body = await _json_body(request)
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
                   LEFT JOIN images i ON a.id = i.artwork_id AND i.page = 0
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

    body = await _json_body(request)
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


@app.post("/api/scan")
def api_scan():
    if jobs.is_busy():
        return _busy_error()

    source_dir = os.getenv("IMAGE_SOURCE_DIR", "")
    if not source_dir:
        return _error_response("NO_SOURCE_DIR", "未设置本地图片目录",
                               "请到 设置 → 本地图片目录 填写后重试")
    if not os.path.isdir(source_dir):
        return _error_response("SOURCE_DIR_NOT_FOUND", f"图片目录不存在：{source_dir}",
                               "请检查设置中的目录路径是否正确")

    def run_scan(job):
        from app.thumbnails import generate_all_thumbnails

        job.update(phase="scan", message="开始扫描…")
        result = scan_directory(source_dir, job.update, job.cancel_event)
        if result.get("error"):
            raise RuntimeError(result["error"])
        if not result.get("cancelled") and result.get("new_artworks", 0) > 0:
            job.update(phase="thumb", message="扫描完成，正在生成缩略图…")
            with get_db() as conn:
                thumb = generate_all_thumbnails(conn, job.update, job.cancel_event)
                result["thumbnails"] = thumb
        job.state["result"] = result

    job_id, error = jobs.start("scan", run_scan)
    if error:
        return _busy_error()
    return {"job_id": job_id, "kind": "scan"}


# ===== 收藏订阅（订阅某用户的公开收藏列表）=====

def _bookmark_source_dir_check():
    """收藏订阅任务共用前置校验，返回 (source_dir, error_response)。"""
    if jobs.is_busy():
        return None, _busy_error()
    source_dir = os.getenv("IMAGE_SOURCE_DIR", "")
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
    from app.thumbnails import generate_all_thumbnails
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
        if not job.cancel_event.is_set() and scan_result.get("new_artworks", 0) > 0:
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
    body = await _json_body(request)
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


@app.post("/api/bookmark-subs/check")
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


@app.post("/api/sync")
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


def _read_env_file():
    env_path = paths.ENV_FILE
    if not os.path.exists(env_path):
        return {}
    result = {}
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                result[key.strip()] = value.strip()
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
    return {
        "has_token": bool(token),
        "token_preview": token[:8] + "..." if len(token) > 8 else token,
        "image_source_dir": settings.get("IMAGE_SOURCE_DIR", ""),
        "proxy": settings.get("PIXIV_PROXY", ""),
        "connection_mode": settings.get("PIXIV_MODE", "auto"),
        "image_mirror": settings.get("PIXIV_IMAGE_MIRROR", ""),
        "server_port": int(port_str) if port_str.isdigit() else DEFAULT_PORT,
        "sync_delay_ms": _parse_sync_delay(settings.get("SYNC_DELAY_MS", "")),
    }


@app.post("/api/settings")
async def api_update_settings(request: Request):
    from pydantic import BaseModel

    class SettingsUpdate(BaseModel):
        refresh_token: str = ""
        image_source_dir: str = ""
        proxy: str = ""
        connection_mode: str = ""
        server_port: str = ""
        sync_delay_ms: str = ""
        image_mirror: str = ""

    body = await _json_body(request)
    data = SettingsUpdate(**body)
    updates = {}
    if data.refresh_token:
        updates["PIXIV_REFRESH_TOKEN"] = data.refresh_token
        os.environ["PIXIV_REFRESH_TOKEN"] = data.refresh_token
    if data.image_source_dir:
        updates["IMAGE_SOURCE_DIR"] = data.image_source_dir
        os.environ["IMAGE_SOURCE_DIR"] = data.image_source_dir
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
    _write_env_file(updates)
    return {"status": "ok"}


@app.post("/api/settings/refresh-ips")
def api_refresh_ips():
    from app.pixiv import refresh_direct_ips
    updated = refresh_direct_ips()
    return {"status": "ok", "updated": updated}


@app.get("/image/{filepath:path}")
def serve_image(filepath: str):
    decoded = unquote(filepath)
    # 只允许读取已登记在库的作品图片，防止被利用读取任意本地文件
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