use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;

use crate::error::{AppError, AppResult};
use crate::migration::{import_legacy as run_import, ImportReport};
use crate::platform::paths;

fn stamp() -> String {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
        .to_string()
}

fn sidecar(path: &Path, suffix: &str) -> PathBuf {
    let mut value = path.as_os_str().to_os_string();
    value.push(suffix);
    PathBuf::from(value)
}

fn dir_size(dir: &Path) -> u64 {
    let mut total = 0u64;
    let Ok(entries) = std::fs::read_dir(dir) else {
        return 0;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            total += dir_size(&path);
        } else if let Ok(meta) = entry.metadata() {
            total += meta.len();
        }
    }
    total
}

#[derive(Debug, Clone, Serialize)]
pub struct DataSizes {
    pub db: u64,
    pub thumbnails: u64,
    pub metadata: u64,
}

/// 设置页「数据迁移」顶部显示的当前数据大小。
#[tauri::command]
pub fn data_sizes() -> AppResult<DataSizes> {
    let data = paths::data_dir()?;
    let db = std::fs::metadata(data.join(paths::DB_FILE_NAME))
        .map(|meta| meta.len())
        .unwrap_or(0);
    Ok(DataSizes {
        db,
        thumbnails: dir_size(&data.join("thumbnails")),
        metadata: dir_size(&data.join("metadata")),
    })
}

/// 把库 + 缩略图 + 元数据打包成 zip（旧项目「导出数据包」）。
#[tauri::command]
pub async fn export_data_bundle(dest_dir: String) -> AppResult<String> {
    let data = paths::data_dir()?;
    let dest = PathBuf::from(dest_dir.trim());
    if !dest.is_dir() {
        return Err(AppError::invalid("导出目录不存在"));
    }
    let zip_path = dest.join(format!("pixiv-archive-{}.zip", stamp()));

    tauri::async_runtime::spawn_blocking(move || -> AppResult<String> {
        write_bundle(&data, &zip_path)?;
        Ok(zip_path.to_string_lossy().to_string())
    })
    .await
    .map_err(|error| AppError::internal(format!("导出任务异常退出：{error}")))?
}

fn write_bundle(data: &Path, zip_path: &Path) -> AppResult<()> {
    let db_path = data.join(paths::DB_FILE_NAME);
    if !db_path.is_file() {
        return Err(AppError::not_found(
            "还没有数据库，先导入旧数据或扫描一次图片目录",
        ));
    }

    // WAL 里的内容先落盘，否则打包出来的库可能缺最近写入
    {
        let conn = crate::db::open(&db_path)?;
        let _ = conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);");
    }

    let file = std::fs::File::create(zip_path)?;
    let mut zip = zip::ZipWriter::new(file);
    let options =
        zip::write::SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated);

    add_file(&mut zip, options, &db_path, "library.db")?;
    add_dir(&mut zip, options, &data.join("thumbnails"), "thumbnails")?;
    add_dir(&mut zip, options, &data.join("metadata"), "metadata")?;

    let manifest = serde_json::json!({
        "version": 1,
        "created_at": stamp(),
        "app": "pixiv-archive-desktop",
    });
    zip.start_file("manifest.json", options)?;
    zip.write_all(manifest.to_string().as_bytes())?;
    zip.finish()?;
    Ok(())
}

fn add_file(
    zip: &mut zip::ZipWriter<std::fs::File>,
    options: zip::write::SimpleFileOptions,
    path: &Path,
    name: &str,
) -> AppResult<()> {
    zip.start_file(name, options)?;
    let mut file = std::fs::File::open(path)?;
    let mut buffer = Vec::new();
    file.read_to_end(&mut buffer)?;
    zip.write_all(&buffer)?;
    Ok(())
}

fn add_dir(
    zip: &mut zip::ZipWriter<std::fs::File>,
    options: zip::write::SimpleFileOptions,
    root: &Path,
    prefix: &str,
) -> AppResult<usize> {
    if !root.is_dir() {
        return Ok(0);
    }
    let mut count = 0;
    for entry in walkdir::WalkDir::new(root).into_iter().filter_map(Result::ok) {
        if !entry.file_type().is_file() {
            continue;
        }
        let rel = entry
            .path()
            .strip_prefix(root)
            .unwrap_or(entry.path())
            .to_string_lossy()
            .replace('\\', "/");
        zip.start_file(format!("{prefix}/{rel}"), options)?;
        let mut file = std::fs::File::open(entry.path())?;
        let mut buffer = Vec::new();
        file.read_to_end(&mut buffer)?;
        zip.write_all(&buffer)?;
        count += 1;
    }
    Ok(count)
}

#[derive(Debug, Clone, Serialize)]
pub struct RelinkReport {
    pub relinked: usize,
    pub missing: usize,
    pub ambiguous: usize,
}

/// 把库里失效的图片绝对路径按文件名重新关联到当前图片目录。
///
/// 同名多份时不动（宁可留缺失，也不猜错）。
#[tauri::command]
pub fn relink_images() -> AppResult<RelinkReport> {
    let data = paths::data_dir()?;
    let source = crate::commands::settings::image_source_dir()?
        .map(PathBuf::from)
        .ok_or_else(|| AppError::invalid("还没有设置图片目录（设置 → 基础）"))?;
    if !source.is_dir() {
        return Err(AppError::invalid(format!(
            "图片目录不存在：{}",
            source.display()
        )));
    }

    let mut index: std::collections::HashMap<String, Vec<PathBuf>> = std::collections::HashMap::new();
    for entry in walkdir::WalkDir::new(&source).into_iter().filter_map(Result::ok) {
        if !entry.file_type().is_file() {
            continue;
        }
        let name = entry.file_name().to_string_lossy().to_lowercase();
        index.entry(name).or_default().push(entry.into_path());
    }

    let conn = crate::db::open(&data.join(paths::DB_FILE_NAME))?;
    let rows: Vec<(i64, String)> = {
        let mut stmt = conn.prepare("SELECT id, path FROM images")?;
        let mapped = stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?;
        mapped.filter_map(Result::ok).collect()
    };

    let mut relinked = 0usize;
    let mut missing = 0usize;
    let mut ambiguous = 0usize;

    for (id, path) in rows {
        if Path::new(&path).exists() {
            continue;
        }
        let name = Path::new(&path)
            .file_name()
            .map(|value| value.to_string_lossy().to_lowercase())
            .unwrap_or_default();
        match index.get(&name) {
            None => missing += 1,
            Some(matches) if matches.len() > 1 => ambiguous += 1,
            Some(matches) => {
                let _ = conn.execute(
                    "UPDATE images SET path = ?1 WHERE id = ?2",
                    rusqlite::params![matches[0].to_string_lossy().to_string(), id],
                );
                relinked += 1;
            }
        }
    }

    if relinked > 0 {
        let _ = conn.execute(
            "UPDATE artworks SET local_path = (
                 SELECT i.path FROM images i WHERE i.artwork_id = artworks.id
                 ORDER BY i.page LIMIT 1
             ) WHERE id IN (SELECT artwork_id FROM images)",
            [],
        );
    }

    Ok(RelinkReport {
        relinked,
        missing,
        ambiguous,
    })
}

#[derive(Debug, Clone, Serialize)]
pub struct BundleImportReport {
    pub artworks: i64,
    pub images: i64,
    pub thumbs_restored: usize,
    pub backed_up_to: Option<String>,
    pub relinked: Option<RelinkReport>,
}

/// 导入数据包：先解压到临时目录校验，再备份现有库、替换、合并缩略图/元数据。
#[tauri::command]
pub async fn import_data_bundle(zip_path: String, relink: bool) -> AppResult<BundleImportReport> {
    let data = paths::data_dir()?;
    let source = PathBuf::from(zip_path.trim());
    if !source.is_file() {
        return Err(AppError::not_found(format!(
            "数据包不存在：{}",
            source.display()
        )));
    }

    tauri::async_runtime::spawn_blocking(move || read_bundle(&data, &source, relink))
        .await
        .map_err(|error| AppError::internal(format!("导入任务异常退出：{error}")))?
}

fn read_bundle(data: &Path, zip_path: &Path, relink: bool) -> AppResult<BundleImportReport> {
    let file = std::fs::File::open(zip_path)?;
    let mut archive = zip::ZipArchive::new(file)
        .map_err(|error| AppError::invalid(format!("不是有效的数据包：{error}")))?;

    let staging = data.join(format!(".import-{}", stamp()));
    let _ = std::fs::remove_dir_all(&staging);
    std::fs::create_dir_all(&staging)?;

    for index in 0..archive.len() {
        let mut entry = archive
            .by_index(index)
            .map_err(|error| AppError::invalid(format!("数据包损坏：{error}")))?;
        // enclosed_name 会挡掉 ../ 之类的路径穿越
        let Some(relative) = entry.enclosed_name() else {
            continue;
        };
        let name = relative.to_string_lossy().replace('\\', "/");
        let target_rel = if name == "library.db" {
            Some(PathBuf::from("library.db"))
        } else if let Some(rest) = name.strip_prefix("thumbnails/") {
            Some(Path::new("thumbnails").join(rest))
        } else if let Some(rest) = name.strip_prefix("metadata/") {
            Some(Path::new("metadata").join(rest))
        } else {
            None
        };
        let Some(relative) = target_rel else {
            continue;
        };

        let target = staging.join(&relative);
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let mut out = std::fs::File::create(&target)?;
        std::io::copy(&mut entry, &mut out)?;
    }

    let staged_db = staging.join("library.db");
    if !staged_db.is_file() {
        let _ = std::fs::remove_dir_all(&staging);
        return Err(AppError::invalid("数据包里没有 library.db"));
    }

    // 先校验能打开、能数出记录，坏包不覆盖现有库。
    // 走 init 而不是 open：旧版本导出的包里没有 subs 表，换进来之后要能直接迁移。
    let (artworks, images) = {
        let conn = crate::db::init(&staged_db)?;
        let artworks: i64 = conn.query_row("SELECT COUNT(*) FROM artworks", [], |row| row.get(0))?;
        let images: i64 = conn.query_row("SELECT COUNT(*) FROM images", [], |row| row.get(0))?;
        (artworks, images)
    };

    let dst_db = data.join(paths::DB_FILE_NAME);
    let backed_up_to = if dst_db.exists() {
        let backup = data.join(format!("library.db.bak-{}", stamp()));
        for suffix in ["-wal", "-shm"] {
            let from = sidecar(&dst_db, suffix);
            if from.is_file() {
                let _ = std::fs::rename(&from, sidecar(&backup, suffix));
            }
        }
        std::fs::rename(&dst_db, &backup)?;
        Some(backup.to_string_lossy().to_string())
    } else {
        None
    };

    std::fs::rename(&staged_db, &dst_db)?;
    let thumbs_restored = merge_dir(&staging.join("thumbnails"), &data.join("thumbnails"))?;
    let _ = merge_dir(&staging.join("metadata"), &data.join("metadata"));
    let _ = std::fs::remove_dir_all(&staging);

    let relinked = if relink { Some(relink_images()?) } else { None };

    Ok(BundleImportReport {
        artworks,
        images,
        thumbs_restored,
        backed_up_to,
        relinked,
    })
}

fn merge_dir(src: &Path, dst: &Path) -> AppResult<usize> {
    if !src.is_dir() {
        return Ok(0);
    }
    std::fs::create_dir_all(dst)?;
    let mut merged = 0usize;
    for entry in walkdir::WalkDir::new(src).into_iter().filter_map(Result::ok) {
        if !entry.file_type().is_file() {
            continue;
        }
        let rel = entry.path().strip_prefix(src).unwrap_or(entry.path());
        let target = dst.join(rel);
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::copy(entry.path(), &target)?;
        merged += 1;
    }
    Ok(merged)
}

#[tauri::command]
pub fn import_legacy(
    legacy_dir: Option<String>,
    include_thumbs: bool,
    include_metadata: bool,
) -> AppResult<ImportReport> {
    let source = legacy_dir
        .map(PathBuf::from)
        .unwrap_or_else(paths::legacy_dir);
    let data = paths::data_dir()?;
    paths::ensure_dirs()?;
    run_import(&source, &data, include_thumbs, include_metadata)
}