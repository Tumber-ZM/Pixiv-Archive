use std::io::{BufWriter, Read, Write};
use std::path::{Path, PathBuf};

use futures_util::future::join_all;
use rusqlite::{params, Connection};
use sha2::{Digest, Sha256};

use crate::commands::thumbnails;
use crate::db;
use crate::error::{AppError, AppResult};
use crate::net::pixiv::{IllustData, PixivClient};
use crate::platform::paths;

const IMAGE_EXTENSIONS: &[&str] = &["jpg", "jpeg", "png", "gif", "webp"];

/// 旧项目 `_safe_name`：非法字符换 `_`，去掉尾部 `.`/空格，限长 150。
pub fn safe_name(name: &str) -> String {
    let cleaned: String = name
        .chars()
        .map(|ch| match ch {
            '\\' | '/' | ':' | '*' | '?' | '"' | '<' | '>' | '|' => '_',
            '\u{0}'..='\u{1f}' => '_',
            other => other,
        })
        .collect();
    let trimmed = cleaned.trim().trim_end_matches(['.', ' ']).to_string();
    if trimmed.is_empty() {
        "untitled".to_string()
    } else {
        trimmed.chars().take(150).collect()
    }
}

/// URL 的图片扩展名（只认扫描器支持的格式，其它一律按 jpg 存）。
fn url_extension(url: &str) -> String {
    let path = url.split('?').next().unwrap_or(url);
    let ext = path.rsplit('.').next().unwrap_or("jpg").to_ascii_lowercase();
    match ext.as_str() {
        "jpeg" => ".jpg".to_string(),
        "jpg" | "png" | "gif" | "webp" => format!(".{ext}"),
        _ => ".jpg".to_string(),
    }
}

/// 目标目录与文件名前缀，命名规则与扫描器兼容（见旧项目 `_resolve_dest`）。
fn resolve_dest(author_root: &Path, author_name: &str, data: &IllustData) -> (PathBuf, String) {
    let author = safe_name(author_name);
    if data.page_count > 1 {
        let title = safe_name(&data.title);
        (
            author_root.join(format!("[{author}] {title}")),
            // 多页：目录用标题，文件名前缀用作者名（与旧项目下载路径宏一致）
            format!("[{author}] {}_p", data.pixiv_id),
        )
    } else {
        (author_root.to_path_buf(), format!("[{author}] {}", data.pixiv_id))
    }
}

fn work_exists(dir: &Path, prefix: &str, page_count: i64) -> bool {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return false;
    };
    let names: Vec<String> = entries
        .filter_map(|entry| entry.ok())
        .filter_map(|entry| entry.file_name().into_string().ok())
        .collect();

    let has = |stem: &str| {
        IMAGE_EXTENSIONS
            .iter()
            .any(|ext| names.iter().any(|name| name == &format!("{stem}.{ext}")))
    };

    if page_count <= 1 {
        return has(prefix);
    }
    (0..page_count).all(|page| has(&format!("{prefix}{page}")))
}

#[derive(Debug, Clone)]
pub struct DownloadedFile {
    pub path: PathBuf,
    pub page: i64,
    /// 下载时顺带算好的 sha256；已存在的文件为 None，入库时再算
    pub hash: Option<String>,
}

/// 下载一个作品的全部页。已完整下载过的直接返回空列表（幂等）。
///
/// 多页之间并发执行，并发上限由 `PixivClient` 的信号量统一控制；
/// 单页失败不影响其它页：已下好的部分照常入库，缺失页下次同步时补齐。
pub async fn download_illust(
    client: &PixivClient,
    data: &IllustData,
    source_root: &Path,
) -> AppResult<Vec<DownloadedFile>> {
    if data.is_ugoira {
        return download_ugoira(client, data, source_root).await;
    }

    if data.image_urls.is_empty() {
        return Err(AppError::internal("作品没有可下载的原图地址"));
    }

    let author_root = source_root.join(safe_name(&data.author_name));
    let (dest_dir, prefix) = resolve_dest(&author_root, &data.author_name, data);
    let page_count = data.page_count.max(1);

    if work_exists(&dest_dir, &prefix, page_count) {
        // 文件已经在磁盘上（旧工具下载的 / 上次中断前下好的）：把已有文件返回给
        // 调用方入库。否则会出现「作品行建了、却没有图片记录、缩略图也生成不了」
        // 的空壳作品。
        return Ok(collect_existing(&dest_dir, &prefix));
    }

    std::fs::create_dir_all(&dest_dir)?;

    let targets: Vec<(usize, PathBuf)> = data
        .image_urls
        .iter()
        .enumerate()
        .map(|(page, url)| {
            let ext = url_extension(url);
            let name = if page_count > 1 {
                format!("{prefix}{page}{ext}")
            } else {
                format!("{prefix}{ext}")
            };
            (page, dest_dir.join(name))
        })
        .collect();

    let tasks = targets.iter().map(|(page, dest)| async move {
        let file: AppResult<DownloadedFile> = if dest.is_file() {
            // 上次中断前已经下好的页：跳过下载，哈希留给入库时算
            Ok(DownloadedFile {
                path: dest.clone(),
                page: *page as i64,
                hash: None,
            })
        } else {
            client
                .fetch_image_to(&data.image_urls[*page], dest)
                .await
                .map(|hash| DownloadedFile {
                    path: dest.clone(),
                    page: *page as i64,
                    hash: Some(hash),
                })
        };
        file
    });

    let mut files = Vec::with_capacity(targets.len());
    let mut failures = 0usize;
    let mut last_error: Option<AppError> = None;
    for outcome in join_all(tasks).await {
        match outcome {
            Ok(file) => files.push(file),
            Err(error) => {
                failures += 1;
                eprintln!("页面下载失败：{}", error.message);
                last_error = Some(error);
            }
        }
    }

    // 全部失败才算失败；部分成功时先入库，下次补缺页
    if files.is_empty() {
        return Err(last_error.unwrap_or_else(|| AppError::internal("没有下载到任何页面")));
    }
    if failures > 0 {
        eprintln!(
            "作品 {} 有 {failures} 页下载失败，将在下次同步时补齐",
            data.pixiv_id
        );
    }
    Ok(files)
}

/// 动图帧数上限：防止异常文件把内存和 CPU 拖死。
const MAX_UGOIRA_FRAMES: usize = 500;

/// 动图：下载 zip → 逐帧合成无限循环 GIF。
///
/// zip 先落到作品目录下的隐藏文件，合成时逐帧解码、逐帧写入，
/// 内存里只保留一帧；完成后删掉 zip，只留 GIF。
async fn download_ugoira(
    client: &PixivClient,
    data: &IllustData,
    source_root: &Path,
) -> AppResult<Vec<DownloadedFile>> {
    let author_root = source_root.join(safe_name(&data.author_name));
    let dest = author_root.join(format!(
        "[{}] {}.gif",
        safe_name(&data.author_name),
        data.pixiv_id
    ));
    if dest.is_file() {
        return Ok(vec![DownloadedFile {
            path: dest,
            page: 0,
            hash: None,
        }]);
    }

    let meta = client.ugoira_metadata(data.pixiv_id).await?;
    if meta.frames.is_empty() {
        return Err(AppError::internal("动图没有帧信息"));
    }
    if meta.frames.len() > MAX_UGOIRA_FRAMES {
        return Err(AppError::internal(format!(
            "动图帧数 {} 超过上限 {MAX_UGOIRA_FRAMES}",
            meta.frames.len()
        )));
    }

    std::fs::create_dir_all(&author_root)?;
    let zip_path = author_root.join(format!(".ugoira-{}.zip", data.pixiv_id));
    client.fetch_image_to(&meta.zip_url, &zip_path).await?;

    let frames = meta.frames.clone();
    let gif_path = dest.clone();
    let zip_for_task = zip_path.clone();
    let outcome = tokio::task::spawn_blocking(move || {
        build_gif_from_zip(&zip_for_task, &frames, &gif_path)
    })
    .await
    .map_err(|error| AppError::internal(format!("动图合成任务失败：{error}")))?;

    let _ = std::fs::remove_file(&zip_path);
    outcome?;

    Ok(vec![DownloadedFile {
        path: dest,
        page: 0,
        hash: None,
    }])
}

/// 从 zip 里解一帧，返回 (宽, 高, RGBA 像素, 延时)。
fn decode_ugoira_frame(
    archive: &mut zip::ZipArchive<std::fs::File>,
    frame: &crate::net::pixiv::UgoiraFrame,
) -> AppResult<(u16, u16, Vec<u8>, u16)> {
    let mut entry = archive
        .by_name(&frame.file)
        .map_err(|error| AppError::internal(format!("动图缺少帧 {}：{error}", frame.file)))?;
    let mut buffer = Vec::new();
    entry.read_to_end(&mut buffer)?;
    drop(entry);

    let decoded = image::load_from_memory(&buffer)
        .map_err(|error| AppError::internal(format!("动图帧解码失败：{error}")))?;
    let rgba = decoded.to_rgba8();
    let (width, height) = rgba.dimensions();
    // GIF 延时以 1/100 秒为单位，低于 20ms 在一些浏览器里会被当成 100ms
    let delay = (frame.delay.max(20) / 10).min(u16::MAX as u64) as u16;
    Ok((width as u16, height as u16, rgba.into_raw(), delay))
}

/// 把 ugoira zip 逐帧写成 GIF（尺寸取第一帧，延时取 frames 元数据）。
///
/// 每帧处理完立刻释放，峰值只有一帧；旧实现会把所有帧 RGBA 都留在内存里。
fn build_gif_from_zip(
    zip_path: &Path,
    frames: &[crate::net::pixiv::UgoiraFrame],
    dest: &Path,
) -> AppResult<()> {
    let file = std::fs::File::open(zip_path)?;
    let mut archive = zip::ZipArchive::new(file)
        .map_err(|error| AppError::internal(format!("动图压缩包无法读取：{error}")))?;

    let Some(first) = frames.first() else {
        return Err(AppError::internal("动图没有可用的帧"));
    };
    let (width, height, mut pixels, delay) = decode_ugoira_frame(&mut archive, first)?;

    let temp = PathBuf::from(format!("{}.part", dest.display()));
    let mut writer = BufWriter::new(std::fs::File::create(&temp)?);
    let mut encoder = gif::Encoder::new(&mut writer, width, height, &[])
        .map_err(|error| AppError::internal(format!("GIF 编码初始化失败：{error}")))?;
    encoder
        .set_repeat(gif::Repeat::Infinite)
        .map_err(|error| AppError::internal(format!("GIF 循环设置失败：{error}")))?;

    let mut frame = gif::Frame::from_rgba_speed(width, height, &mut pixels, 10);
    frame.delay = delay;
    encoder
        .write_frame(&frame)
        .map_err(|error| AppError::internal(format!("GIF 帧写入失败：{error}")))?;

    for item in &frames[1..] {
        let (frame_width, frame_height, mut pixels, delay) =
            decode_ugoira_frame(&mut archive, item)?;
        if frame_width != width || frame_height != height {
            eprintln!(
                "动图帧 {} 尺寸 {frame_width}x{frame_height} 与首帧 {width}x{height} 不一致，跳过",
                item.file
            );
            continue;
        }
        let mut frame = gif::Frame::from_rgba_speed(width, height, &mut pixels, 10);
        frame.delay = delay;
        encoder
            .write_frame(&frame)
            .map_err(|error| AppError::internal(format!("GIF 帧写入失败：{error}")))?;
    }

    drop(encoder);
    writer.flush()?;

    if dest.exists() {
        let _ = std::fs::remove_file(dest);
    }
    std::fs::rename(&temp, dest).map_err(AppError::io)?;
    Ok(())
}

/// 收集目录里已存在的、属于该作品的文件（按文件名前缀 + `_pN` 页码解析）。
fn collect_existing(dir: &Path, prefix: &str) -> Vec<DownloadedFile> {
    let mut out = Vec::new();
    let Ok(entries) = std::fs::read_dir(dir) else {
        return out;
    };

    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        let Some(name) = path.file_name().and_then(|value| value.to_str()) else {
            continue;
        };
        if !name.starts_with(prefix) {
            continue;
        }
        let Some((stem, _ext)) = name.rsplit_once('.') else {
            continue;
        };
        let page = stem
            .rsplit_once("_p")
            .and_then(|(_, rest)| rest.parse::<i64>().ok())
            .unwrap_or(0);
        out.push(DownloadedFile {
            path,
            page,
            hash: None,
        });
    }

    out
}

/// 作者头像落到 `data_dir/avatars/<pixiv_user_id>.png`（旧项目同路径）。
///
/// 只有「文件不存在」或「库里的头像地址变了」才重下：作者换头像后要能跟着更新。
/// `stored_url` 由调用方先查 `authors.profile_image` 传入——不能在 async 里直接
/// 传 `&Connection`，那会让整个 command 的 future 失去 `Send`。
pub async fn ensure_avatar(
    client: &PixivClient,
    data_dir: &Path,
    author_id: i64,
    url: Option<&str>,
    stored_url: Option<&str>,
) -> AppResult<bool> {
    let Some(url) = url.filter(|value| !value.is_empty()) else {
        return Ok(false);
    };
    if author_id <= 0 {
        return Ok(false);
    }

    let dir = data_dir.join("avatars");
    let target = dir.join(format!("{author_id}.png"));
    if target.is_file() && stored_url == Some(url) {
        return Ok(false);
    }

    let bytes = client.fetch_image(url).await?;
    std::fs::create_dir_all(&dir)?;
    let temp = dir.join(format!("{author_id}.png.part"));
    std::fs::write(&temp, &bytes)?;
    if target.exists() {
        let _ = std::fs::remove_file(&target);
    }
    std::fs::rename(&temp, &target)?;
    Ok(true)
}

/// 把原始元数据留一份到 `data_dir/raw/<pixiv_id>.json`。
///
/// 以后解析逻辑变了（新字段、新口径）可以离线重放，不必再请求一次 Pixiv。
pub fn save_raw_metadata(data_dir: &Path, data: &IllustData) -> AppResult<()> {
    let dir = data_dir.join("raw");
    std::fs::create_dir_all(&dir)?;
    let path = dir.join(format!("{}.json", data.pixiv_id));
    let raw = serde_json::to_string_pretty(data)
        .map_err(|error| AppError::internal(format!("元数据序列化失败：{error}")))?;
    let temp = path.with_extension("json.tmp");
    std::fs::write(&temp, raw)?;
    std::fs::rename(&temp, &path)?;
    Ok(())
}

fn file_sha256(path: &Path) -> std::io::Result<String> {
    let mut file = std::fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

/// 作品来源：扫描/收藏订阅等既有收藏，与作者订阅下载分开。
///
/// 只用于「仅看收藏」筛选，不影响展示；作者订阅下载的作品记 `author_sub`。
pub const ORIGIN_LOCAL: &str = "local";
pub const ORIGIN_AUTHOR_SUB: &str = "author_sub";

/// 把刚下载的文件写进库：作品行（缺则建）+ 图片行（按作品/页去重）。
///
/// 不再按 sha256 全局去重：同一张图可能被不同作品引用，按内容跳过会让
/// 后一个作品变成「有作品行、没有图片」的空壳。
pub fn import_downloaded(
    conn: &mut Connection,
    data_dir: &Path,
    data: &IllustData,
    files: &[DownloadedFile],
    origin: &str,
) -> AppResult<i64> {
    let thumbnails_dir = data_dir.join("thumbnails");
    std::fs::create_dir_all(&thumbnails_dir)?;

    // 哈希在开事务前算好：避免持写锁做整文件读取
    let mut hashes: Vec<String> = Vec::with_capacity(files.len());
    for file in files {
        hashes.push(match &file.hash {
            Some(hash) => hash.clone(),
            None => file_sha256(&file.path)?,
        });
    }

    let tx = conn.transaction()?;

    tx.execute(
        "INSERT OR IGNORE INTO artworks (pixiv_id, title, page_count, local_path, origin)
         VALUES (?1, ?2, ?3, ?4, ?5)",
        params![
            data.pixiv_id,
            data.title,
            data.page_count.max(1),
            files.first().map(|file| file.path.to_string_lossy().to_string()),
            origin
        ],
    )?;

    let artwork_id: i64 = tx.query_row(
        "SELECT id FROM artworks WHERE pixiv_id = ?1",
        [data.pixiv_id],
        |row| row.get(0),
    )?;

    // 之前建行时可能是空壳（没有图片），这里补上 local_path
    if let Some(first) = files.first() {
        let _ = tx.execute(
            "UPDATE artworks SET local_path = COALESCE(local_path, ?1) WHERE id = ?2",
            params![first.path.to_string_lossy().to_string(), artwork_id],
        );
    }

    let mut queued: Vec<(PathBuf, Option<PathBuf>, i64)> = Vec::new();

    for (file, hash) in files.iter().zip(hashes.iter()) {
        let size = thumbnails::probe_size(&file.path);
        let inserted = tx.execute(
            "INSERT OR IGNORE INTO images (artwork_id, page, path, width, height, sha256)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                artwork_id,
                file.page,
                file.path.to_string_lossy().to_string(),
                size.map(|value| value.0),
                size.map(|value| value.1),
                hash
            ],
        )?;
        if inserted == 0 {
            continue;
        }
        let image_id = tx.last_insert_rowid();

        // 只有封面需要缩略图；其它页只算感知哈希
        let thumb_target = if file.page == 0 {
            Some(thumbnails_dir.join(format!("{}.jpg", data.pixiv_id)))
        } else {
            None
        };
        queued.push((file.path.clone(), thumb_target, image_id));
    }

    // 页数以实际入库的图片数为准（多页作品中途失败也不会写出错误的页数）
    tx.execute(
        "UPDATE artworks SET page_count = (
             SELECT COUNT(*) FROM images WHERE artwork_id = ?1
         ) WHERE id = ?1",
        [artwork_id],
    )?;

    tx.commit()?;

    // 事务提交后再投递：后台线程写 phash 时行一定已经存在
    for (source, target, image_id) in queued {
        thumbnails::queue(source, target, image_id);
    }

    Ok(artwork_id)
}

/// 一次消费多少队列项：避免单个任务跑太久，剩下的下次继续。
pub const QUEUE_BATCH: i64 = 500;

/// 把作品加入下载队列（已存在则忽略）。
///
/// `origin` 用来区分队列属于收藏/手动还是作者订阅：检查某个订阅时只消费
/// 自己那一种，否则作者订阅会把别人排队的作品下载下来、还把来源标错。
pub fn enqueue(conn: &Connection, pixiv_id: i64, origin: &str) -> AppResult<()> {
    conn.execute(
        "INSERT OR IGNORE INTO download_queue (pixiv_id, origin) VALUES (?1, ?2)",
        params![pixiv_id, origin],
    )?;
    Ok(())
}

/// 取出该来源下可以立即处理的作品：`pending` 或已到重试时间的 `failed`。
pub fn pending_queue(conn: &Connection, limit: i64, origin: &str) -> AppResult<Vec<i64>> {
    let mut stmt = conn.prepare(
        "SELECT pixiv_id FROM download_queue
         WHERE status IN ('pending', 'failed')
           AND (next_retry_at IS NULL OR next_retry_at <= datetime('now'))
           AND COALESCE(origin, 'local') = ?2
         ORDER BY id ASC
         LIMIT ?1",
    )?;
    let rows = stmt.query_map(params![limit, origin], |row| row.get::<_, i64>(0))?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

/// 成功或确认已删除：移出队列。
pub fn complete_queue_item(conn: &Connection, pixiv_id: i64) -> AppResult<()> {
    conn.execute("DELETE FROM download_queue WHERE pixiv_id = ?1", [pixiv_id])?;
    Ok(())
}

/// 失败：记次数与退避时间（5 分钟起、指数增长、封顶 12 小时）。
pub fn fail_queue_item(conn: &Connection, pixiv_id: i64, error: &str) -> AppResult<()> {
    let message: String = error.chars().take(300).collect();
    conn.execute(
        "UPDATE download_queue SET
            status = 'failed',
            attempts = attempts + 1,
            last_error = ?2,
            updated_at = datetime('now'),
            next_retry_at = datetime('now', '+' || MIN(720, 5 * (1 << MIN(attempts, 4))) || ' minutes')
         WHERE pixiv_id = ?1",
        params![pixiv_id, message],
    )?;
    Ok(())
}

/// 下载 → 入库 → 缩略图 → 元数据的完整一段，供订阅检查复用。
pub async fn fetch_and_import(
    client: &PixivClient,
    conn: &mut Connection,
    data_dir: &Path,
    source_root: &Path,
    data: &IllustData,
    origin: &str,
) -> AppResult<usize> {
    let files = download_illust(client, data, source_root).await?;
    // 和远端页数核对：缺页时不入库，队列保留重试（已下载的页留在磁盘上）
    let expected = data.page_count.max(1) as usize;
    if !data.is_ugoira && files.len() < expected {
        return Err(AppError::internal(format!(
            "作品 {} 只下载到 {}/{} 页，保留已下载部分，稍后重试",
            data.pixiv_id,
            files.len(),
            expected
        )));
    }
    import_downloaded(conn, data_dir, data, &files, origin)?;

    let artwork_id: i64 = conn.query_row(
        "SELECT id FROM artworks WHERE pixiv_id = ?1",
        [data.pixiv_id],
        |row| row.get(0),
    )?;
    // 订阅下载的作品顺手把元数据写全（标题/作者/标签/AI），不用再等一次全局同步
    crate::commands::sync::apply_illust(conn, artwork_id, data)?;
    Ok(files.len())
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

/// 重新生成缩略图（覆盖已有文件）。本地作品沿用 `local-<id>.jpg` 命名。
#[tauri::command]
pub fn regenerate_thumbnails(artwork_ids: Vec<i64>) -> AppResult<usize> {
    let conn = open_library()?;
    let data_dir = paths::data_dir()?;
    let thumbnails_dir = data_dir.join("thumbnails");
    std::fs::create_dir_all(&thumbnails_dir)?;

    let mut regenerated = 0usize;
    for artwork_id in artwork_ids {
        let row: Option<(i64, Option<String>)> = conn
            .query_row(
                "SELECT pixiv_id,
                        (SELECT i.path FROM images i WHERE i.artwork_id = artworks.id
                         ORDER BY i.page ASC, i.id ASC LIMIT 1)
                 FROM artworks WHERE id = ?1",
                [artwork_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .ok();
        let Some((pixiv_id, Some(source))) = row else {
            continue;
        };

        let name = if pixiv_id < 0 {
            format!("local-{}.jpg", -pixiv_id)
        } else {
            format!("{pixiv_id}.jpg")
        };
        let target = thumbnails_dir.join(name);
        let _ = std::fs::remove_file(&target);
        if thumbnails::ensure_thumbnail(std::path::Path::new(&source), &target).is_ok() {
            regenerated += 1;
        }
    }
    Ok(regenerated)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn safe_name_strips_illegal_chars() {
        assert_eq!(safe_name("a/b:c*?\"<>|d"), "a_b_c______d");
        assert_eq!(safe_name("  trailing.  "), "trailing");
        assert_eq!(safe_name(""), "untitled");
        assert!(safe_name(&"x".repeat(300)).len() == 150);
    }

    #[test]
    fn resolve_dest_matches_old_layout() {
        let data = IllustData {
            pixiv_id: 12345678,
            title: "标题".to_string(),
            description: String::new(),
            author_id: 1,
            author_name: "作者".to_string(),
            author_avatar: None,
            create_date: None,
            page_count: 2,
            width: None,
            height: None,
            ai_type: 1,
            tags: Vec::new(),
            image_urls: Vec::new(),
            is_ugoira: false,
        };
        let root = Path::new("D:\\pics").join("作者");
        let (dir, prefix) = resolve_dest(&root, "作者", &data);
        assert_eq!(dir, Path::new("D:\\pics\\作者\\[作者] 标题"));
        assert_eq!(prefix, "[作者] 12345678_p");
    }

    #[test]
    fn url_extension_is_normalised() {
        assert_eq!(url_extension("https://i.pximg.net/a/b_p0.jpg"), ".jpg");
        assert_eq!(url_extension("https://i.pximg.net/a/b_p0.jpeg"), ".jpg");
        assert_eq!(url_extension("https://i.pximg.net/a/b_p0.png?x=1"), ".png");
        assert_eq!(url_extension("https://i.pximg.net/a/b"), ".jpg");
    }
}