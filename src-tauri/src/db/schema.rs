/// 11 张表 + 12 个索引，逐字照抄旧项目 `app/database.py`。
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
    -- 作品来源：local = 扫描/收藏订阅等既有收藏，author_sub = 作者订阅下载
    origin TEXT DEFAULT 'local',
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

-- 订阅：kind = bookmark（对方公开收藏）/ author（该作者的作品）。
-- 同一用户两种类型可以并存，所以主键是 (kind, target_pixiv_id)。
-- baseline_pid 是作者订阅的起点：NULL = 还没建立，0 = 补全全部历史。
CREATE TABLE IF NOT EXISTS subs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    target_pixiv_id INTEGER NOT NULL,
    name TEXT,
    baseline_pid INTEGER,
    auto_download INTEGER DEFAULT 1,
    last_checked TEXT,
    last_result TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (kind, target_pixiv_id)
);

-- Pixiv 收藏的本地索引：只保存作品 ID，用于作者列表「仅看收藏」筛选。
-- 数据来自用户自己账号的公开/私密收藏，不上传任何东西。
CREATE TABLE IF NOT EXISTS pixiv_favorites (
    pixiv_id INTEGER PRIMARY KEY,
    favorited_at TEXT DEFAULT (datetime('now'))
);

-- 杂项元数据（如上次同步 Pixiv 收藏的时间）。
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT
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
pub fn table_exists(conn: &rusqlite::Connection, table: &str) -> crate::error::AppResult<bool> {
    Ok(conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?1)",
        [table],
        |row| row.get(0),
    )?)
}

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
    // 存量作品都算「收藏」；作者订阅之后下载的才写 author_sub
    if !columns.iter().any(|c| c == "origin") {
        conn.execute(
            "ALTER TABLE artworks ADD COLUMN origin TEXT DEFAULT 'local'",
            [],
        )?;
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
    Ok(())
}

/// v2.1：收藏订阅泛化成 `subs`。老库的 `bookmark_subs` 一次性搬进新表。
///
/// 只有 `db::init` 发现库里还没有 `subs` 表时才调用，所以删掉的订阅不会
/// 被旧表重新搬回来。旧表保留不删：万一退回旧版 exe，收藏订阅仍然能读。
pub fn migrate_bookmark_subs(conn: &rusqlite::Connection) -> crate::error::AppResult<()> {
    if !table_exists(conn, "bookmark_subs")? {
        return Ok(());
    }
    conn.execute(
        "INSERT OR IGNORE INTO subs
             (kind, target_pixiv_id, name, auto_download, last_checked, last_result, created_at)
         SELECT 'bookmark', pixiv_user_id, name, COALESCE(auto_download, 1),
                last_checked, last_result, COALESCE(created_at, datetime('now'))
           FROM bookmark_subs",
        [],
    )?;
    Ok(())
}

/// v1.2.1 起画师订阅被「收藏订阅」取代，旧表连同数据一并丢弃。
pub fn drop_legacy_tables(conn: &rusqlite::Connection) -> crate::error::AppResult<()> {
    conn.execute("DROP TABLE IF EXISTS subscriptions", [])?;
    Ok(())
}

/// 旧库可能已有重复行；先清理再建唯一索引，否则建索引会直接失败。
pub fn ensure_unique_indexes(conn: &rusqlite::Connection) -> crate::error::AppResult<()> {
    // 同一作品同一页保留最早的一行
    conn.execute(
        "DELETE FROM images WHERE id NOT IN (
             SELECT MIN(id) FROM images GROUP BY artwork_id, page
         )",
        [],
    )?;
    // 同一内容（sha256 相同）保留最早的一行；sha256 为 NULL 的行不受影响
    conn.execute(
        "DELETE FROM images WHERE sha256 IS NOT NULL AND id NOT IN (
             SELECT MIN(id) FROM images WHERE sha256 IS NOT NULL GROUP BY sha256
         )",
        [],
    )?;
    conn.execute_batch(
        "CREATE UNIQUE INDEX IF NOT EXISTS uidx_images_artwork_page ON images(artwork_id, page);
         CREATE UNIQUE INDEX IF NOT EXISTS uidx_images_sha256 ON images(sha256);",
    )?;
    Ok(())
}

/// 是否需要跑一次「有副作用」的迁移（用于迁移前备份数据库）。
pub fn needs_migration(conn: &rusqlite::Connection) -> crate::error::AppResult<bool> {
    // v2.1 的 subs 表：老库还没有它，要先把 bookmark_subs 搬进去，先备份
    if !table_exists(conn, "subs")? {
        return Ok(true);
    }
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