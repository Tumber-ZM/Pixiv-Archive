/// 8 张表 + 12 个索引，逐字照抄旧项目 `app/database.py`。
///
/// 与旧项目唯一的差别：`sync_error` 直接写进建表语句。
/// 旧库是逐步 `ALTER TABLE` 演化来的，这里建表就带全，`columns.rs` 仍会为旧库补列。
pub const SCHEMA_SQL: &str = r#"
CREATE TABLE IF NOT EXISTS authors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pixiv_user_id INTEGER UNIQUE NOT NULL,
    name TEXT NOT NULL,
    profile_image TEXT
);

CREATE TABLE IF NOT EXISTS artworks (
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
    first_seen TEXT DEFAULT (datetime('now')),
    last_synced TEXT,
    local_path TEXT,
    -- Pixiv illust_ai_type：1 = 非 AI，2 = AI 生成。存量与未同步行按 1 处理
    ai_type INTEGER DEFAULT 1,
    sync_error TEXT,
    -- 同步失败分类：auth / network / not_found / rate_limited / internal
    sync_error_kind TEXT,
    -- 连续失败次数与下次允许重试的时间（避免坏作品反复占用配额）
    sync_attempts INTEGER DEFAULT 0,
    next_retry_at TEXT,
    FOREIGN KEY (author_id) REFERENCES authors(id)
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    translated_name TEXT
);

CREATE TABLE IF NOT EXISTS artwork_tags (
    artwork_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (artwork_id, tag_id),
    FOREIGN KEY (artwork_id) REFERENCES artworks(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artwork_id INTEGER NOT NULL,
    page INTEGER NOT NULL,
    path TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    sha256 TEXT,
    phash TEXT,
    FOREIGN KEY (artwork_id) REFERENCES artworks(id) ON DELETE CASCADE
);

-- 持久化下载队列（作品级）：进程重启后能接着下，失败项按退避时间重试。
-- 页级进度不落库：已下好的页由磁盘上的文件和 `.part` 续传判定。
CREATE TABLE IF NOT EXISTS download_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pixiv_id INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    last_error TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    create_date TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS favorite_artworks (
    favorite_id INTEGER NOT NULL,
    artwork_id INTEGER NOT NULL,
    added_date TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (favorite_id, artwork_id),
    FOREIGN KEY (favorite_id) REFERENCES favorites(id) ON DELETE CASCADE,
    FOREIGN KEY (artwork_id) REFERENCES artworks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bookmark_subs (
    pixiv_user_id INTEGER PRIMARY KEY,
    name TEXT,
    last_pid INTEGER,
    auto_download INTEGER DEFAULT 1,
    restrict TEXT DEFAULT 'public',
    last_checked TEXT,
    last_result TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_artworks_pixiv_id ON artworks(pixiv_id);
CREATE INDEX IF NOT EXISTS idx_artworks_author_id ON artworks(author_id);
CREATE INDEX IF NOT EXISTS idx_artworks_title ON artworks(title);
CREATE INDEX IF NOT EXISTS idx_artworks_create_date ON artworks(create_date);
CREATE INDEX IF NOT EXISTS idx_artworks_first_seen ON artworks(first_seen);
CREATE INDEX IF NOT EXISTS idx_images_artwork_id ON images(artwork_id);
CREATE INDEX IF NOT EXISTS idx_images_sha256 ON images(sha256);
/* 扫描判重按 path 查（命中就不读文件），没有索引会退化成全表扫 */
CREATE INDEX IF NOT EXISTS idx_images_path ON images(path);
CREATE INDEX IF NOT EXISTS idx_artwork_tags_artwork_id ON artwork_tags(artwork_id);
CREATE INDEX IF NOT EXISTS idx_artwork_tags_tag_id ON artwork_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);
CREATE INDEX IF NOT EXISTS idx_download_queue_status ON download_queue(status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_favorite_artworks_favorite ON favorite_artworks(favorite_id);
CREATE INDEX IF NOT EXISTS idx_favorite_artworks_artwork ON favorite_artworks(artwork_id);
"#;

/// 旧库可能缺的列与过时的表，逐个补齐。
/// 这里覆盖 `app/database.py` 的四个 `_migrate_*` 函数，缺一个就会在旧库上报错。
pub fn table_columns(conn: &rusqlite::Connection, table: &str) -> crate::error::AppResult<Vec<String>> {
    let mut stmt = conn.prepare(&format!("PRAGMA table_info({table})"))?;
    let rows = stmt.query_map([], |row| row.get::<_, String>(1))?;
    let mut columns = Vec::new();
    for row in rows {
        columns.push(row?);
    }
    Ok(columns)
}

pub fn ensure_columns(conn: &rusqlite::Connection) -> crate::error::AppResult<()> {
    let columns = table_columns(conn, "artworks")?;
    if !columns.iter().any(|c| c == "sync_error") {
        conn.execute("ALTER TABLE artworks ADD COLUMN sync_error TEXT", [])?;
    }
    if !columns.iter().any(|c| c == "ai_type") {
        conn.execute("ALTER TABLE artworks ADD COLUMN ai_type INTEGER DEFAULT 1", [])?;
    }
    if !columns.iter().any(|c| c == "sync_error_kind") {
        conn.execute("ALTER TABLE artworks ADD COLUMN sync_error_kind TEXT", [])?;
    }
    if !columns.iter().any(|c| c == "sync_attempts") {
        conn.execute(
            "ALTER TABLE artworks ADD COLUMN sync_attempts INTEGER DEFAULT 0",
            [],
        )?;
    }
    if !columns.iter().any(|c| c == "next_retry_at") {
        conn.execute("ALTER TABLE artworks ADD COLUMN next_retry_at TEXT", [])?;
    }
    Ok(())
}

pub fn ensure_bookmark_subs_cols(conn: &rusqlite::Connection) -> crate::error::AppResult<()> {
    let columns = table_columns(conn, "bookmark_subs")?;
    let has_bid = columns.iter().any(|c| c == "last_bid");
    let has_pid = columns.iter().any(|c| c == "last_pid");
    if has_bid && !has_pid {
        conn.execute(
            "ALTER TABLE bookmark_subs RENAME COLUMN last_bid TO last_pid",
            [],
        )?;
    }
    // 收藏类型：public = 对方的公开收藏，private = 当前登录账号自己的收藏
    // （检查时私密与公开两份列表都会翻）。老库补列后统一按 public 处理。
    if !columns.iter().any(|c| c == "restrict") {
        conn.execute(
            "ALTER TABLE bookmark_subs ADD COLUMN restrict TEXT DEFAULT 'public'",
            [],
        )?;
    }
    Ok(())
}

/// v1.2.1 起画师订阅被「收藏订阅」取代，旧表连同数据一并丢弃。
///
/// 同时丢掉旧的 `uidx_images_sha256` 全局唯一索引：同一张图可能被不同作品
/// 引用（跨作品同图），按内容唯一会把后一个作品变成「有作品、没有图片」。
pub fn drop_legacy_tables(conn: &rusqlite::Connection) -> crate::error::AppResult<()> {
    conn.execute("DROP TABLE IF EXISTS subscriptions", [])?;
    conn.execute("DROP INDEX IF EXISTS uidx_images_sha256", [])?;
    Ok(())
}

/// 旧库是否还带着 `uidx_images_sha256` 唯一索引（启动时据此提示用户跑校验）。
pub fn has_legacy_sha256_index(conn: &rusqlite::Connection) -> crate::error::AppResult<bool> {
    let exists: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='index' AND name='uidx_images_sha256')",
        [],
        |row| row.get(0),
    )?;
    Ok(exists)
}

/// 旧库可能已有重复行；先清理再建唯一索引，否则建索引会直接失败。
///
/// 只按 (artwork_id, page) 去重；不再按 sha256 全局去重，避免跨作品同图
/// 导致作品缺页或空壳。
pub fn ensure_unique_indexes(conn: &rusqlite::Connection) -> crate::error::AppResult<()> {
    // 同一作品同一页保留最早的一行
    conn.execute(
        "DELETE FROM images WHERE id NOT IN (
             SELECT MIN(id) FROM images GROUP BY artwork_id, page
         )",
        [],
    )?;
    conn.execute_batch(
        "DROP INDEX IF EXISTS uidx_images_sha256;
         CREATE UNIQUE INDEX IF NOT EXISTS uidx_images_artwork_page ON images(artwork_id, page);",
    )?;
    Ok(())
}

/// 是否需要跑一次「有副作用」的迁移（用于迁移前备份数据库）。
pub fn needs_migration(conn: &rusqlite::Connection) -> crate::error::AppResult<bool> {
    let columns = table_columns(conn, "artworks")?;
    if !columns.iter().any(|c| c == "sync_error_kind") {
        return Ok(true);
    }
    let has_queue: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='download_queue')",
        [],
        |row| row.get(0),
    )?;
    if !has_queue {
        return Ok(true);
    }
    let has_unique: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='index' AND name='uidx_images_artwork_page')",
        [],
        |row| row.get(0),
    )?;
    Ok(!has_unique)
}