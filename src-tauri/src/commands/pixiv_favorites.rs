use std::collections::HashSet;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use rusqlite::Connection;
use tauri::{AppHandle, Emitter, State};

use crate::commands::task::TaskCancel;
use crate::db;
use crate::error::{AppError, AppResult};
use crate::models::{FavoriteSyncReport, FavoriteSyncStatus};
use crate::net::pixiv::PixivClient;
use crate::platform::paths;

pub const FAVORITES_PROGRESS_EVENT: &str = "favorites-progress";
/// 最多翻 200 页（约 6000 件收藏），防止异常情况下无限翻页
const MAX_PAGES: usize = 200;
const SYNCED_AT_KEY: &str = "pixiv_favorites_synced_at";

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

/// 本地缓存的 Pixiv 收藏概况。
#[tauri::command]
pub fn pixiv_favorites_status() -> AppResult<FavoriteSyncStatus> {
    let conn = open_library()?;
    let count: i64 = conn.query_row("SELECT COUNT(*) FROM pixiv_favorites", [], |row| row.get(0))?;
    let last_synced: Option<String> = conn
        .query_row(
            "SELECT value FROM app_meta WHERE key = ?1",
            [SYNCED_AT_KEY],
            |row| row.get(0),
        )
        .ok();
    Ok(FavoriteSyncStatus { count, last_synced })
}

/// 同步「我的 Pixiv 收藏」：读自己账号的公开 + 私密收藏，只把作品 ID 写进本地库。
///
/// 只是给作者列表的「仅看收藏」用；不改作品、不下载图片、不上传任何东西。
#[tauri::command]
pub async fn sync_pixiv_favorites(
    app: AppHandle,
    state: State<'_, TaskCancel>,
) -> AppResult<FavoriteSyncReport> {
    let Some(cancel) = state.begin("favorites") else {
        return Err(AppError::invalid("已有任务在运行"));
    };
    let outcome = sync_inner(&app, &cancel).await;
    state.end("favorites");
    outcome
}

async fn sync_inner(app: &AppHandle, cancel: &Arc<AtomicBool>) -> AppResult<FavoriteSyncReport> {
    let client = PixivClient::from_settings()?;
    let user_id = client.current_user_id().await?;

    let mut ids: HashSet<i64> = HashSet::new();
    let mut report = FavoriteSyncReport::default();

    for (restrict, label) in [("public", "公开"), ("private", "私密")] {
        let mut next_url: Option<String> = None;
        let mut fetched = 0usize;

        loop {
            if cancel.load(Ordering::Relaxed) {
                report.cancelled = true;
                break;
            }
            if report.pages >= MAX_PAGES {
                break;
            }

            let result = client
                .user_bookmarks(user_id, restrict, next_url.as_deref())
                .await;
            let (items, next) = match result {
                Ok(value) => value,
                // 私密收藏接口个别账号会 404/403：不让整次同步失败，公开收藏仍然可用
                Err(error) if restrict == "private" && error.code != "AUTH" => break,
                Err(error) => return Err(error),
            };
            report.pages += 1;
            if items.is_empty() {
                break;
            }
            for item in &items {
                ids.insert(item.pixiv_id);
                fetched += 1;
            }

            let _ = app.emit(
                FAVORITES_PROGRESS_EVENT,
                serde_json::json!({
                    "current": fetched,
                    "total": 0,
                    "message": format!("读取{label}收藏：{fetched} 件（第 {} 页）", report.pages),
                }),
            );

            match next {
                Some(url) => next_url = Some(url),
                None => break,
            }
        }

        if restrict == "public" {
            report.public = fetched;
        } else {
            report.private = fetched;
        }
        if report.cancelled {
            break;
        }
    }

    // 取消时保留上一次同步结果，不写半份数据，避免「收藏」视图忽然缺一块
    if report.cancelled {
        return Ok(report);
    }

    let mut conn = open_library()?;
    let tx = conn.transaction()?;
    tx.execute("DELETE FROM pixiv_favorites", [])?;
    {
        let mut stmt = tx.prepare("INSERT OR IGNORE INTO pixiv_favorites (pixiv_id) VALUES (?1)")?;
        for pixiv_id in &ids {
            stmt.execute([pixiv_id])?;
        }
    }
    tx.execute(
        "INSERT INTO app_meta (key, value) VALUES (?1, datetime('now'))
         ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [SYNCED_AT_KEY],
    )?;
    tx.commit()?;

    report.total = ids.len();
    Ok(report)
}
