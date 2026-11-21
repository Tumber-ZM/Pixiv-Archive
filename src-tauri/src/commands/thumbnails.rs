use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Sender};
use std::sync::{Mutex, OnceLock};

use image::ImageReader;
use rusqlite::params;

/// 缩略图长边像素（T07 验收项）
pub const THUMBNAIL_LONG_EDGE: u32 = 400;

/// 读取图片尺寸，不解码整幅像素。
pub fn probe_size(path: &Path) -> Option<(u32, u32)> {
    ImageReader::open(path)
        .ok()?
        .with_guessed_format()
        .ok()?
        .into_dimensions()
        .ok()
}

/// 生成 400px 长边 JPEG 缩略图。
///
/// 目标已存在就直接跳过并返回 `false`，所以可重复调用。
/// GIF 只取第一帧——列表不需要动图预览，而且解码全部帧代价太高。
pub fn ensure_thumbnail(source: &Path, target: &Path) -> Result<bool, String> {
    if target.is_file() {
        return Ok(false);
    }

    let decoded = ImageReader::open(source)
        .map_err(|error| format!("打开失败: {error}"))?
        .with_guessed_format()
        .map_err(|error| format!("识别格式失败: {error}"))?
        .decode()
        .map_err(|error| format!("解码失败: {error}"))?;

    let (width, height) = (decoded.width(), decoded.height());
    if width == 0 || height == 0 {
        return Err("尺寸为 0".to_string());
    }

    let scale = THUMBNAIL_LONG_EDGE as f32 / width.max(height) as f32;
    let thumbnail = if scale < 1.0 {
        decoded.resize(
            ((width as f32 * scale).round() as u32).max(1),
            ((height as f32 * scale).round() as u32).max(1),
            image::imageops::FilterType::Lanczos3,
        )
    } else {
        // 原图本来就小于 400px，直接按原样转存
        decoded
    };

    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent).map_err(|error| format!("创建目录失败: {error}"))?;
    }

    // 先写临时文件再改名，崩溃时不会留下半张图
    let temp = target.with_extension("tmp");
    thumbnail
        .save_with_format(&temp, image::ImageFormat::Jpeg)
        .map_err(|error| format!("编码失败: {error}"))?;
    std::fs::rename(&temp, target).map_err(|error| format!("改名失败: {error}"))?;

    Ok(true)
}

/// 感知哈希（dHash）：缩到 9×8 灰度后比较相邻像素，得到 64 位指纹。
///
/// 同一张图换个压缩或尺寸，指纹基本不变，存进 `images.phash` 供后续近似去重。
pub fn dhash(path: &Path) -> Option<String> {
    let decoded = ImageReader::open(path)
        .ok()?
        .with_guessed_format()
        .ok()?
        .decode()
        .ok()?;
    let small = decoded
        .resize_exact(9, 8, image::imageops::FilterType::Triangle)
        .to_luma8();

    let mut bits: u64 = 0;
    for y in 0..8 {
        for x in 0..8 {
            let left = small.get_pixel(x, y)[0];
            let right = small.get_pixel(x + 1, y)[0];
            bits = (bits << 1) | u64::from(right > left);
        }
    }
    Some(format!("{bits:016x}"))
}

/// 后台任务：缩略图生成 + 感知哈希写库。
///
/// 下载流程只投递路径，解码与编码在独立线程完成——否则大图解码会把下载热路径堵住。
/// 单线程消费是为了避免多个大图同时解码把内存打满；缩略图晚几秒生成不影响浏览。
struct ThumbnailJob {
    source: PathBuf,
    /// 首页才有缩略图目标，其它页只算 phash
    thumb_target: Option<PathBuf>,
    image_id: i64,
}

static THUMBNAIL_QUEUE: OnceLock<Mutex<Sender<ThumbnailJob>>> = OnceLock::new();

fn sender() -> &'static Mutex<Sender<ThumbnailJob>> {
    THUMBNAIL_QUEUE.get_or_init(|| {
        let (tx, rx) = mpsc::channel::<ThumbnailJob>();
        let spawned = std::thread::Builder::new()
            .name("thumbnail-worker".to_string())
            .spawn(move || {
                // 队列线程自己开连接：主线程的事务提交后再算，不会读到半成品
                let conn = crate::platform::paths::db_path()
                    .ok()
                    .and_then(|path| crate::db::open(&path).ok());
                for job in rx {
                    if let Some(target) = job.thumb_target.as_ref() {
                        let _ = ensure_thumbnail(&job.source, target);
                    }
                    if let (Some(conn), Some(hash)) = (conn.as_ref(), dhash(&job.source)) {
                        let _ = conn.execute(
                            "UPDATE images SET phash = ?1 WHERE id = ?2",
                            params![hash, job.image_id],
                        );
                    }
                }
            });
        // 线程起不来时仍把 sender 返回：投递会失败并被忽略，不影响下载
        let _ = spawned;
        Mutex::new(tx)
    })
}

/// 投递一张图片的后台任务（不阻塞调用方）。
pub fn queue(source: PathBuf, thumb_target: Option<PathBuf>, image_id: i64) {
    if let Ok(sender) = sender().lock() {
        let _ = sender.send(ThumbnailJob {
            source,
            thumb_target,
            image_id,
        });
    }
}