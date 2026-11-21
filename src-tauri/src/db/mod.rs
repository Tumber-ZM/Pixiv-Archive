pub mod query;
pub mod schema;

use std::path::{Path, PathBuf};

use rusqlite::Connection;

use crate::error::AppResult;

/// 打开连接。参数必须与旧项目一致，否则后台任务持写事务时 UI 的并发写会报
/// `database is locked`（旧版踩过，见 `03-数据模型与旧库兼容.md`）。
pub fn open(path: &Path) -> AppResult<Connection> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let conn = Connection::open(path)?;
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
         PRAGMA foreign_keys=ON;
         PRAGMA busy_timeout=30000;",
    )?;
    Ok(conn)
}

/// 打开并保证 schema 与迁移到位。新库与导入的旧库副本都走这里。
pub fn init(path: &Path) -> AppResult<Connection> {
    let conn = open(path)?;
    // 有副作用的迁移（加列 / 去重 / 建唯一索引）之前先留一份备份，
    // 出问题时可以直接把 library.db 换回去。
    if schema::needs_migration(&conn).unwrap_or(false) {
        let _ = backup(path, &conn);
    }
    conn.execute_batch(schema::SCHEMA_SQL)?;
    schema::ensure_columns(&conn)?;
    schema::ensure_bookmark_subs_cols(&conn)?;
    schema::drop_legacy_tables(&conn)?;
    schema::ensure_unique_indexes(&conn)?;
    Ok(conn)
}

/// 迁移前备份：先 checkpoint 把 WAL 合回主库，再整文件复制。
fn backup(path: &Path, conn: &Connection) -> Option<PathBuf> {
    if !path.is_file() {
        return None;
    }
    let _ = conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);");
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0);
    let target = path.with_extension(format!("db.bak-{stamp}"));
    std::fs::copy(path, &target).ok()?;
    Some(target)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    /// 每个测试独占一个临时目录，避免相互踩数据。
    /// 用 `target/` 而不是系统 TEMP：本机受控环境里 TEMP 不可写。
    pub fn temp_dir(tag: &str) -> PathBuf {
        let unique = format!(
            "{}-{}-{:?}",
            tag,
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("test-tmp")
            .join(unique);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn creates_all_tables_with_required_columns() {
        let dir = temp_dir("schema");
        let db = dir.join("library.db");
        let conn = init(&db).unwrap();

        let tables: Vec<String> = conn
            .prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
            .unwrap()
            .query_map([], |r| r.get::<_, String>(0))
            .unwrap()
            .map(|r| r.unwrap())
            .collect();

        for expected in [
            "artwork_tags",
            "artworks",
            "authors",
            "bookmark_subs",
            "favorite_artworks",
            "favorites",
            "images",
            "tags",
        ] {
            assert!(tables.iter().any(|t| t == expected), "缺少表 {expected}");
        }

        let columns = schema::table_columns(&conn, "artworks").unwrap();
        assert!(columns.iter().any(|c| c == "sync_error"), "artworks 缺 sync_error");
        assert!(columns.iter().any(|c| c == "ai_type"), "artworks 缺 ai_type");
    }

    #[test]
    fn backfills_columns_on_legacy_schema() {
        let dir = temp_dir("legacy-cols");
        let db = dir.join("library.db");
        {
            // 模拟一个早期版本的库：artworks 有全部业务列但没有 sync_error / ai_type，
            // bookmark_subs 用 last_bid，且残留 v1.2.1 之前的 subscriptions 表。
            let legacy_schema = r#"
CREATE TABLE artworks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pixiv_id INTEGER UNIQUE NOT NULL,
    title TEXT,
    description TEXT,
    author_id INTEGER,
    author_name TEXT,
    create_date TEXT,
    page_count INTEGER DEFAULT 1,
    width INTEGER,
    height INTEGER,
    pixiv_status TEXT DEFAULT 'active',
    first_seen TEXT,
    last_synced TEXT,
    local_path TEXT
);
CREATE TABLE bookmark_subs (
    pixiv_user_id INTEGER PRIMARY KEY,
    name TEXT,
    last_bid INTEGER,
    auto_download INTEGER DEFAULT 1,
    last_checked TEXT,
    last_result TEXT,
    created_at TEXT
);
CREATE TABLE subscriptions (id INTEGER PRIMARY KEY);
"#;
            let conn = Connection::open(&db).unwrap();
            conn.execute_batch(legacy_schema).unwrap();
        }

        let conn = init(&db).unwrap();

        let columns = schema::table_columns(&conn, "artworks").unwrap();
        assert!(columns.iter().any(|c| c == "sync_error"));
        assert!(columns.iter().any(|c| c == "ai_type"));

        let sub_cols = schema::table_columns(&conn, "bookmark_subs").unwrap();
        assert!(sub_cols.iter().any(|c| c == "last_pid"));
        assert!(!sub_cols.iter().any(|c| c == "last_bid"));

        let gone: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='subscriptions'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(gone, 0, "subscriptions 旧表应被丢弃");
    }

    #[test]
    fn init_is_idempotent() {
        let dir = temp_dir("idempotent");
        let db = dir.join("library.db");
        {
            let conn = init(&db).unwrap();
            conn.execute(
                "INSERT INTO artworks (pixiv_id, title) VALUES (1, 'x')",
                [],
            )
            .unwrap();
        }
        let conn = init(&db).unwrap();
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM artworks", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1, "重复 init 不应丢数据");
    }
}