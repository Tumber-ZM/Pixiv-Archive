use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use rusqlite::Connection;
use tauri::{AppHandle, Emitter, State};

use super::download;
use super::task::TaskCancel;
use crate::db;
use crate::error::{AppError, AppResult};
use crate::models::SubKind;
use crate::net::pixiv::{IllustRef, PixivClient};
use crate::platform::paths;

pub const SUB_PROGRESS_EVENT: &str = "sub-progress";
pub const REPAIR_PROGRESS_EVENT: &str = "repair-progress";
const MAX_PAGES: usize = 100;

#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct RepairReport {
    pub checked: usize,
    pub repaired: usize,
    pub failed: usize,
}

#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct SubResult {
    /// `bookmark` / `author`，前端据此显示订阅类型
    pub kind: String,
    pub pixiv_user_id: i64,
    pub name: String,
    /// 列表里发现、库里还没有的作品数
    pub new_found: usize,
    /// 库里已有、但缺页需要补的作品数
    pub repaired: usize,
    pub downloaded: usize,
    pub failed: usize,
    pub pages_scanned: usize,
    /// 快速模式下「整页都已入库」提前停止；作者订阅追到订阅起点也算
    pub caught_up: bool,
    /// 用户点取消后提前结束（前端据此显示「已取消」而不是「检查完成」）
    pub cancelled: bool,
    pub error: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct SubProgress {
    pub current: usize,
    pub total: usize,
    /// 本次检查已经下载入库的作品数
    pub imported: usize,
    /// 该订阅还没下完的队列总量（跨多次检查累计）
    pub backlog: usize,
    pub message: String,
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

fn known_pixiv_ids(conn: &Connection) -> AppResult<HashSet<i64>> {
    let mut stmt = conn.prepare("SELECT pixiv_id FROM artworks WHERE pixiv_id > 0")?;
    let rows = stmt.query_map([], |row| row.get::<_, i64>(0))?;
    let mut known = HashSet::new();
    for row in rows {
        known.insert(row?);
    }
    Ok(known)
}

/// 每件作品已经入库的图片数：和远端 page_count 对比，判断要不要补页。
fn local_page_counts(conn: &Connection) -> AppResult<HashMap<i64, i64>> {
    let mut stmt = conn.prepare(
        "SELECT a.pixiv_id, COUNT(i.id)
           FROM artworks a
           LEFT JOIN images i ON i.artwork_id = a.id
          WHERE a.pixiv_id > 0
          GROUP BY a.id",
    )?;
    let rows = stmt.query_map([], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?)))?;
    let mut counts = HashMap::new();
    for row in rows {
        let (pixiv_id, pages) = row?;
        counts.insert(pixiv_id, pages);
    }
    Ok(counts)
}

fn kind_label(kind: SubKind) -> &'static str {
    match kind {
        SubKind::Bookmark => "收藏订阅",
        SubKind::Author => "作者订阅",
    }
}

fn emit(
    app: &AppHandle,
    current: usize,
    total: usize,
    imported: usize,
    backlog: usize,
    message: &str,
) {
    let _ = app.emit(
        SUB_PROGRESS_EVENT,
        &SubProgress {
            current,
            total,
            imported,
            backlog,
            message: message.to_string(),
        },
    );
}

fn parse_kind(raw: &str) -> AppResult<SubKind> {
    SubKind::parse(raw).ok_or_else(|| AppError::invalid(format!("未知的订阅类型：{raw}")))
}

/// 检查单个订阅（`full` = 忽略快速停止，翻遍列表）。
#[tauri::command]
pub async fn check_sub(
    app: AppHandle,
    state: State<'_, TaskCancel>,
    kind: String,
    target_pixiv_id: i64,
    full: Option<bool>,
) -> AppResult<SubResult> {
    let kind = parse_kind(&kind)?;
    let Some(cancel) = state.begin("download") else {
        return Err(AppError::invalid("已有任务在运行"));
    };
    let outcome = run_checks(
        &app,
        vec![(kind, target_pixiv_id)],
        full.unwrap_or(false),
        &cancel,
    )
    .await;
    state.end("download");
    let mut results = outcome?;
    results
        .pop()
        .ok_or_else(|| AppError::internal("检查没有返回结果"))
}

/// 「订阅更新」：检查所有 `auto_download = 1` 的订阅（收藏与作者一起跑）。
#[tauri::command]
pub async fn check_all_subs(
    app: AppHandle,
    state: State<'_, TaskCancel>,
    full: Option<bool>,
) -> AppResult<Vec<SubResult>> {
    let targets: Vec<(SubKind, i64)> = {
        let conn = open_library()?;
        let mut stmt = conn.prepare(
            "SELECT kind, target_pixiv_id FROM subs WHERE auto_download = 1 \
             ORDER BY COALESCE(created_at, ''), id",
        )?;
        let rows =
            stmt.query_map([], |row| Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?)))?;
        rows.filter_map(Result::ok)
            .filter_map(|(kind, target_pixiv_id)| {
                SubKind::parse(&kind).map(|kind| (kind, target_pixiv_id))
            })
            .collect()
    };

    if targets.is_empty() {
        return Err(AppError::invalid(
            "还没有启用中的订阅，先在「订阅列表」里添加并开启自动下载",
        ));
    }

    let Some(cancel) = state.begin("download") else {
        return Err(AppError::invalid("已有任务在运行"));
    };
    let outcome = run_checks(&app, targets, full.unwrap_or(false), &cancel).await;
    state.end("download");
    outcome
}

async fn run_checks(
    app: &AppHandle,
    targets: Vec<(SubKind, i64)>,
    full: bool,
    cancel: &Arc<AtomicBool>,
) -> AppResult<Vec<SubResult>> {
    let client = PixivClient::from_settings()?;
    let source_root = crate::commands::settings::image_source_dir()?
        .map(std::path::PathBuf::from)
        .ok_or_else(|| AppError::invalid("还没有设置图片目录（设置 → 基础）"))?;
    let data_dir = paths::data_dir()?;

    let mut conn = open_library()?;
    let mut known = known_pixiv_ids(&conn)?;
    let local_pages = local_page_counts(&conn)?;
    let total = targets.len();
    let mut results = Vec::with_capacity(total);
    let mut cancelled = false;

    for (index, (kind, user_id)) in targets.iter().enumerate() {
        if cancel.load(Ordering::Relaxed) {
            cancelled = true;
            break;
        }
        let mut result = SubResult {
            kind: kind.as_str().to_string(),
            pixiv_user_id: *user_id,
            ..Default::default()
        };

        // 订阅名字优先用本地 authors（重新同步后会自动跟着改名），
        // 其次才是订阅时记在 subs.name 里的快照。
        let (mut name, baseline): (String, Option<i64>) = conn
            .query_row(
                "SELECT COALESCE(NULLIF(TRIM(au.name), ''), NULLIF(TRIM(s.name), ''), ''),
                        s.baseline_pid
                   FROM subs s
                   LEFT JOIN authors au
                          ON s.kind = 'author' AND au.pixiv_user_id = s.target_pixiv_id
                  WHERE s.kind = ?1 AND s.target_pixiv_id = ?2",
                rusqlite::params![kind.as_str(), user_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap_or_default();

        // 订阅名缺失（老数据 / 订阅时离线）时顺手补一次，检查结果里也能显示真名
        if name.trim().is_empty() {
            if let Ok(Some(fetched)) = client.user_name(*user_id).await {
                let _ = conn.execute(
                    "UPDATE subs SET name = ?1 WHERE kind = ?2 AND target_pixiv_id = ?3",
                    rusqlite::params![fetched, kind.as_str(), user_id],
                );
                name = fetched;
            }
        }
        result.name = name;

        // 作者订阅第一次检查如果还没有起点，会在 check_one 里补建一遍，结果文案要说清楚
        let baseline_missing = *kind == SubKind::Author && baseline.is_none();

        emit(
            app,
            index,
            total,
            0,
            0,
            &format!("{}「{}」：读取列表…", kind_label(*kind), result.name),
        );

        let outcome = check_one(
            &client,
            &mut conn,
            &mut known,
            &local_pages,
            &data_dir,
            &source_root,
            *kind,
            user_id,
            baseline,
            full,
            app,
            cancel,
            &mut result,
        )
        .await;

        let auth_failed = matches!(&outcome, Err(error) if error.code == "AUTH");
        if let Err(error) = outcome {
            result.error = Some(error.message);
        }

        let summary = if baseline_missing && result.error.is_none() {
            "已建立订阅起点".to_string()
        } else {
            format!(
                "新增 {} · 补缺 {} · 下载 {} · 失败 {}",
                result.new_found, result.repaired, result.downloaded, result.failed
            )
        };
        let _ = conn.execute(
            "UPDATE subs SET last_checked = datetime('now'), last_result = ?1 \
             WHERE kind = ?2 AND target_pixiv_id = ?3",
            rusqlite::params![summary, kind.as_str(), user_id],
        );

        results.push(result);
        if auth_failed {
            break;
        }
    }

    // 取消发生在「上一个订阅检查完、下一个还没开始」时，最后一个结果也要标出来
    if cancelled {
        if let Some(last) = results.last_mut() {
            last.cancelled = true;
        }
    }
    // check_one 内部发现取消时也会标 result；最终提示统一看这两个来源
    let cancelled = cancelled || results.last().map(|row| row.cancelled).unwrap_or(false);

    let downloaded: usize = results.iter().map(|row| row.downloaded).sum();
    emit(
        app,
        total,
        total,
        downloaded,
        0,
        if cancelled {
            "订阅检查已取消"
        } else {
            "订阅检查完成"
        },
    );
    Ok(results)
}

#[allow(clippy::too_many_arguments)]
async fn check_one(
    client: &PixivClient,
    conn: &mut Connection,
    known: &mut HashSet<i64>,
    local_pages: &HashMap<i64, i64>,
    data_dir: &Path,
    source_root: &Path,
    kind: SubKind,
    user_id: &i64,
    baseline: Option<i64>,
    full: bool,
    app: &AppHandle,
    cancel: &Arc<AtomicBool>,
    result: &mut SubResult,
) -> AppResult<()> {
    // 作者订阅默认只追新，但订阅时没联网（拿不到最新作品）：
    // 以「现在的最新作品」为起点，这次不下载历史，避免误拉几千张。
    if kind == SubKind::Author && baseline.is_none() {
        let (items, _) = client.user_illusts(*user_id, None).await?;
        let newest = items.iter().map(|item| item.pixiv_id).max().unwrap_or(0);
        conn.execute(
            "UPDATE subs SET baseline_pid = ?1 WHERE kind = 'author' AND target_pixiv_id = ?2",
            rusqlite::params![newest, user_id],
        )?;
        result.pages_scanned = 1;
        result.caught_up = true;
        return Ok(());
    }

    // 补全历史：起点是 0，必须一直翻到底，不能因为「整页都已入库」提前停。
    let backfill = kind == SubKind::Author && baseline == Some(0);
    // 作者订阅下载的作品单独标来源，供作者列表的「仅看收藏」筛掉
    let origin = if kind == SubKind::Author {
        download::ORIGIN_AUTHOR_SUB
    } else {
        download::ORIGIN_LOCAL
    };

    // 1. 按类型翻列表，收集库里没有的
    let mut next_url: Option<String> = None;
    let mut collected: Vec<IllustRef> = Vec::new();
    let mut pages = 0usize;
    let mut newest_seen = 0i64;
    let mut reached_end = false;

    while pages < MAX_PAGES {
        if cancel.load(Ordering::Relaxed) {
            result.cancelled = true;
            break;
        }
        let (illusts, next) = match kind {
            SubKind::Bookmark => {
                client
                    .user_bookmarks_public(*user_id, next_url.as_deref())
                    .await?
            }
            SubKind::Author => client.user_illusts(*user_id, next_url.as_deref()).await?,
        };
        pages += 1;
        if illusts.is_empty() {
            reached_end = next.is_none();
            break;
        }

        let mut page_all_complete = true;
        let mut hit_baseline = false;
        for item in &illusts {
            newest_seen = newest_seen.max(item.pixiv_id);
            // 订阅起点之前的作品不属于这次订阅（补全模式起点是 0，不会命中）
            if let Some(base) = baseline {
                if base > 0 && item.pixiv_id <= base {
                    hit_baseline = true;
                    continue;
                }
            }
            // 本地页数和远端 page_count 对得上才算齐全；缺页的已知作品也重新入队
            let complete = local_pages
                .get(&item.pixiv_id)
                .map(|pages| *pages >= item.page_count)
                .unwrap_or(false);
            if complete {
                continue;
            }
            page_all_complete = false;
            if !known.contains(&item.pixiv_id) {
                known.insert(item.pixiv_id);
            }
            collected.push(item.clone());
        }

        emit(
            app,
            pages,
            0,
            result.downloaded,
            0,
            &format!(
                "{}「{}」：第 {} 页，已发现 {} 件待处理",
                kind_label(kind),
                result.name,
                pages,
                collected.len()
            ),
        );

        // 追到订阅起点：再往后都是订阅之前的作品，停止翻页
        if hit_baseline {
            result.caught_up = true;
            break;
        }        let Some(next) = next else {
            reached_end = true;
            break;
        };
        // 快速模式：整页都已齐全 → 已追平（作品按发布时间倒序，新作品在最前）。
        // 补全模式例外：还要继续往后翻，否则会漏掉更早的作品。
        if !full && !backfill && page_all_complete {
            result.caught_up = true;
            break;
        }
        next_url = Some(next);
    }

    // 补全翻到底了：把起点推进到本次看到的最新作品，下次检查只追新。
    // 中途取消或失败则保持 0，下次接着补。
    if backfill && reached_end && !result.cancelled && newest_seen > 0 {
        conn.execute(
            "UPDATE subs SET baseline_pid = ?1 WHERE kind = 'author' AND target_pixiv_id = ?2",
            rusqlite::params![newest_seen, user_id],
        )?;
    }

    result.pages_scanned = pages;
    // 已知但缺页的作品算「补缺」，不算新增
    result.repaired = collected
        .iter()
        .filter(|item| local_pages.contains_key(&item.pixiv_id))
        .count();
    result.new_found = collected.len() - result.repaired;
    collected.sort_by_key(|item| item.pixiv_id); // 旧 → 新

    // 2. 入队：本次发现的与上次失败的都在队列里，一起消费
    for item in &collected {
        download::enqueue(conn, item.pixiv_id, origin)?;
    }

    let queued = download::pending_queue(conn, download::QUEUE_BATCH, origin)?;
    let queued_total = queued.len();
    let backlog = download::queue_len(conn, origin)?;
    if queued_total > 0 {
        emit(
            app,
            0,
            queued_total,
            result.downloaded,
            backlog,
            &format!(
                "{}「{}」：本地待下载 {} 件，本次处理 {} 件",
                kind_label(kind),
                result.name,
                backlog,
                queued_total
            ),
        );
    }
    for (idx, pixiv_id) in queued.iter().enumerate() {
        if cancel.load(Ordering::Relaxed) {
            result.cancelled = true;
            break;
        }
        emit(
            app,
            idx + 1,
            queued_total,
            result.downloaded,
            backlog,
            &format!(
                "{}「{}」：下载 {}/{}（PID {}）",
                kind_label(kind),
                result.name,
                idx + 1,
                queued_total,
                pixiv_id
            ),
        );

        match client.illust_detail(*pixiv_id).await {
            Ok(data) => {
                if cancel.load(Ordering::Relaxed) {
                    result.cancelled = true;
                    break;
                }
                // 头像要在入库前判断：apply_illust 会把 profile_image 更新成新地址
                let stored_avatar: Option<String> = conn
                    .query_row(
                        "SELECT profile_image FROM authors WHERE pixiv_user_id = ?1",
                        [data.author_id],
                        |row| row.get(0),
                    )
                    .ok()
                    .flatten();
                let _ = download::ensure_avatar(
                    client,
                    data_dir,
                    data.author_id,
                    data.author_avatar.as_deref(),
                    stored_avatar.as_deref(),
                )
                .await;
                match download::fetch_and_import(
                    client,
                    conn,
                    data_dir,
                    source_root,
                    &data,
                    origin,
                    cancel,
                )
                .await
                {
                    Ok(_) => {
                        download::complete_queue_item(conn, *pixiv_id)?;
                        result.downloaded += 1;
                    }
                    Err(error) if error.code == "CANCELLED" => {
                        result.cancelled = true;
                        break;
                    }
                    Err(error) => {
                        download::fail_queue_item(conn, *pixiv_id, &error.message)?;
                        result.failed += 1;
                        eprintln!("下载作品 {pixiv_id} 失败：{}", error.message);
                    }
                }
            }
            Err(error) if error.code == "AUTH" => return Err(error),
            Err(error) if error.code == "NOT_FOUND" => {
                // Pixiv 上已删除：移出队列，不再重试
                download::complete_queue_item(conn, *pixiv_id)?;
                result.failed += 1;
            }
            Err(error) => {
                download::fail_queue_item(conn, *pixiv_id, &error.message)?;
                result.failed += 1;
            }
        }
    }

    Ok(())
}

/// 校验本地作品：找出「图片数 < page_count」的作品，和远端详情核对后补齐缺页。
///
/// 已经齐全的作品不会下载；缺页但文件已在磁盘上的，只补图片记录、不重新下图。
#[tauri::command]
pub async fn repair_incomplete_images(
    app: AppHandle,
    state: State<'_, TaskCancel>,
) -> AppResult<RepairReport> {
    let Some(cancel) = state.begin("download") else {
        return Err(AppError::invalid("已有任务在运行"));
    };
    let outcome = repair_inner(&app, &cancel).await;
    state.end("download");
    outcome
}

async fn repair_inner(app: &AppHandle, cancel: &Arc<AtomicBool>) -> AppResult<RepairReport> {
    let client = PixivClient::from_settings()?;
    let source_root = crate::commands::settings::image_source_dir()?
        .map(std::path::PathBuf::from)
        .ok_or_else(|| AppError::invalid("还没有设置图片目录（设置 → 基础）"))?;
    let data_dir = paths::data_dir()?;
    let mut conn = open_library()?;

    let targets: Vec<(i64, String)> = {
        let mut stmt = conn.prepare(
            "SELECT a.pixiv_id, COALESCE(a.origin, 'local')
               FROM artworks a
              WHERE (SELECT COUNT(*) FROM images i WHERE i.artwork_id = a.id) < a.page_count
                 OR NOT EXISTS (SELECT 1 FROM images i WHERE i.artwork_id = a.id)
              ORDER BY a.id",
        )?;
        let rows = stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?;
        rows.filter_map(Result::ok).collect()
    };

    let total = targets.len();
    let mut report = RepairReport {
        checked: total,
        ..Default::default()
    };

    for (index, (pixiv_id, origin)) in targets.iter().enumerate() {
        if cancel.load(Ordering::Relaxed) {
            break;
        }
        let _ = app.emit(
            REPAIR_PROGRESS_EVENT,
            serde_json::json!({
                "current": index + 1,
                "total": total,
                "imported": report.repaired,
                "message": format!("校验作品 {}/{}（PID {}）", index + 1, total, pixiv_id),
            }),
        );

        let data = match client.illust_detail(*pixiv_id).await {
            Ok(data) => data,
            Err(error) => {
                report.failed += 1;
                eprintln!("校验作品 {pixiv_id} 失败：{}", error.message);
                continue;
            }
        };

        match download::fetch_and_import(
            &client,
            &mut conn,
            &data_dir,
            &source_root,
            &data,
            origin,
            cancel,
        )
        .await
        {
            Ok(_) => report.repaired += 1,
            Err(error) if error.code == "CANCELLED" => break,
            Err(error) => {
                report.failed += 1;
                eprintln!("补页 {pixiv_id} 失败：{}", error.message);
            }
        }
    }

    let _ = app.emit(
        REPAIR_PROGRESS_EVENT,
        serde_json::json!({
            "current": total,
            "total": total,
            "imported": report.repaired,
            "message": "校验完成",
        }),
    );
    Ok(report)
}
