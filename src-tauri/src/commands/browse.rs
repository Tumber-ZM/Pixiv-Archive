use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use rusqlite::Connection;
use tauri::{AppHandle, Emitter, State};

use crate::commands::download;
use crate::commands::task::TaskCancel;
use crate::db;
use crate::error::{AppError, AppResult};
use crate::models::{AuthorRow, BookmarkSub, SubPreview, TagRow};
use crate::net::pixiv::PixivClient;
use crate::platform::paths;

fn open_library() -> AppResult<Connection> {
    let path = paths::db_path()?;
    if !path.is_file() {
        return Err(AppError::not_found("数据库尚未初始化，请先导入旧数据或扫描目录"));
    }
    db::open(&path)
}

fn like_pattern(q: Option<&str>) -> Option<String> {
    let keyword = q.map(str::trim).filter(|s| !s.is_empty())?;
    Some(format!("%{}%", crate::db::query::escape_like(keyword)))
}

/// 作者列表。每个作者带上最近 3 件作品的缩略图，供列表卡片做封面条。
///
/// 三个相关子查询都走 `idx_artworks_author_id`，几百个作者的量级可以直接接受；
/// 如果作者数上万再考虑改成窗口函数一次分组。
pub fn list_authors_with(
    conn: &Connection,
    data_dir: &Path,
    q: Option<&str>,
) -> AppResult<Vec<AuthorRow>> {
    let pattern = like_pattern(q);

    let sql = "SELECT au.id, au.pixiv_user_id, au.name, COUNT(a.id) AS artwork_count, au.profile_image,
                      (SELECT w.pixiv_id FROM artworks w WHERE w.author_id = au.id ORDER BY w.id DESC LIMIT 1) AS c1,
                      (SELECT w.pixiv_id FROM artworks w WHERE w.author_id = au.id ORDER BY w.id DESC LIMIT 1 OFFSET 1) AS c2,
                      (SELECT w.pixiv_id FROM artworks w WHERE w.author_id = au.id ORDER BY w.id DESC LIMIT 1 OFFSET 2) AS c3
               FROM authors au
               LEFT JOIN artworks a ON a.author_id = au.id
               WHERE (?1 IS NULL OR au.name LIKE ?1 ESCAPE '\\')
               GROUP BY au.id
               HAVING COUNT(a.id) > 0
               ORDER BY artwork_count DESC, au.name";

    let mut stmt = conn.prepare(sql)?;
    let rows = stmt.query_map([pattern.as_deref()], |row| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, i64>(3)?,
            row.get::<_, Option<String>>(4)?,
            [
                row.get::<_, Option<i64>>(5)?,
                row.get::<_, Option<i64>>(6)?,
                row.get::<_, Option<i64>>(7)?,
            ],
        ))
    })?;

    let mut out = Vec::new();
    for row in rows {
        let (id, pixiv_user_id, name, artwork_count, profile_image, cover_ids) = row?;
        let covers = cover_ids
            .into_iter()
            .flatten()
            .filter_map(|pixiv_id| {
                let candidate = data_dir.join("thumbnails").join(format!("{pixiv_id}.jpg"));
                candidate
                    .is_file()
                    .then(|| candidate.to_string_lossy().to_string())
            })
            .collect();

        out.push(AuthorRow {
            id,
            pixiv_user_id,
            name,
            artwork_count,
            avatar_path: {
                // 优先本地缓存的头像（同步/订阅时下到 avatars/），其次旧库里的 URL
                let cached = data_dir.join("avatars").join(format!("{pixiv_user_id}.png"));
                if cached.is_file() {
                    Some(cached.to_string_lossy().to_string())
                } else {
                    profile_image.filter(|value| !value.trim().is_empty())
                }
            },
            covers,
        });
    }
    Ok(out)
}

/// 标签列表。每个标签附一张**随机**作品缩略图做封面。
///
/// 封面子查询必须限定在已 LIMIT 的结果集内，否则几千个标签会各跑一次随机排序。
/// 先用 CTE 排序截断，再对剩下的行取封面。
pub fn list_tags_with(
    conn: &Connection,
    data_dir: &Path,
    q: Option<&str>,
    limit: u32,
) -> AppResult<Vec<TagRow>> {
    let pattern = like_pattern(q);

    let sql = "WITH ranked AS (
                   SELECT t.id, t.name, t.translated_name, COUNT(at.artwork_id) AS usage
                   FROM tags t
                   JOIN artwork_tags at ON at.tag_id = t.id
                   JOIN artworks w ON w.id = at.artwork_id
                   WHERE (?1 IS NULL OR t.name LIKE ?1 ESCAPE '\\' OR t.translated_name LIKE ?1 ESCAPE '\\')
                   GROUP BY t.id
                   HAVING COUNT(at.artwork_id) > 0
                   ORDER BY usage DESC, t.name
                   LIMIT ?2
               )
               SELECT r.id, r.name, r.translated_name, r.usage,
                 (SELECT w.pixiv_id
                    FROM artwork_tags at2
                    JOIN artworks w ON w.id = at2.artwork_id
                   WHERE at2.tag_id = r.id
                   ORDER BY RANDOM() LIMIT 1) AS cover_pixiv_id
               FROM ranked r
               ORDER BY r.usage DESC, r.name";

    let mut stmt = conn.prepare(sql)?;
    let rows = stmt.query_map(rusqlite::params![pattern.as_deref(), limit], |row| {
        let cover_pixiv_id: Option<i64> = row.get(4)?;
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, i64>(3)?,
            cover_pixiv_id,
        ))
    })?;

    let mut out = Vec::new();
    for row in rows {
        let (id, name, translated_name, count, cover_pixiv_id) = row?;
        // 封面统一走 400px 缩略图，不直接引原图
        let cover_path = cover_pixiv_id.and_then(|pixiv_id| {
            let candidate = data_dir.join("thumbnails").join(format!("{pixiv_id}.jpg"));
            candidate
                .is_file()
                .then(|| candidate.to_string_lossy().to_string())
        });
        out.push(TagRow {
            id,
            name,
            translated_name,
            count,
            cover_path,
        });
    }
    Ok(out)
}

/// 头像优先取 `data_dir/avatars/<pixiv_user_id>.png`，
/// 回退到 `authors.profile_image`（旧库可能存了本地路径）。
pub fn author_avatar_with(
    conn: &Connection,
    data_dir: &Path,
    author_id: i64,
) -> AppResult<Option<String>> {
    let (pixiv_user_id, profile_image): (i64, Option<String>) = conn
        .query_row(
            "SELECT pixiv_user_id, profile_image FROM authors WHERE id = ?1",
            [author_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(|_| AppError::not_found(format!("作者 {author_id} 不存在")))?;

    let candidate = data_dir
        .join("avatars")
        .join(format!("{pixiv_user_id}.png"));
    if candidate.is_file() {
        return Ok(Some(candidate.to_string_lossy().to_string()));
    }

    if let Some(path) = profile_image.filter(|p| !p.is_empty()) {
        if Path::new(&path).is_file() {
            return Ok(Some(path));
        }
    }

    Ok(None)
}

fn sub_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<BookmarkSub> {
    Ok(BookmarkSub {
        pixiv_user_id: row.get(0)?,
        name: row.get(1)?,
        last_pid: row.get(2)?,
        auto_download: row.get::<_, i64>(3)? != 0,
        last_checked: row.get(4)?,
        last_result: row.get(5)?,
        created_at: row.get::<_, Option<String>>(6)?.unwrap_or_default(),
    })
}

const SUB_COLUMNS: &str =
    "pixiv_user_id, name, last_pid, COALESCE(auto_download, 1), last_checked, last_result, created_at";

pub fn list_subs_with(conn: &Connection) -> AppResult<Vec<BookmarkSub>> {
    let mut stmt =
        conn.prepare(&format!("SELECT {SUB_COLUMNS} FROM bookmark_subs ORDER BY created_at DESC"))?;
    let rows = stmt.query_map([], sub_row)?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

fn sub_of(conn: &Connection, pixiv_user_id: i64) -> AppResult<BookmarkSub> {
    conn.query_row(
        &format!("SELECT {SUB_COLUMNS} FROM bookmark_subs WHERE pixiv_user_id = ?1"),
        [pixiv_user_id],
        sub_row,
    )
    .map_err(|_| AppError::not_found(format!("订阅 {pixiv_user_id} 不存在")))
}

pub fn add_sub_with(conn: &Connection, pixiv_user_id: i64) -> AppResult<BookmarkSub> {
    if pixiv_user_id <= 0 {
        return Err(AppError::invalid("Pixiv 用户 ID 必须是正整数"));
    }
    conn.execute(
        "INSERT OR IGNORE INTO bookmark_subs (pixiv_user_id, auto_download) VALUES (?1, 1)",
        [pixiv_user_id],
    )?;
    sub_of(conn, pixiv_user_id)
}

pub fn remove_sub_with(conn: &Connection, pixiv_user_id: i64) -> AppResult<bool> {
    // 只删订阅记录，已下载的作品保留（01-范围与功能清单.md §8）
    let affected = conn.execute(
        "DELETE FROM bookmark_subs WHERE pixiv_user_id = ?1",
        [pixiv_user_id],
    )?;
    Ok(affected > 0)
}

pub fn toggle_sub_auto_with(conn: &Connection, pixiv_user_id: i64) -> AppResult<BookmarkSub> {
    conn.execute(
        "UPDATE bookmark_subs SET auto_download = 1 - COALESCE(auto_download, 1) WHERE pixiv_user_id = ?1",
        [pixiv_user_id],
    )?;
    sub_of(conn, pixiv_user_id)
}

/// 订阅预览：查 Pixiv 用户详情拿昵称。查不到（无 token / 网络失败）就留空，
/// 前端退回显示「用户 <id>」，不阻塞订阅流程。
pub async fn preview_sub(pixiv_user_id: i64) -> AppResult<SubPreview> {
    if pixiv_user_id <= 0 {
        return Err(AppError::invalid("Pixiv 用户 ID 必须是正整数"));
    }
    let name = fetch_sub_name(pixiv_user_id).await.unwrap_or_default();
    Ok(SubPreview {
        pixiv_user_id,
        name,
        total_bookmarks: None,
    })
}

/// 用 `/v1/user/detail` 补昵称。无 token 或网络失败返回 None，调用方静默跳过。
async fn fetch_sub_name(pixiv_user_id: i64) -> Option<String> {
    let client = PixivClient::from_settings().ok()?;
    client.user_name(pixiv_user_id).await.ok().flatten()
}

fn store_sub_name(conn: &Connection, pixiv_user_id: i64, name: &str) {
    let _ = conn.execute(
        "UPDATE bookmark_subs SET name = ?1 WHERE pixiv_user_id = ?2",
        rusqlite::params![name.trim(), pixiv_user_id],
    );
}

#[tauri::command]
pub fn list_authors(q: Option<String>) -> AppResult<Vec<AuthorRow>> {
    let conn = open_library()?;
    let data_dir = paths::data_dir()?;
    list_authors_with(&conn, &data_dir, q.as_deref())
}

#[tauri::command]
pub fn list_tags(q: Option<String>) -> AppResult<Vec<TagRow>> {
    let conn = open_library()?;
    let data_dir = paths::data_dir()?;
    // 标签可能上千个，先按使用次数截断再取封面
    list_tags_with(&conn, &data_dir, q.as_deref(), 300)
}

#[tauri::command]
pub fn get_author_avatar(author_id: i64) -> AppResult<Option<String>> {
    let conn = open_library()?;
    let data_dir = paths::data_dir()?;
    author_avatar_with(&conn, &data_dir, author_id)
}

/// 库里还没有昵称的行（老数据 / 订阅时离线）。空字符串也算缺失。
fn needs_name(name: Option<&str>) -> bool {
    name.map(|value| value.trim().is_empty()).unwrap_or(true)
}

#[tauri::command]
pub async fn list_bookmark_subs() -> AppResult<Vec<BookmarkSub>> {
    // 老数据（以及订阅时离线的情况）name 是空的：逐个补一次昵称，成功后写回库。
    let missing: Vec<i64> = {
        let conn = open_library()?;
        list_subs_with(&conn)?
            .into_iter()
            .filter(|sub| needs_name(sub.name.as_deref()))
            .map(|sub| sub.pixiv_user_id)
            .collect()
    };

    if !missing.is_empty() {
        // 补昵称只是锦上添花，不能把订阅列表卡在网络上：没有 token 直接跳过，
        // 有 token 时整段最多等 8 秒，超时就先显示「未知用户」，下次再补。
        let has_token = crate::commands::settings::view()
            .map(|view| view.has_token)
            .unwrap_or(false);
        if has_token {
            let _ = tokio::time::timeout(
                std::time::Duration::from_secs(8),
                backfill_sub_names(&missing),
            )
            .await;
        }
    }

    let conn = open_library()?;
    list_subs_with(&conn)
}

/// 逐个把缺失的订阅昵称补进库；失败/超时都由调用方吞掉。
async fn backfill_sub_names(ids: &[i64]) {
    let Ok(client) = PixivClient::from_settings() else {
        return;
    };
    for pixiv_user_id in ids {
        let Ok(Some(name)) = client.user_name(*pixiv_user_id).await else {
            continue;
        };
        if let Ok(conn) = open_library() {
            store_sub_name(&conn, *pixiv_user_id, &name);
        }
    }
}

#[tauri::command]
pub async fn add_bookmark_sub(pixiv_user_id: i64, _check_now: bool) -> AppResult<BookmarkSub> {
    {
        let conn = open_library()?;
        add_sub_with(&conn, pixiv_user_id)?;
    }

    // 订阅本身不依赖网络；能连上 Pixiv 就顺手把昵称补上
    if let Some(name) = fetch_sub_name(pixiv_user_id).await {
        if let Ok(conn) = open_library() {
            store_sub_name(&conn, pixiv_user_id, &name);
        }
    }

    let conn = open_library()?;
    sub_of(&conn, pixiv_user_id)
}

#[tauri::command]
pub fn remove_bookmark_sub(pixiv_user_id: i64) -> AppResult<bool> {
    let conn = open_library()?;
    remove_sub_with(&conn, pixiv_user_id)
}

#[tauri::command]
pub fn toggle_bookmark_sub_auto(pixiv_user_id: i64) -> AppResult<BookmarkSub> {
    let conn = open_library()?;
    toggle_sub_auto_with(&conn, pixiv_user_id)
}

#[tauri::command]
pub async fn preview_bookmark_sub(pixiv_user_id: i64) -> AppResult<SubPreview> {
    preview_sub(pixiv_user_id).await
}

/// 重新获取所有作者的头像（按 `pixiv_user_id` 调 `/v1/user/detail`）。
///
/// 会覆盖本地缓存：点这个按钮通常就是因为某个头像缺失或不对。
#[tauri::command]
pub async fn refresh_author_avatars(
    app: AppHandle,
    state: State<'_, TaskCancel>,
) -> AppResult<usize> {
    let Some(cancel) = state.begin("avatar") else {
        return Err(AppError::invalid("已有任务在运行"));
    };
    let outcome = refresh_avatars_inner(&app, &cancel).await;
    state.end("avatar");
    outcome
}

async fn refresh_avatars_inner(app: &AppHandle, cancel: &Arc<AtomicBool>) -> AppResult<usize> {
    let client = PixivClient::from_settings()?;
    let data_dir = paths::data_dir()?;
    let conn = open_library()?;
    let delay_ms = crate::commands::settings::view()?.sync_delay_ms as u64;

    let authors: Vec<(i64, Option<String>)> = {
        let mut stmt = conn.prepare(
            "SELECT pixiv_user_id, profile_image FROM authors WHERE pixiv_user_id > 0 ORDER BY id",
        )?;
        let rows = stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?;
        rows.filter_map(Result::ok).collect()
    };

    let total = authors.len();
    let mut updated = 0usize;

    for (index, (pixiv_user_id, profile_image)) in authors.iter().enumerate() {
        if cancel.load(Ordering::Relaxed) {
            break;
        }
        let _ = app.emit(
            "sub-progress",
            &serde_json::json!({
                "current": index,
                "total": total,
                "message": format!("更新作者头像 {}/{}", index + 1, total),
            }),
        );

        let url = match client.user_avatar_url(*pixiv_user_id).await {
            Ok(Some(url)) => Some(url),
            _ => profile_image.clone(),
        };
        let Some(url) = url else {
            continue;
        };

        // 覆盖旧文件，保证「更新」真的能换掉不对的头像
        let target = data_dir
            .join("avatars")
            .join(format!("{pixiv_user_id}.png"));
        let _ = std::fs::remove_file(&target);
        if download::ensure_avatar(&client, &data_dir, *pixiv_user_id, Some(url.as_str()), None)
            .await
            .unwrap_or(false)
        {
            updated += 1;
        }
        let _ = conn.execute(
            "UPDATE authors SET profile_image = ?1 WHERE pixiv_user_id = ?2",
            rusqlite::params![url, pixiv_user_id],
        );

        if delay_ms > 0 {
            tokio::time::sleep(std::time::Duration::from_millis(delay_ms)).await;
        }
    }

    Ok(updated)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn memory_db() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(crate::db::schema::SCHEMA_SQL).unwrap();
        conn.execute(
            "INSERT INTO authors (pixiv_user_id, name) VALUES (1, '多い作者')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO authors (pixiv_user_id, name) VALUES (2, '少ない作者')",
            [],
        )
        .unwrap();
        for i in 0..3 {
            conn.execute(
                "INSERT INTO artworks (pixiv_id, title, author_id) VALUES (?1, ?2, 1)",
                rusqlite::params![100 + i, format!("多{i}")],
            )
            .unwrap();
        }
        conn.execute(
            "INSERT INTO artworks (pixiv_id, title, author_id) VALUES (200, '少0', 2)",
            [],
        )
        .unwrap();

        conn.execute("INSERT INTO tags (name, translated_name) VALUES ('R-18', '成人')", [])
            .unwrap();
        conn.execute("INSERT INTO tags (name) VALUES ('风景')", []).unwrap();
        conn.execute("INSERT INTO artwork_tags (artwork_id, tag_id) VALUES (1, 1)", [])
            .unwrap();
        conn.execute("INSERT INTO artwork_tags (artwork_id, tag_id) VALUES (2, 1)", [])
            .unwrap();
        conn.execute("INSERT INTO artwork_tags (artwork_id, tag_id) VALUES (3, 2)", [])
            .unwrap();
        conn
    }

    #[test]
    fn authors_ordered_by_artwork_count() {
        let conn = memory_db();
        let dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("test-tmp");
        std::fs::create_dir_all(dir.join("thumbnails")).unwrap();

        let rows = list_authors_with(&conn, &dir, None).unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].name, "多い作者");
        assert_eq!(rows[0].artwork_count, 3);
        assert_eq!(rows[1].artwork_count, 1);
    }

    #[test]
    fn authors_and_tags_disappear_after_artworks_deleted() {
        let conn = memory_db();
        let dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("test-tmp");

        // 只挂在「作者 2 的唯一作品」（artwork id = 4）上的标签
        conn.execute("INSERT INTO tags (name) VALUES ('孤标签')", []).unwrap();
        conn.execute("INSERT INTO artwork_tags (artwork_id, tag_id) VALUES (4, 3)", [])
            .unwrap();

        conn.execute("DELETE FROM artworks WHERE author_id = 2", []).unwrap();

        let authors = list_authors_with(&conn, &dir, None).unwrap();
        assert_eq!(authors.len(), 1, "没有作品的作者不该再出现在列表里");
        assert_eq!(authors[0].name, "多い作者");

        // 残留的 artwork_tags 行（无外键级联时）不能把标签留在列表里
        let tags = list_tags_with(&conn, &dir, None, 300).unwrap();
        assert!(
            tags.iter().all(|tag| tag.name != "孤标签"),
            "没有作品的标签不该再出现在列表里"
        );
    }

    #[test]
    fn authors_search_filters() {
        let conn = memory_db();
        let dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("test-tmp");
        let rows = list_authors_with(&conn, &dir, Some("少な")).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].name, "少ない作者");
    }

    #[test]
    fn tags_ordered_by_usage_and_searchable_by_translation() {
        let conn = memory_db();
        let dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("test-tmp");
        std::fs::create_dir_all(dir.join("thumbnails")).unwrap();

        let rows = list_tags_with(&conn, &dir, None, 300).unwrap();
        assert_eq!(rows[0].name, "R-18");
        assert_eq!(rows[0].count, 2);

        // 译名也要能搜到
        let by_translation = list_tags_with(&conn, &dir, Some("成人"), 300).unwrap();
        assert_eq!(by_translation.len(), 1);
        assert_eq!(by_translation[0].name, "R-18");
    }

    #[test]
    fn tags_limit_is_applied() {
        let conn = memory_db();
        let dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("test-tmp");
        let rows = list_tags_with(&conn, &dir, None, 1).unwrap();
        assert_eq!(rows.len(), 1, "limit 应限制返回条数");
        assert_eq!(rows[0].name, "R-18", "应保留使用次数最高的标签");
    }

    #[test]
    fn subscriptions_add_toggle_remove() {
        let conn = memory_db();

        let added = add_sub_with(&conn, 777).unwrap();
        assert!(added.auto_download);
        assert_eq!(added.pixiv_user_id, 777);

        // 重复添加是幂等的
        let again = add_sub_with(&conn, 777).unwrap();
        assert!(again.auto_download);
        assert_eq!(list_subs_with(&conn).unwrap().len(), 1);

        let toggled = toggle_sub_auto_with(&conn, 777).unwrap();
        assert!(!toggled.auto_download);

        assert!(remove_sub_with(&conn, 777).unwrap());
        assert!(list_subs_with(&conn).unwrap().is_empty());

        // 退订后作品仍在（这里只是确认不会误删）
        assert!(!remove_sub_with(&conn, 777).unwrap());
    }

    #[test]
    fn rejects_invalid_user_id() {
        let conn = memory_db();
        assert_eq!(add_sub_with(&conn, 0).unwrap_err().code, "INVALID_INPUT");
    }

    #[test]
    fn name_backfill_only_targets_blank_rows() {
        assert!(needs_name(None));
        assert!(needs_name(Some("")));
        assert!(needs_name(Some("   ")));
        assert!(!needs_name(Some("Tumber")));
    }
}