use std::collections::HashSet;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use rusqlite::{params, Connection};
use tauri::{AppHandle, Emitter, State};

use super::task::TaskCancel;
use crate::db;
use crate::error::{AppError, AppResult};
use crate::net::pixiv::{IllustData, PixivClient};
use crate::platform::paths;

pub const SYNC_PROGRESS_EVENT: &str = "sync-progress";
/// 每批处理多少件再发一次进度，避免逐件 emit 把前端刷爆
const SYNC_BATCH: usize = 200;

#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct SyncReport {
    pub total: usize,
    pub synced: usize,
    pub deleted: usize,
    pub failed: usize,
    /// 认证失败时整批中止，把原因带回去
    pub auth_error: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct SyncProgress {
    pub current: usize,
    pub total: usize,
    pub synced: usize,
    pub message: String,
}

struct Target {
    id: i64,
    pixiv_id: i64,
}

fn open_library() -> AppResult<Connection> {
    let path = paths::db_path()?;
    let data_dir = paths::data_dir()?;
    std::fs::create_dir_all(&data_dir)?;
    if !path.is_file() {
        db::init(&path)?;
        return db::open(&path);
    }
    db::open(&path)
}

/// `full = false` 只补「从没同步过 / 上次失败 / 已到重试时间」的作品；
/// `full = true` 全量重同步，但跳过已确认删除的作品（要重试单件用右键同步）。
///
/// 结果只含 (id, pixiv_id)，几万条也只有几百 KB，一次取完再分批处理。
fn collect_targets(conn: &Connection, full: bool) -> AppResult<Vec<Target>> {
    let sql = if full {
        "SELECT id, pixiv_id FROM artworks
         WHERE pixiv_id > 0 AND COALESCE(pixiv_status, 'active') != 'deleted'
         ORDER BY COALESCE(last_synced, '') ASC, id ASC"
    } else {
        "SELECT id, pixiv_id FROM artworks
         WHERE pixiv_id > 0
           AND (last_synced IS NULL OR sync_error IS NOT NULL)
           AND (next_retry_at IS NULL OR next_retry_at <= datetime('now'))
         ORDER BY COALESCE(sync_attempts, 0) ASC, id ASC"
    };

    let mut stmt = conn.prepare(sql)?;
    let rows = stmt.query_map([], |row| {
        Ok(Target {
            id: row.get(0)?,
            pixiv_id: row.get(1)?,
        })
    })?;
    let mut targets = Vec::new();
    for row in rows {
        targets.push(row?);
    }
    Ok(targets)
}

/// 作者会改名、换头像：已存在也要更新，否则库里永远是第一次同步的值。
fn upsert_author(conn: &Connection, data: &IllustData) -> AppResult<Option<i64>> {
    if data.author_id <= 0 {
        return Ok(None);
    }
    conn.execute(
        "INSERT INTO authors (pixiv_user_id, name, profile_image) VALUES (?1, ?2, ?3)
         ON CONFLICT(pixiv_user_id) DO UPDATE SET
             name = excluded.name,
             profile_image = COALESCE(excluded.profile_image, authors.profile_image)",
        params![data.author_id, data.author_name, data.author_avatar],
    )?;
    let id: i64 = conn.query_row(
        "SELECT id FROM authors WHERE pixiv_user_id = ?1",
        [data.author_id],
        |row| row.get(0),
    )?;
    Ok(Some(id))
}

/// 标签按集合做差量：只删不再有的、只插新出现的，不再「全删再全插」。
fn upsert_tags(conn: &Connection, artwork_id: i64, data: &IllustData) -> AppResult<()> {
    let mut existing: HashSet<i64> = HashSet::new();
    {
        let mut stmt = conn.prepare("SELECT tag_id FROM artwork_tags WHERE artwork_id = ?1")?;
        let rows = stmt.query_map([artwork_id], |row| row.get::<_, i64>(0))?;
        for row in rows {
            existing.insert(row?);
        }
    }

    // 标签本体按名字复用；译名只补空值，不覆盖已有翻译
    for tag in &data.tags {
        conn.execute(
            "INSERT OR IGNORE INTO tags (name, translated_name) VALUES (?1, ?2)",
            params![tag.name, tag.translated_name],
        )?;
        if tag.translated_name.is_some() {
            let _ = conn.execute(
                "UPDATE tags SET translated_name = COALESCE(translated_name, ?1) WHERE name = ?2",
                params![tag.translated_name, tag.name],
            );
        }
    }

    let mut wanted: Vec<i64> = Vec::with_capacity(data.tags.len());
    {
        let mut stmt = conn.prepare("SELECT id FROM tags WHERE name = ?1")?;
        for tag in &data.tags {
            if let Ok(id) = stmt.query_row([&tag.name], |row| row.get::<_, i64>(0)) {
                wanted.push(id);
            }
        }
    }
    let wanted_set: HashSet<i64> = wanted.iter().copied().collect();

    for tag_id in existing.difference(&wanted_set) {
        conn.execute(
            "DELETE FROM artwork_tags WHERE artwork_id = ?1 AND tag_id = ?2",
            params![artwork_id, tag_id],
        )?;
    }
    for tag_id in &wanted {
        if existing.contains(tag_id) {
            continue;
        }
        conn.execute(
            "INSERT OR IGNORE INTO artwork_tags (artwork_id, tag_id) VALUES (?1, ?2)",
            params![artwork_id, tag_id],
        )?;
    }
    Ok(())
}

/// 把一次同步结果整段写库。事务保证不会出现「标签删了一半」的中间态。
pub fn apply_illust(conn: &mut Connection, artwork_id: i64, data: &IllustData) -> AppResult<()> {
    let tx = conn.transaction()?;
    let author_id = upsert_author(&tx, data)?;
    upsert_tags(&tx, artwork_id, data)?;
    tx.execute(
        "UPDATE artworks SET
            title = ?1, description = ?2, author_id = ?3, author_name = ?4,
            create_date = ?5, page_count = ?6, width = ?7, height = ?8,
            ai_type = ?9, pixiv_status = 'active',
            last_synced = datetime('now'),
            sync_error = NULL, sync_error_kind = NULL,
            sync_attempts = 0, next_retry_at = NULL
         WHERE id = ?10",
        params![
            data.title,
            data.description,
            author_id,
            data.author_name,
            data.create_date,
            data.page_count,
            data.width,
            data.height,
            data.ai_type,
            artwork_id,
        ],
    )?;
    tx.commit()?;
    Ok(())
}

/// 同步作品元数据（标题 / 作者 / 标签 / R18 / AI 类型）。
///
/// 判定与旧项目 `sync.py` 一致：
/// - 认证失败（AUTH）→ 中止整批，把原因带回前端
/// - 404 / 明确不存在（NOT_FOUND）→ 标记 `pixiv_status = 'deleted'`
/// - 其它失败 → 记分类与次数，按退避时间再试，不改状态
#[tauri::command]
pub async fn sync_metadata(
    app: AppHandle,
    state: State<'_, TaskCancel>,
    full: Option<bool>,
) -> AppResult<SyncReport> {
    let Some(cancel) = state.begin("sync") else {
        return Err(AppError::invalid("已有任务在运行"));
    };
    let outcome = run_sync(&app, &cancel, full.unwrap_or(false)).await;
    state.end("sync");
    outcome
}

/// 对指定作品强制重新同步元数据（不看 `last_synced`，右键/批量菜单用）。
///
/// 服务器返回作品已删除时**保留已有元数据**，只把 `pixiv_status` 标成 `deleted`，
/// 这样画廊里还能看到标题等信息，同时带上「已删除」角标。
#[tauri::command]
pub async fn sync_artworks(
    app: AppHandle,
    state: State<'_, TaskCancel>,
    artwork_ids: Vec<i64>,
) -> AppResult<SyncReport> {
    let Some(cancel) = state.begin("sync") else {
        return Err(AppError::invalid("已有任务在运行"));
    };
    let outcome = run_sync_selected(&app, &cancel, &artwork_ids).await;
    state.end("sync");
    outcome
}

async fn run_sync(app: &AppHandle, cancel: &Arc<AtomicBool>, full: bool) -> AppResult<SyncReport> {
    let client = PixivClient::from_settings()?;
    let mut conn = open_library()?;
    let data_dir = paths::data_dir()?;

    let targets = collect_targets(&conn, full)?;
    let total = targets.len();
    let mut report = SyncReport {
        total,
        ..Default::default()
    };

    let mut done = 0usize;
    for chunk in targets.chunks(SYNC_BATCH) {
        if cancel.load(Ordering::Relaxed) {
            break;
        }
        let _ = app.emit(
            SYNC_PROGRESS_EVENT,
            &SyncProgress {
                current: done,
                total,
                synced: report.synced,
                message: format!("同步元数据 {done}/{total}"),
            },
        );

        for target in chunk {
            if cancel.load(Ordering::Relaxed) {
                break;
            }
            done += 1;
            match sync_one(&mut conn, &client, &data_dir, target).await? {
                SyncOutcome::Synced => report.synced += 1,
                SyncOutcome::Deleted => report.deleted += 1,
                SyncOutcome::Failed => report.failed += 1,
                SyncOutcome::Auth(message) => {
                    report.failed += 1;
                    report.auth_error = Some(message);
                    emit_done(app, total, &report);
                    return Ok(report);
                }
            }
        }
    }

    emit_done(app, total, &report);
    Ok(report)
}

async fn run_sync_selected(
    app: &AppHandle,
    cancel: &Arc<AtomicBool>,
    artwork_ids: &[i64],
) -> AppResult<SyncReport> {
    let client = PixivClient::from_settings()?;
    let mut conn = open_library()?;
    let data_dir = paths::data_dir()?;

    let total = artwork_ids.len();
    let mut report = SyncReport {
        total,
        ..Default::default()
    };

    for (index, artwork_id) in artwork_ids.iter().enumerate() {
        if cancel.load(Ordering::Relaxed) {
            break;
        }
        if index % 20 == 0 || index + 1 == total {
            let _ = app.emit(
                SYNC_PROGRESS_EVENT,
                &SyncProgress {
                    current: index,
                    total,
                    synced: report.synced,
                    message: format!("同步元数据 {}/{}", index + 1, total),
                },
            );
        }

        let pixiv_id: Option<i64> = conn
            .query_row(
                "SELECT pixiv_id FROM artworks WHERE id = ?1",
                [artwork_id],
                |row| row.get(0),
            )
            .ok();
        let Some(pixiv_id) = pixiv_id.filter(|value| *value > 0) else {
            // 本地文件没有 Pixiv 元数据可同步
            report.failed += 1;
            continue;
        };

        let target = Target {
            id: *artwork_id,
            pixiv_id,
        };
        match sync_one(&mut conn, &client, &data_dir, &target).await? {
            SyncOutcome::Synced => report.synced += 1,
            SyncOutcome::Deleted => report.deleted += 1,
            SyncOutcome::Failed => report.failed += 1,
            SyncOutcome::Auth(message) => {
                report.failed += 1;
                report.auth_error = Some(message);
                break;
            }
        }
    }

    emit_done(app, total, &report);
    Ok(report)
}

enum SyncOutcome {
    Synced,
    Deleted,
    Failed,
    Auth(String),
}

/// 同步一件作品：成功写库、404 标记删除、其它错误记分类与退避时间。
async fn sync_one(
    conn: &mut Connection,
    client: &PixivClient,
    data_dir: &std::path::Path,
    target: &Target,
) -> AppResult<SyncOutcome> {
    match client.illust_detail(target.pixiv_id).await {
        Ok(data) => {
            // visible=false：已删除或作者设为非公开，API 仍返回详情但图片是占位图
            if !data.visible {
                mark_deleted(conn, target.id)?;
                return Ok(SyncOutcome::Deleted);
            }
            // 头像判定要看 apply_illust 之前的旧地址，所以先查再传值
            // （不能直接传 &Connection，会让 command 的 future 失去 Send）
            let stored_avatar: Option<String> = conn
                .query_row(
                    "SELECT profile_image FROM authors WHERE pixiv_user_id = ?1",
                    [data.author_id],
                    |row| row.get(0),
                )
                .ok()
                .flatten();
            let _ = crate::commands::download::ensure_avatar(
                client,
                data_dir,
                data.author_id,
                data.author_avatar.as_deref(),
                stored_avatar.as_deref(),
            )
            .await;
            if let Err(error) = apply_illust(conn, target.id, &data) {
                let _ = record_failure(conn, target.id, &error);
                return Ok(SyncOutcome::Failed);
            }
            let _ = crate::commands::download::save_raw_metadata(data_dir, &data);
            Ok(SyncOutcome::Synced)
        }
        Err(error) if error.code == "NOT_FOUND" => {
            // 只打标记，标题/作者/标签等已有数据原样保留
            mark_deleted(conn, target.id)?;
            Ok(SyncOutcome::Deleted)
        }
        Err(error) if error.code == "AUTH" => {
            let _ = record_failure(conn, target.id, &error);
            Ok(SyncOutcome::Auth(error.message.clone()))
        }
        Err(error) => {
            let _ = record_failure(conn, target.id, &error);
            Ok(SyncOutcome::Failed)
        }
    }
}

fn mark_deleted(conn: &Connection, artwork_id: i64) -> AppResult<()> {
    conn.execute(
        "UPDATE artworks SET
            pixiv_status = 'deleted', last_synced = datetime('now'),
            sync_error = NULL, sync_error_kind = NULL, next_retry_at = NULL
         WHERE id = ?1",
        [artwork_id],
    )?;
    Ok(())
}

/// 失败落库：记分类、次数与下次重试时间，坏作品不会每次都挤在最前面。
fn record_failure(conn: &Connection, artwork_id: i64, error: &AppError) -> AppResult<()> {
    let kind = classify_error(error);
    let attempts: i64 = conn
        .query_row(
            "SELECT COALESCE(sync_attempts, 0) FROM artworks WHERE id = ?1",
            [artwork_id],
            |row| row.get(0),
        )
        .unwrap_or(0);
    let minutes = match kind {
        // 已删作品一天试一次；限流等半小时；其它指数退避，封顶 12 小时
        "not_found" => 1440,
        "rate_limited" => 30,
        "auth" => 60,
        _ => (5i64 * (1 << attempts.min(4))).min(720),
    };
    conn.execute(
        "UPDATE artworks SET
            sync_error = ?1, sync_error_kind = ?2,
            sync_attempts = COALESCE(sync_attempts, 0) + 1,
            next_retry_at = datetime('now', ?3)
         WHERE id = ?4",
        params![
            error.message.chars().take(300).collect::<String>(),
            kind,
            format!("+{minutes} minutes"),
            artwork_id
        ],
    )?;
    Ok(())
}

fn classify_error(error: &AppError) -> &'static str {
    match error.code.as_str() {
        "AUTH" => "auth",
        "NOT_FOUND" => "not_found",
        _ => {
            let message = error.message.to_ascii_lowercase();
            if message.contains("429") || message.contains("rate") {
                "rate_limited"
            } else if message.contains("http 5")
                || message.contains("连接")
                || message.contains("超时")
                || message.contains("timed out")
            {
                "network"
            } else {
                "internal"
            }
        }
    }
}

fn emit_done(app: &AppHandle, total: usize, report: &SyncReport) {
    let _ = app.emit(
        SYNC_PROGRESS_EVENT,
        &SyncProgress {
            current: total,
            total,
            synced: report.synced,
            message: "同步完成".to_string(),
        },
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    fn memory_db() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(crate::db::schema::SCHEMA_SQL).unwrap();
        conn
    }

    fn illust() -> IllustData {
        IllustData {
            pixiv_id: 112290978,
            title: "标题".to_string(),
            description: "描述".to_string(),
            author_id: 42,
            author_name: "作者".to_string(),
            author_avatar: Some("https://i.pximg.net/avatar.jpg".to_string()),
            create_date: Some("2024-01-02T03:04:05+09:00".to_string()),
            page_count: 2,
            visible: true,
            width: Some(1200),
            height: Some(800),
            ai_type: 2,
            tags: vec![
                crate::net::pixiv::IllustTag {
                    name: "R-18".to_string(),
                    translated_name: None,
                },
                crate::net::pixiv::IllustTag {
                    name: "オリジナル".to_string(),
                    translated_name: Some("原创".to_string()),
                },
            ],
            image_urls: vec![],
            is_ugoira: false,
        }
    }

    #[test]
    fn applies_metadata_and_tags() {
        let mut conn = memory_db();
        conn.execute(
            "INSERT INTO artworks (pixiv_id, title) VALUES (112290978, '旧标题')",
            [],
        )
        .unwrap();
        let artwork_id = conn.last_insert_rowid();

        apply_illust(&mut conn, artwork_id, &illust()).unwrap();

        let (title, author_id, ai_type): (String, i64, i64) = conn
            .query_row(
                "SELECT title, author_id, ai_type FROM artworks WHERE id = ?1",
                [artwork_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(title, "标题");
        assert!(author_id > 0);
        assert_eq!(ai_type, 2);

        let author_name: String = conn
            .query_row("SELECT name FROM authors WHERE pixiv_user_id = 42", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(author_name, "作者");

        let tag_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM artwork_tags WHERE artwork_id = ?1",
                [artwork_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(tag_count, 2, "R-18 标签要落库，R18 筛选靠它");
    }

    #[test]
    fn resync_replaces_tags_without_duplicates() {
        let mut conn = memory_db();
        conn.execute(
            "INSERT INTO artworks (pixiv_id, title) VALUES (112290978, '旧标题')",
            [],
        )
        .unwrap();
        let artwork_id = conn.last_insert_rowid();

        apply_illust(&mut conn, artwork_id, &illust()).unwrap();
        apply_illust(&mut conn, artwork_id, &illust()).unwrap();

        let tag_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM artwork_tags WHERE artwork_id = ?1",
                [artwork_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(tag_count, 2, "重复同步不能把标签叠加成重复行");

        let author_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM authors", [], |row| row.get(0))
            .unwrap();
        assert_eq!(author_count, 1, "作者复用同一行");
    }

    #[test]
    fn author_name_updates_on_resync() {
        let mut conn = memory_db();
        conn.execute(
            "INSERT INTO artworks (pixiv_id, title) VALUES (112290978, '标题')",
            [],
        )
        .unwrap();
        let artwork_id = conn.last_insert_rowid();

        let mut first = illust();
        first.author_name = "旧名字".to_string();
        apply_illust(&mut conn, artwork_id, &first).unwrap();

        let mut second = illust();
        second.author_name = "新名字".to_string();
        apply_illust(&mut conn, artwork_id, &second).unwrap();

        let name: String = conn
            .query_row(
                "SELECT name FROM authors WHERE pixiv_user_id = 42",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(name, "新名字", "作者改名后同步必须跟着更新");
    }

    #[test]
    fn tag_removal_is_reflected() {
        let mut conn = memory_db();
        conn.execute(
            "INSERT INTO artworks (pixiv_id, title) VALUES (112290978, '标题')",
            [],
        )
        .unwrap();
        let artwork_id = conn.last_insert_rowid();

        apply_illust(&mut conn, artwork_id, &illust()).unwrap();

        let mut fewer = illust();
        fewer.tags.truncate(1);
        apply_illust(&mut conn, artwork_id, &fewer).unwrap();

        let tag_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM artwork_tags WHERE artwork_id = ?1",
                [artwork_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(tag_count, 1, "标签减少后关联行要跟着删掉");
    }

    #[test]
    fn incremental_scan_skips_synced_rows() {
        let conn = memory_db();
        conn.execute(
            "INSERT INTO artworks (pixiv_id, last_synced) VALUES (1, datetime('now'))",
            [],
        )
        .unwrap();
        conn.execute("INSERT INTO artworks (pixiv_id) VALUES (2)", [])
            .unwrap();
        conn.execute(
            "INSERT INTO artworks (pixiv_id, last_synced) VALUES (3, datetime('now'))",
            [],
        )
        .unwrap();
        conn.execute(
            "UPDATE artworks SET sync_error = 'boom' WHERE pixiv_id = 3",
            [],
        )
        .unwrap();

        let incremental = collect_targets(&conn, false).unwrap();
        let ids: Vec<i64> = incremental.iter().map(|t| t.pixiv_id).collect();
        assert_eq!(ids, vec![2, 3], "没同步过的和上次失败的才要补");

        let all = collect_targets(&conn, true).unwrap();
        assert_eq!(all.len(), 3, "全量模式包含已同步的");
    }

    #[test]
    fn full_scan_skips_deleted_artworks() {
        let conn = memory_db();
        conn.execute("INSERT INTO artworks (pixiv_id) VALUES (1)", [])
            .unwrap();
        conn.execute(
            "INSERT INTO artworks (pixiv_id, pixiv_status) VALUES (2, 'deleted')",
            [],
        )
        .unwrap();

        let all = collect_targets(&conn, true).unwrap();
        assert_eq!(all.len(), 1, "全量重同步跳过已确认删除的作品");
    }

    #[test]
    fn failures_are_classified_and_backed_off() {
        let conn = memory_db();
        conn.execute("INSERT INTO artworks (pixiv_id) VALUES (1)", [])
            .unwrap();
        let artwork_id = conn.last_insert_rowid();

        let error = AppError::internal("请求作品详情失败：操作超时");
        record_failure(&conn, artwork_id, &error).unwrap();

        let (kind, attempts, retry): (String, i64, Option<String>) = conn
            .query_row(
                "SELECT COALESCE(sync_error_kind, ''), COALESCE(sync_attempts, 0), next_retry_at
                 FROM artworks WHERE id = ?1",
                [artwork_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(kind, "network");
        assert_eq!(attempts, 1);
        assert!(retry.is_some(), "失败后要写下一次重试时间");

        // 退避时间已设：增量同步不会再马上取到它
        let targets = collect_targets(&conn, false).unwrap();
        assert!(targets.is_empty(), "未到重试时间的失败项应跳过");
    }
}