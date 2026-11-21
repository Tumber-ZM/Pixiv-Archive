use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;

use crate::db;
use crate::error::{AppError, AppResult};
use crate::platform::paths::DB_FILE_NAME;

#[derive(Debug, Clone, Serialize)]
pub struct ImportReport {
    pub artworks: i64,
    pub images: i64,
    pub thumbs_copied: usize,
    pub backed_up_to: Option<String>,
}

fn stamp() -> String {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
        .to_string()
}

fn sidecar(path: &Path, suffix: &str) -> PathBuf {
    let mut s = path.as_os_str().to_os_string();
    s.push(suffix);
    PathBuf::from(s)
}

/// 复制目录树，返回复制的文件数。源目录不存在时算 0，不报错
/// （旧项目可能没有 metadata/ 或 thumbnails/）。
fn copy_dir_all(src: &Path, dst: &Path) -> AppResult<usize> {
    if !src.is_dir() {
        return Ok(0);
    }
    std::fs::create_dir_all(dst)?;
    let mut copied = 0usize;
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let from = entry.path();
        let to = dst.join(entry.file_name());
        if from.is_dir() {
            copied += copy_dir_all(&from, &to)?;
        } else if from.is_file() {
            std::fs::copy(&from, &to)?;
            copied += 1;
        }
    }
    Ok(copied)
}

/// 把旧库连同派生数据复制进新数据目录。
///
/// **只读保证**：对 `legacy_dir` 只做 `read` / `copy`，没有任何写、删、改名落在旧目录。
pub fn import_legacy(
    legacy_dir: &Path,
    data_dir: &Path,
    include_thumbs: bool,
    include_metadata: bool,
) -> AppResult<ImportReport> {
    let src_db = legacy_dir.join("archive.db");
    if !src_db.is_file() {
        return Err(AppError::not_found(format!(
            "该目录下没有 archive.db，请选择旧项目的根目录：{}",
            src_db.display()
        )));
    }

    std::fs::create_dir_all(data_dir)?;
    let dst_db = data_dir.join(DB_FILE_NAME);

    // 目标已有库先备份。WAL/SHM 边车一并挪走，否则残留会把备份库带进不一致状态。
    let backed_up_to = if dst_db.exists() {
        let bak = data_dir.join(format!("library.db.bak-{}", stamp()));
        for suffix in ["-wal", "-shm"] {
            let from = sidecar(&dst_db, suffix);
            if from.is_file() {
                let _ = std::fs::rename(&from, sidecar(&bak, suffix));
            }
        }
        std::fs::rename(&dst_db, &bak)?;
        Some(bak.to_string_lossy().to_string())
    } else {
        None
    };

    // 主库 + 边车文件。旧库若处于 WAL 状态，只复制 .db 会丢掉尚未 checkpoint 的写入。
    std::fs::copy(&src_db, &dst_db)?;
    for suffix in ["-wal", "-shm"] {
        let from = sidecar(&src_db, suffix);
        if from.is_file() {
            std::fs::copy(&from, sidecar(&dst_db, suffix))?;
        }
    }

    let thumbs_copied = if include_thumbs {
        copy_dir_all(&legacy_dir.join("thumbnails"), &data_dir.join("thumbnails"))?
    } else {
        0
    };
    if include_metadata {
        copy_dir_all(&legacy_dir.join("metadata"), &data_dir.join("metadata"))?;
    }

    let conn = db::init(&dst_db)?;
    let artworks: i64 = conn.query_row("SELECT COUNT(*) FROM artworks", [], |r| r.get(0))?;
    let images: i64 = conn.query_row("SELECT COUNT(*) FROM images", [], |r| r.get(0))?;
    drop(conn);

    Ok(ImportReport {
        artworks,
        images,
        thumbs_copied,
        backed_up_to,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    use std::time::SystemTime;

    fn temp_dir(tag: &str) -> PathBuf {
        let unique = format!(
            "{}-{}-{:?}",
            tag,
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        // 用 target/ 而不是系统 TEMP：本机受控环境里 TEMP 不可写。
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("test-tmp")
            .join(unique);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// 目录快照：相对路径 → (字节数, 修改时间)。用于证明导入没有碰过旧目录。
    fn snapshot(root: &Path) -> BTreeMap<String, (u64, SystemTime)> {
        let mut out = BTreeMap::new();
        fn walk(root: &Path, dir: &Path, out: &mut BTreeMap<String, (u64, SystemTime)>) {
            let Ok(entries) = std::fs::read_dir(dir) else {
                return;
            };
            for entry in entries.flatten() {
                let path = entry.path();
                if path.is_dir() {
                    walk(root, &path, out);
                } else if path.is_file() {
                    let meta = std::fs::metadata(&path).unwrap();
                    let rel = path
                        .strip_prefix(root)
                        .unwrap()
                        .to_string_lossy()
                        .to_string();
                    out.insert(rel, (meta.len(), meta.modified().unwrap()));
                }
            }
        }
        walk(root, root, &mut out);
        out
    }

    /// 造一个"旧项目"目录：真实 schema 的 archive.db + 若干缩略图与元数据。
    fn build_fake_legacy(root: &Path) -> (i64, i64) {
        std::fs::create_dir_all(root.join("thumbnails")).unwrap();
        std::fs::create_dir_all(root.join("metadata")).unwrap();

        let conn = db::init(&root.join("archive.db")).unwrap();
        conn.execute(
            "INSERT INTO authors (pixiv_user_id, name) VALUES (100, '作者甲')",
            [],
        )
        .unwrap();
        let author_id = conn.last_insert_rowid();
        for pid in [1001i64, 1002] {
            conn.execute(
                "INSERT INTO artworks (pixiv_id, title, author_id, author_name, page_count)
                 VALUES (?1, ?2, ?3, '作者甲', 1)",
                rusqlite::params![pid, format!("作品{pid}"), author_id],
            )
            .unwrap();
            let artwork_id = conn.last_insert_rowid();
            conn.execute(
                "INSERT INTO images (artwork_id, page, path, sha256) VALUES (?1, 0, ?2, ?3)",
                rusqlite::params![artwork_id, format!(r"D:\pics\{pid}_p0.jpg"), format!("sha{pid}")],
            )
            .unwrap();
        }
        // 关闭连接，让 WAL 落盘
        drop(conn);

        std::fs::write(root.join("thumbnails").join("1001.jpg"), b"thumb-a").unwrap();
        std::fs::write(root.join("thumbnails").join("1002.jpg"), b"thumb-b").unwrap();
        std::fs::write(root.join("metadata").join("1001.json"), b"{}").unwrap();

        (2, 2)
    }

    #[test]
    fn imports_legacy_db_with_matching_row_counts() {
        let legacy = temp_dir("legacy-src");
        let data = temp_dir("legacy-dst");
        let (expect_artworks, expect_images) = build_fake_legacy(&legacy);

        let report = import_legacy(&legacy, &data, true, true).unwrap();

        assert_eq!(report.artworks, expect_artworks);
        assert_eq!(report.images, expect_images);
        assert_eq!(report.thumbs_copied, 2);
        assert!(report.backed_up_to.is_none());

        // 新库真的能查出行
        let conn = db::init(&data.join(DB_FILE_NAME)).unwrap();
        let title: String = conn
            .query_row("SELECT title FROM artworks WHERE pixiv_id = 1001", [], |r| r.get(0))
            .unwrap();
        assert_eq!(title, "作品1001");

        // 派生数据也复制过来了
        assert!(data.join("thumbnails").join("1001.jpg").is_file());
        assert!(data.join("metadata").join("1001.json").is_file());
    }

    #[test]
    fn leaves_legacy_directory_untouched() {
        let legacy = temp_dir("legacy-readonly");
        let data = temp_dir("legacy-readonly-dst");
        build_fake_legacy(&legacy);

        let before = snapshot(&legacy);
        import_legacy(&legacy, &data, true, true).unwrap();
        let after = snapshot(&legacy);

        assert_eq!(
            before, after,
            "导入过程修改了旧目录：文件数量、大小或修改时间发生变化"
        );
    }

    #[test]
    fn backs_up_existing_library_before_overwrite() {
        let legacy = temp_dir("legacy-overwrite");
        let data = temp_dir("legacy-overwrite-dst");
        build_fake_legacy(&legacy);

        // 先放一个"现有库"进去
        {
            let conn = db::init(&data.join(DB_FILE_NAME)).unwrap();
            conn.execute("INSERT INTO artworks (pixiv_id, title) VALUES (9, '旧数据')", [])
                .unwrap();
        }

        let report = import_legacy(&legacy, &data, false, false).unwrap();

        let backup = report.backed_up_to.expect("已有库时应返回备份路径");
        assert!(Path::new(&backup).is_file(), "备份文件不存在：{backup}");

        // 备份里保留的是被覆盖前的数据
        let old = db::open(Path::new(&backup)).unwrap();
        let old_count: i64 = old
            .query_row("SELECT COUNT(*) FROM artworks WHERE pixiv_id = 9", [], |r| r.get(0))
            .unwrap();
        assert_eq!(old_count, 1);

        // 导入后是新库内容
        assert_eq!(report.artworks, 2);
    }

    #[test]
    fn reports_missing_legacy_db() {
        let legacy = temp_dir("legacy-missing");
        let data = temp_dir("legacy-missing-dst");
        let err = import_legacy(&legacy, &data, false, false).unwrap_err();
        assert_eq!(err.code, "NOT_FOUND");
    }
}