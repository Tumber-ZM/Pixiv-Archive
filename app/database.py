import sqlite3
import os
from contextlib import contextmanager
from dotenv import load_dotenv
from app import paths

load_dotenv(paths.ENV_FILE)

DB_PATH = os.path.join(paths.DATA_DIR, "archive.db")


def get_connection():
    # timeout=30：任务（同步/扫描）持有写事务期间，UI 发起的并发写等待锁而不是 5 秒即报
    # "database is locked"
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.executescript("""
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
            last_checked TEXT,
            last_result TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_artworks_pixiv_id ON artworks(pixiv_id);
        CREATE INDEX IF NOT EXISTS idx_artworks_author_id ON artworks(author_id);
        CREATE INDEX IF NOT EXISTS idx_artworks_title ON artworks(title);
        CREATE INDEX IF NOT EXISTS idx_images_artwork_id ON images(artwork_id);
        CREATE INDEX IF NOT EXISTS idx_images_sha256 ON images(sha256);
        CREATE INDEX IF NOT EXISTS idx_artwork_tags_artwork_id ON artwork_tags(artwork_id);
        CREATE INDEX IF NOT EXISTS idx_artwork_tags_tag_id ON artwork_tags(tag_id);
        CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);
        CREATE INDEX IF NOT EXISTS idx_favorite_artworks_favorite ON favorite_artworks(favorite_id);
        CREATE INDEX IF NOT EXISTS idx_favorite_artworks_artwork ON favorite_artworks(artwork_id);
    """)

    _migrate_sync_error(conn)
    _migrate_drop_artist_subs(conn)
    _migrate_bookmark_subs_col(conn)

    conn.commit()
    conn.close()


def _migrate_bookmark_subs_col(conn):
    """bookmark_subs 游标列语义修正：last_bid → last_pid（收藏项不含 bookmark_data.id，
    改用最大作品 PID 作增量游标）。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(bookmark_subs)").fetchall()}
    if "last_bid" in cols and "last_pid" not in cols:
        conn.execute("ALTER TABLE bookmark_subs RENAME COLUMN last_bid TO last_pid")


def _migrate_drop_artist_subs(conn):
    """v1.2.1：画师订阅功能已被「收藏订阅」(bookmark_subs) 取代，旧表数据丢弃。"""
    conn.execute("DROP TABLE IF EXISTS subscriptions")


def _migrate_sync_error(conn):
    """为旧库补上 sync_error 列（记录最近一次同步失败原因，用于下次重试与提示）。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(artworks)").fetchall()}
    if "sync_error" not in cols:
        conn.execute("ALTER TABLE artworks ADD COLUMN sync_error TEXT")