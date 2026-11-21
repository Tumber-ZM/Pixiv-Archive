use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use rusqlite::Connection;
use sha2::{Digest, Sha256};
use walkdir::WalkDir;

use super::thumbnails;
use crate::error::{AppError, AppResult};

const SUPPORTED_EXTENSIONS: &[&str] = &["jpg", "jpeg", "png", "gif", "webp"];

#[derive(Debug, Clone)]
pub struct ScanOptions {
    pub root: PathBuf,
    pub recursive: bool,
    pub generate_thumbnails: bool,
}

#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct ScanReport {
    pub scanned: usize,
    pub imported: usize,
    pub duplicates: usize,
    pub unsupported: usize,
    pub thumbnails: usize,
    pub errors: usize,
    pub cancelled: bool,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct ScanProgress {
    pub current: usize,
    pub total: usize,
    pub imported: usize,
    pub message: String,
}

/// 从一段文本里找出第一段长度 7..=10 的连续数字。
///
/// 长度下限是照旧项目 `scanner.py` 的正则 `(\d{7,10})_p(\d+)` 定的：
/// Pixiv 作品 ID 至少 7 位，短数字多半是页码或帧号，不能当 ID。
fn find_pixiv_number(text: &str) -> Option<i64> {
    let mut start: Option<usize> = None;
    for (index, ch) in text.char_indices() {
        if ch.is_ascii_digit() {
            if start.is_none() {
                start = Some(index);
            }
            continue;
        }
        if let Some(begin) = start.take() {
            let len = index - begin;
            if (7..=10).contains(&len) {
                return text[begin..index].parse().ok();
            }
        }
    }
    if let Some(begin) = start {
        let len = text.len() - begin;
        if (7..=10).contains(&len) {
            return text[begin..].parse().ok();
        }
    }
    None
}

/// 找**最后一段**长度 7..=10 的连续数字，等价于旧项目
/// `re.findall(r"\d{7,10}", stem)[-1]`：文件名里同时出现日期之类的数字时，取靠后的那个。
fn find_last_pixiv_number(text: &str) -> Option<i64> {
    let mut last: Option<i64> = None;
    let mut index = 0;
    while index < text.len() {
        if !text.as_bytes()[index].is_ascii_digit() {
            index += 1;
            continue;
        }
        let begin = index;
        while index < text.len() && text.as_bytes()[index].is_ascii_digit() {
            index += 1;
        }
        if (7..=10).contains(&(index - begin)) {
            if let Ok(value) = text[begin..index].parse::<i64>() {
                last = Some(value);
            }
        }
    }
    last
}

/// 拆出 `12345678_p0` / `12345678p0` 里的 (作品 ID, 页码)，大小写不敏感。
///
/// 只对 ASCII 做折叠，折叠后的字符串与原文等长，所以可以拿同一个下标切 `stem`。
fn split_page_suffix(stem: &str) -> Option<(i64, i64)> {
    let lower = stem.to_ascii_lowercase();
    for separator in ["_p", "p"] {
        let Some(position) = lower.rfind(separator) else {
            continue;
        };
        let id_part = &stem[..position];
        let page_part = &stem[position + separator.len()..];
        if let (Some(id), Ok(page)) = (find_last_pixiv_number(id_part), page_part.parse::<i64>()) {
            return Some((id, page));
        }
    }
    None
}

/// 解析作品 ID 与页码。
///
/// 顺序对齐旧项目 `app/scanner.py` 的 `extract_pixiv_info`：
/// 1. `[标题] 12345678_p0.jpg` —— 文件名里带 `_p{页码}`
/// 2. `[标题] 12345678p0.jpg` —— 页码前没有下划线
/// 3. `[作者] 112290978.png` —— 文件名里只有 ID，页码按 0 处理
/// 4. `[作者] 138838560\164.jpg` —— 作品 ID 在**父文件夹名**里，文件名只是帧号/页码
///
/// 全都不是就返回 `None`，调用方会跳过该文件。绝不能拿短数字当 ID：
/// 动图帧 `164.jpg` 会被误判成作品 164，一个动图就会炸成上百个"作品"。
fn parse_pixiv_id(path: &Path) -> Option<(i64, i64)> {
    let stem = path.file_stem().and_then(|value| value.to_str())?;

    // 形态 1 / 2：文件名自带 ID 和页码
    if let Some(parsed) = split_page_suffix(stem) {
        return Some(parsed);
    }

    // 形态 3：文件名里只有 ID（如 `[老鼠公爵(MouseKKK)] 112290978.png`），页码按 0
    if let Some(id) = find_last_pixiv_number(stem) {
        return Some((id, 0));
    }

    // 形态 4：文件夹名带 ID，文件名当页码
    if let Some(parent) = path.parent().and_then(|dir| dir.file_name()) {
        if let Some(id) = find_pixiv_number(&parent.to_string_lossy()) {
            let page = stem.parse::<i64>().unwrap_or(0);
            return Some((id, page));
        }
    }

    None
}

/// 给认不出 Pixiv ID 的本地图片派生一个稳定的负数 ID。
///
/// `artworks.pixiv_id` 是 `UNIQUE NOT NULL`，又不能统一填 0（全库只能有一行），
/// 所以取 sha256 前 15 个十六进制位（60 bit）取负再加一。同一张图内容不变，
/// 每次扫描派生的 ID 都一样；真实 Pixiv ID 恒为正，不会撞车。
fn local_pixiv_id(hash: &str) -> i64 {
    let prefix = hash.get(..15).unwrap_or(hash);
    let value = i64::from_str_radix(prefix, 16).unwrap_or(0);
    -(value + 1)
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

fn collect_files(options: &ScanOptions) -> Vec<PathBuf> {
    let walker = if options.recursive {
        WalkDir::new(&options.root)
    } else {
        WalkDir::new(&options.root).max_depth(1)
    };

    walker
        .into_iter()
        .filter_map(Result::ok)
        .filter(|entry| entry.file_type().is_file())
        .filter(|entry| {
            entry
                .path()
                .extension()
                .and_then(|ext| ext.to_str())
                .map(|ext| SUPPORTED_EXTENSIONS.contains(&ext.to_ascii_lowercase().as_str()))
                .unwrap_or(false)
        })
        .map(|entry| entry.into_path())
        .collect()
}

/// 扫描目录并入库。
///
/// - 用 `images.sha256` 去重：同一张图换个文件名再扫一次不会重复入库
/// - 每处理一张就回调一次进度，节流交给调用方
/// - `cancel` 被置位时立刻停下，已入库的数据保留（T07 验收项）
pub fn scan<F>(
    conn: &Connection,
    data_dir: &Path,
    options: &ScanOptions,
    cancel: &AtomicBool,
    mut on_progress: F,
) -> AppResult<ScanReport>
where
    F: FnMut(ScanProgress),
{
    if !options.root.is_dir() {
        return Err(AppError::invalid(format!(
            "目录不存在：{}",
            options.root.display()
        )));
    }

    let files = collect_files(options);
    let total = files.len();
    let thumbnails_dir = data_dir.join("thumbnails");

    let mut report = ScanReport::default();

    for (index, path) in files.iter().enumerate() {
        if cancel.load(Ordering::Relaxed) {
            report.cancelled = true;
            break;
        }

        report.scanned += 1;

        // 关键性能点：先按路径查库，命中就完全不用读文件算哈希。
        // 旧项目也是按 (artwork_id, path) 判重的，所以重复扫描已入库目录很快；
        // 之前这里对每张图都读全文件算 SHA256，2000 张就是几十 GB 的读盘。
        let display_path = path.to_string_lossy().to_string();
        let known_path: bool = conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM images WHERE path = ?1)",
                [&display_path],
                |row| row.get(0),
            )
            .unwrap_or(false);
        if known_path {
            report.duplicates += 1;
            emit(&mut on_progress, index, total, &report, "已存在，跳过");
            continue;
        }

        // 只有新文件才读内容算哈希
        let hash = match file_sha256(path) {
            Ok(value) => value,
            Err(_) => {
                report.errors += 1;
                continue;
            }
        };

        // 认不出 Pixiv ID 的本地图片也照样入库：ID 由内容哈希派生，页码按 0
        let (pixiv_id, page) = match parse_pixiv_id(path) {
            Some(parsed) => parsed,
            None => (local_pixiv_id(&hash), 0),
        };

        // 路径是新的，但同一内容可能已经挂在别的作品下：保持原有的全局哈希去重
        let exists: bool = conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM images WHERE sha256 = ?1)",
                [&hash],
                |row| row.get(0),
            )
            .unwrap_or(false);
        if exists {
            report.duplicates += 1;
            emit(&mut on_progress, index, total, &report, "已存在，跳过");
            continue;
        }

        let stem = path
            .file_stem()
            .and_then(|value| value.to_str())
            .unwrap_or_default()
            .to_string();
        let size = thumbnails::probe_size(path);
        let metadata = std::fs::metadata(path).ok();

        let inserted = conn.execute(
            "INSERT OR IGNORE INTO artworks (pixiv_id, title, page_count, width, height, local_path)
             VALUES (?1, ?2, 1, ?3, ?4, ?5)",
            rusqlite::params![
                pixiv_id,
                stem,
                size.map(|value| value.0),
                size.map(|value| value.1),
                display_path,
            ],
        );

        match inserted {
            Ok(_) => {
                let artwork_id: i64 = conn.query_row(
                    "SELECT id FROM artworks WHERE pixiv_id = ?1",
                    [pixiv_id],
                    |row| row.get(0),
                )?;

                // 唯一约束 (artwork_id, page) 会挡住重复文件，重复的不再计数
                let inserted_rows = conn.execute(
                    "INSERT OR IGNORE INTO images (artwork_id, page, path, width, height, sha256)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                    rusqlite::params![
                        artwork_id,
                        page,
                        display_path,
                        size.map(|value| value.0),
                        size.map(|value| value.1),
                        hash,
                    ],
                )?;

                if inserted_rows > 0 {
                    report.imported += 1;
                }

                // 同一作品的多页共用一个 pixiv_id，页数要跟着实际入库的图走
                let _ = conn.execute(
                    "UPDATE artworks SET page_count = (
                         SELECT COUNT(*) FROM images WHERE artwork_id = artworks.id
                     ) WHERE id = ?1",
                    [artwork_id],
                );

                if options.generate_thumbnails {
                    // 只取页码最小的一张做封面缩略图，避免多页作品反复覆盖；
                    // 无 Pixiv ID 的本地图片用 local-<哈希段> 命名，避免负号混进文件名
                    let thumb_name = if pixiv_id < 0 {
                        format!("local-{}.jpg", -pixiv_id)
                    } else {
                        format!("{pixiv_id}.jpg")
                    };
                    let target = thumbnails_dir.join(thumb_name);
                    let has_earlier_page = conn
                        .query_row(
                            "SELECT EXISTS(SELECT 1 FROM images WHERE artwork_id = ?1 AND page < ?2)",
                            rusqlite::params![artwork_id, page],
                            |row| row.get::<_, bool>(0),
                        )
                        .unwrap_or(false);

                    if !has_earlier_page {
                        match thumbnails::ensure_thumbnail(path, &target) {
                            Ok(true) => report.thumbnails += 1,
                            Ok(false) => {}
                            Err(_) => report.errors += 1,
                        }
                    }
                }
            }
            Err(_) => report.errors += 1,
        }

        let _ = metadata;
        emit(&mut on_progress, index, total, &report, "导入");
    }

    Ok(report)
}

fn emit<F>(on_progress: &mut F, index: usize, total: usize, report: &ScanReport, message: &str)
where
    F: FnMut(ScanProgress),
{
    on_progress(ScanProgress {
        current: index + 1,
        total,
        imported: report.imported,
        message: message.to_string(),
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::schema;

    fn memory_db() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(schema::SCHEMA_SQL).unwrap();
        conn
    }

    /// 每个测试用独立子目录，并且每次从空目录开始——
/// 残留的缩略图或图片会让「导入数 / 缩略图数」这类计数对不上。
    fn fixture_dir(name: &str) -> PathBuf {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("scan-fixture")
            .join(name);
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn parses_pixiv_ids_from_common_names() {
        let parse = |raw: &str| parse_pixiv_id(Path::new(raw));

        // 形态 1：文件名里带 ID 和页码
        assert_eq!(parse("12345678_p0.jpg"), Some((12345678, 0)));
        assert_eq!(parse("[标题] 12345678_p3.jpg"), Some((12345678, 3)));
        assert_eq!(parse("12345678_ugoira_p0.jpg"), Some((12345678, 0)));

        // 形态 2：页码前没有下划线，大小写不敏感
        assert_eq!(parse("12345678p0.jpg"), Some((12345678, 0)));
        assert_eq!(parse("12345678P2.JPG"), Some((12345678, 2)));

        // 形态 3：文件名里只有 ID（真实遇到过的命名），页码按 0
        assert_eq!(
            parse("[老鼠公爵(MouseKKK)] 112290978.png"),
            Some((112290978, 0))
        );
        assert_eq!(parse("12345678.jpg"), Some((12345678, 0)));

        // 形态 4：作品 ID 在父文件夹，文件名只是帧号/页码
        assert_eq!(
            parse("[作者名] 138838560/164.jpg"),
            Some((138838560, 164))
        );
        assert_eq!(parse("[作者名] 138838560/0.jpg"), Some((138838560, 0)));

        // 认不出来的必须拒绝——绝不能把 164.jpg 当成作品 164
        assert_eq!(parse("随机图片.png"), None);
        assert_eq!(parse("164.jpg"), None);
        assert_eq!(parse("1234_p0.jpg"), None, "不足 7 位的数字不是作品 ID");
    }

    #[test]
    fn long_number_requires_seven_digits() {
        assert_eq!(find_pixiv_number("123456"), None);
        assert_eq!(find_pixiv_number("1234567"), Some(1234567));
        assert_eq!(find_pixiv_number("1234567890"), Some(1234567890));
        assert_eq!(find_pixiv_number("12345678901"), None, "超过 10 位不是作品 ID");
        assert_eq!(find_pixiv_number("[作者] 138838560 合集"), Some(138838560));

        // 取最后一个合法数字，和旧项目 `findall(...)[-1]` 一致
        assert_eq!(find_last_pixiv_number("112290978_20230101"), Some(20230101));
        assert_eq!(find_last_pixiv_number("164"), None);
        assert_eq!(find_last_pixiv_number("123456"), None);
    }

    #[test]
    fn local_ids_are_stable_and_negative() {
        let hash = "a3f5c9d1e7b2468a".to_string() + &"0".repeat(48);
        let first = local_pixiv_id(&hash);
        assert_eq!(first, local_pixiv_id(&hash), "同一内容必须派生出同一个 ID");
        assert!(first < 0, "本地 ID 用负数，和真实 Pixiv ID 分开");
        assert_ne!(first, local_pixiv_id(&("b".repeat(64))));
    }

    #[test]
    fn scans_png_and_skips_duplicates() {
        let dir = fixture_dir("scan");
        let root = dir.join("root");
        let data = dir.join("data");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&data).unwrap();

        // 造两张内容不同的 PNG：一张正常命名，一张无 ID
        let first_image = image::RgbImage::from_fn(8, 6, |x, y| {
            image::Rgb([(x * 30) as u8, (y * 40) as u8, 128])
        });
        let first = root.join("99887766_p0.png");
        first_image.save(&first).unwrap();

        // 必须和上面不同内容，否则会被 sha256 去重挡掉
        let second_image = image::RgbImage::from_fn(6, 9, |x, y| {
            image::Rgb([(y * 20) as u8, (x * 50) as u8, 60])
        });
        let second = root.join("无ID图片.png");
        second_image.save(&second).unwrap();

        let conn = memory_db();
        let cancel = AtomicBool::new(false);
        let options = ScanOptions {
            root: root.clone(),
            recursive: true,
            generate_thumbnails: true,
        };

        let report = scan(&conn, &data, &options, &cancel, |_| {}).unwrap();
        assert_eq!(report.scanned, 2);
        assert_eq!(report.imported, 2, "认不出 ID 的本地图片也要入库");
        assert_eq!(report.unsupported, 0);
        assert_eq!(report.duplicates, 0);
        assert_eq!(report.thumbnails, 2);

        // 无 ID 的那张应当以负数本地 ID 入库，标题取文件名
        let local_id: i64 = conn
            .query_row(
                "SELECT pixiv_id FROM artworks WHERE pixiv_id < 0",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(local_id < 0);
        let local_title: String = conn
            .query_row(
                "SELECT title FROM artworks WHERE pixiv_id = ?1",
                [local_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(local_title, "无ID图片");
        assert!(
            data.join("thumbnails")
                .join(format!("local-{}.jpg", -local_id))
                .is_file(),
            "本地图片的缩略图应落在 local-<id>.jpg"
        );

        // 缩略图长边应为 400 以内
        let thumb = data.join("thumbnails").join("99887766.jpg");
        assert!(thumb.is_file(), "应生成缩略图");
        let (width, height) = thumbnails::probe_size(&thumb).unwrap();
        assert!(width.max(height) <= 400);

        // 再扫一次：两张都命中哈希去重
        let again = scan(&conn, &data, &options, &cancel, |_| {}).unwrap();
        assert_eq!(again.imported, 0);
        assert_eq!(again.duplicates, 2);
        assert_eq!(again.unsupported, 0);

        // 取消：一开始就置位，应该什么都不导入
        let fresh = memory_db();
        let cancelled = AtomicBool::new(true);
        let report = scan(&fresh, &data, &options, &cancelled, |_| {}).unwrap();
        assert!(report.cancelled);
        assert_eq!(report.imported, 0);
    }

    #[test]
    fn rejects_missing_directory() {
        let conn = memory_db();
        let cancel = AtomicBool::new(false);
        let options = ScanOptions {
            root: PathBuf::from("Z:\\definitely-not-here"),
            recursive: false,
            generate_thumbnails: false,
        };
        let error = scan(&conn, &fixture_dir("missing"), &options, &cancel, |_| {}).unwrap_err();
        assert_eq!(error.code, "INVALID_INPUT");
    }

    #[test]
    fn thumbnails_helper_is_idempotent() {
        let dir = fixture_dir("thumb");
        let source = dir.join("thumb-source.png");
        let target = dir.join("thumb-out.jpg");
        let _ = std::fs::remove_file(&target);

        let image = image::RgbImage::from_fn(40, 20, |_, _| image::Rgb([200, 120, 60]));
        image.save(&source).unwrap();

        assert!(thumbnails::ensure_thumbnail(&source, &target).unwrap());
        // 第二次应该跳过
        assert!(!thumbnails::ensure_thumbnail(&source, &target).unwrap());

        let (width, height) = thumbnails::probe_size(&target).unwrap();
        assert_eq!(width.max(height), 40, "原图小于 400 时不放大");
    }
}