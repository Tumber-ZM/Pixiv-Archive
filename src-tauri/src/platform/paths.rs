use std::path::{Path, PathBuf};

use crate::error::{AppError, AppResult};

/// 运行时数据目录名。刻意用下划线风格且与旧项目目录名不同，
/// 避免任何工具把它误当成旧项目的 `Pixiv-Archive` 目录写进去。
pub const APP_DIR_NAME: &str = "pixiv_archive_desktop";

/// 新库文件名。**不叫 `archive.db`**，见 `03-数据模型与旧库兼容.md`。
pub const DB_FILE_NAME: &str = "library.db";

/// 旧项目目录的默认位置（只读来源）。
pub const DEFAULT_LEGACY_DIR: &str = r"D:\Little Tool\Pixiv-Archive";

fn appdata_base() -> AppResult<PathBuf> {
    std::env::var_os("APPDATA")
        .map(PathBuf::from)
        .ok_or_else(|| AppError::internal("环境变量 APPDATA 缺失，无法定位数据目录"))
}

/// 运行时数据目录：便携模式——跟着 exe 走（`<exe 目录>/data`）。
///
/// - 环境变量 `PIXIV_ARCHIVE_DATA_DIR` 优先，方便调试/测试；
/// - exe 同目录可写就用它（绿色版：拷贝整个文件夹即可迁移）；
/// - 不可写时（例如装在 Program Files）回退 `%APPDATA%\pixiv_archive_desktop`。
pub fn data_dir() -> AppResult<PathBuf> {
    if let Ok(value) = std::env::var("PIXIV_ARCHIVE_DATA_DIR") {
        let path = PathBuf::from(value.trim());
        if !path.as_os_str().is_empty() {
            std::fs::create_dir_all(&path)?;
            return Ok(path);
        }
    }

    if let Some(dir) = portable_data_dir() {
        return Ok(dir);
    }

    Ok(appdata_base()?.join(APP_DIR_NAME))
}

/// exe 同目录下的 `data/`：能创建且能写入才算可用。
fn portable_data_dir() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let parent = exe.parent()?;
    let dir = parent.join("data");
    std::fs::create_dir_all(&dir).ok()?;

    // 真的写一下，避免只读目录/权限问题到后面才炸
    let probe = dir.join(".write-probe");
    std::fs::write(&probe, b"ok").ok()?;
    let _ = std::fs::remove_file(&probe);
    Some(dir)
}

/// T04 的画廊查询接入后使用。
#[allow(dead_code)]
pub fn db_path() -> AppResult<PathBuf> {
    Ok(data_dir()?.join(DB_FILE_NAME))
}

pub fn thumbnails_dir() -> AppResult<PathBuf> {
    Ok(data_dir()?.join("thumbnails"))
}

pub fn metadata_dir() -> AppResult<PathBuf> {
    Ok(data_dir()?.join("metadata"))
}

pub fn avatars_dir() -> AppResult<PathBuf> {
    Ok(data_dir()?.join("avatars"))
}

pub fn logs_dir() -> AppResult<PathBuf> {
    Ok(data_dir()?.join("logs"))
}

pub fn legacy_dir() -> PathBuf {
    PathBuf::from(DEFAULT_LEGACY_DIR)
}

/// 建齐运行时目录。缺目录时 SQLite 与缩略图都会静默失败，所以启动时先铺好。
pub fn ensure_dirs() -> AppResult<()> {
    for dir in [
        data_dir()?,
        thumbnails_dir()?,
        metadata_dir()?,
        avatars_dir()?,
        logs_dir()?,
    ] {
        std::fs::create_dir_all(&dir)?;
    }
    Ok(())
}

/// 旧库是否存在。用于首启分支判断。
/// 旧库是否存在。用于首启分支判断。
#[allow(dead_code)]
pub fn has_legacy_db(dir: &Path) -> bool {
    dir.join("archive.db").is_file()
}

/// 删除某作品的派生物：缩略图与元数据 JSON。
///
/// 作品记录移除后这两个文件即失去引用，必须一并清理，否则会残留孤儿文件。
/// 与旧项目 `paths.remove_side_files` 一致。
pub fn remove_side_files(data_dir: &Path, pixiv_id: i64) -> Vec<PathBuf> {
    let mut removed = Vec::new();
    for path in [
        data_dir.join("thumbnails").join(format!("{pixiv_id}.jpg")),
        data_dir.join("metadata").join(format!("{pixiv_id}.json")),
    ] {
        if path.is_file() && std::fs::remove_file(&path).is_ok() {
            removed.push(path);
        }
    }
    removed
}