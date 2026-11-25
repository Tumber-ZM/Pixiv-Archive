use std::path::Path;

use rusqlite::types::Value;
use rusqlite::{Connection, OptionalExtension};

use crate::db::{self, query};
use crate::error::{AppError, AppResult};
use crate::models::{
    ArtworkCard, ArtworkDetail, BatchResult, DeletedResult, FavoriteRef, ImageItem, ListQuery,
    Page, Stats, Tag,
};
use crate::platform::paths;

const CARD_COLUMNS: &str = "
  a.id, a.pixiv_id, a.title, a.author_name, a.author_id,
  COALESCE(a.page_count, 1) AS page_count, a.create_date, a.width, a.height,
  COALESCE(a.pixiv_status, 'active') AS pixiv_status, COALESCE(a.ai_type, 1) AS ai_type,
  EXISTS(SELECT 1 FROM favorite_artworks fa WHERE fa.artwork_id = a.id) AS is_favorited,
  (SELECT i.path FROM images i WHERE i.artwork_id = a.id ORDER BY i.page LIMIT 1) AS cover_path,
  (SELECT i.id   FROM images i WHERE i.artwork_id = a.id ORDER BY i.page LIMIT 1) AS cover_image_id,
  (SELECT i.width  FROM images i WHERE i.artwork_id = a.id ORDER BY i.page LIMIT 1) AS cover_width,
  (SELECT i.height FROM images i WHERE i.artwork_id = a.id ORDER BY i.page LIMIT 1) AS cover_height,
  COALESCE(
    (SELECT i.path FROM images i WHERE i.artwork_id = a.id ORDER BY i.page LIMIT 1),
    a.local_path
  ) AS source_path,
  EXISTS(SELECT 1 FROM artwork_tags at JOIN tags t ON t.id = at.tag_id
         WHERE at.artwork_id = a.id AND t.name IN ('R-18','R-18G')) AS is_r18,
  EXISTS(SELECT 1 FROM images i WHERE i.artwork_id = a.id AND lower(i.path) LIKE '%.gif') AS is_gif
";

fn open_library() -> AppResult<Connection> {
    let path = paths::db_path()?;
    if !path.is_file() {
        return Err(AppError::not_found("数据库尚未初始化，请先导入旧数据或扫描目录"));
    }
    db::open(&path)
}

fn map_card(row: &rusqlite::Row<'_>) -> rusqlite::Result<ArtworkCard> {
    Ok(ArtworkCard {
        id: row.get(0)?,
        pixiv_id: row.get(1)?,
        title: row.get(2)?,
        author_name: row.get(3)?,
        author_id: row.get(4)?,
        page_count: row.get(5)?,
        create_date: row.get(6)?,
        width: row.get(7)?,
        height: row.get(8)?,
        pixiv_status: row.get(9)?,
        ai_type: row.get(10)?,
        is_favorited: row.get::<_, i64>(11)? != 0,
        cover_path: row.get(12)?,
        cover_image_id: row.get(13)?,
        cover_width: row.get(14)?,
        cover_height: row.get(15)?,
        source_path: row.get(16)?,
        is_r18: row.get::<_, i64>(17)? != 0,
        is_gif: row.get::<_, i64>(18)? != 0,
    })
}

/// 列表查询主体。接收连接而不是自己开库，方便单测用内存库直接断言。
pub fn list_page_with(conn: &Connection, query: &ListQuery) -> AppResult<Page<ArtworkCard>> {
    let (page, page_size) = query::page_of(query);
    let filter = query::build_filter(query);

    let count_sql = format!("SELECT COUNT(*) FROM artworks a WHERE {}", filter.where_sql);
    let total: i64 = conn.query_row(
        &count_sql,
        rusqlite::params_from_iter(filter.params.iter()),
        |row| row.get(0),
    )?;

    let sql = format!(
        "SELECT {CARD_COLUMNS} FROM artworks a WHERE {} {} LIMIT ? OFFSET ?",
        filter.where_sql,
        query::order_by(query)
    );

    let offset = (page as i64 - 1) * page_size as i64;
    let mut bound: Vec<Value> = filter.params.clone();
    bound.push(Value::Integer(page_size as i64));
    bound.push(Value::Integer(offset));

    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(rusqlite::params_from_iter(bound.iter()), map_card)?;
    let mut items = Vec::with_capacity(page_size as usize);
    for row in rows {
        items.push(row?);
    }

    let total_pages = if total == 0 {
        0
    } else {
        (total + page_size as i64 - 1) / page_size as i64
    } as u32;

    Ok(Page {
        items,
        total,
        page,
        page_size,
        total_pages,
    })
}

pub fn stats_with(conn: &Connection) -> AppResult<Stats> {
    let r18 = query::R18_EXISTS;
    let gif = query::GIF_EXISTS;
    // 本地文件（pixiv_id < 0）不属于 Pixiv 作品，统计里一律排除
    let sql = format!(
        "SELECT
           (SELECT COUNT(*) FROM artworks WHERE pixiv_id > 0 AND COALESCE(pixiv_status, 'active') <> 'deleted'),
           (SELECT COUNT(*) FROM images i JOIN artworks a ON a.id = i.artwork_id \
            WHERE a.pixiv_id > 0 AND COALESCE(a.pixiv_status, 'active') <> 'deleted'),
           (SELECT COUNT(*) FROM authors au WHERE EXISTS (
                SELECT 1 FROM artworks a WHERE a.author_id = au.id AND COALESCE(a.pixiv_status, 'active') <> 'deleted')),
           (SELECT COUNT(*) FROM tags t WHERE EXISTS (
                SELECT 1 FROM artwork_tags at
                JOIN artworks w ON w.id = at.artwork_id AND COALESCE(w.pixiv_status, 'active') <> 'deleted'
                WHERE at.tag_id = t.id)),
           (SELECT COUNT(*) FROM favorites),
           (SELECT COUNT(*) FROM artworks \
            WHERE COALESCE(pixiv_status,'active') = 'deleted' AND pixiv_id > 0),
           (SELECT COUNT(*) FROM artworks a WHERE {r18} AND a.pixiv_id > 0 AND COALESCE(a.pixiv_status, 'active') <> 'deleted'),
           (SELECT COUNT(*) FROM artworks \
            WHERE COALESCE(ai_type,1) = 2 AND pixiv_id > 0 AND COALESCE(pixiv_status, 'active') <> 'deleted'),
           (SELECT COUNT(*) FROM artworks a WHERE {gif} AND a.pixiv_id > 0 AND COALESCE(a.pixiv_status, 'active') <> 'deleted')"
    );

    Ok(conn.query_row(&sql, [], |row| {
        Ok(Stats {
            artworks: row.get(0)?,
            images: row.get(1)?,
            authors: row.get(2)?,
            tags: row.get(3)?,
            favorites: row.get(4)?,
            deleted: row.get(5)?,
            r18: row.get(6)?,
            ai: row.get(7)?,
            gif: row.get(8)?,
        })
    })?)
}

/// 详情：作品 + 标签 + 图片 + 所属收藏夹，查看器一次拿全。
pub fn get_artwork_with(conn: &Connection, artwork_id: i64) -> AppResult<ArtworkDetail> {
    let mut detail = {
        let mut stmt = conn.prepare(
            "SELECT a.id, a.pixiv_id, a.title, a.description, a.author_id, a.author_name,
                    COALESCE(a.page_count, 1), a.width, a.height, a.create_date,
                    COALESCE(a.pixiv_status, 'active'), a.first_seen, a.last_synced, a.sync_error,
                    a.local_path, COALESCE(a.ai_type, 1)
             FROM artworks a WHERE a.id = ?1",
        )?;
        let mut rows = stmt.query([artwork_id])?;
        let Some(row) = rows.next()? else {
            return Err(AppError::not_found(format!("作品 {artwork_id} 不存在")));
        };
        ArtworkDetail {
            id: row.get(0)?,
            pixiv_id: row.get(1)?,
            title: row.get(2)?,
            description: row.get(3)?,
            author_id: row.get(4)?,
            author_name: row.get(5)?,
            page_count: row.get(6)?,
            width: row.get(7)?,
            height: row.get(8)?,
            create_date: row.get(9)?,
            pixiv_status: row.get(10)?,
            first_seen: row.get(11)?,
            last_synced: row.get(12)?,
            sync_error: row.get(13)?,
            local_path: row.get(14)?,
            ai_type: row.get(15)?,
            tags: Vec::new(),
            images: Vec::new(),
            favorites: Vec::new(),
            is_r18: false,
            is_gif: false,
        }
    };

    {
        let mut stmt = conn.prepare(
            "SELECT t.id, t.name, t.translated_name FROM tags t
             JOIN artwork_tags at ON at.tag_id = t.id
             WHERE at.artwork_id = ?1 ORDER BY t.name",
        )?;
        let rows = stmt.query_map([artwork_id], |row| {
            Ok(Tag {
                id: row.get(0)?,
                name: row.get(1)?,
                translated_name: row.get(2)?,
            })
        })?;
        for row in rows {
            detail.tags.push(row?);
        }
    }

    {
        let mut stmt = conn.prepare(
            "SELECT i.id, i.page, i.path, i.width, i.height FROM images i
             WHERE i.artwork_id = ?1 ORDER BY i.page",
        )?;
        let rows = stmt.query_map([artwork_id], |row| {
            Ok(ImageItem {
                id: row.get(0)?,
                page: row.get(1)?,
                path: row.get(2)?,
                width: row.get(3)?,
                height: row.get(4)?,
            })
        })?;
        for row in rows {
            detail.images.push(row?);
        }
    }

    {
        let mut stmt = conn.prepare(
            "SELECT f.id, f.name FROM favorites f
             JOIN favorite_artworks fa ON fa.favorite_id = f.id
             WHERE fa.artwork_id = ?1 ORDER BY f.name",
        )?;
        let rows = stmt.query_map([artwork_id], |row| {
            Ok(FavoriteRef {
                id: row.get(0)?,
                name: row.get(1)?,
            })
        })?;
        for row in rows {
            detail.favorites.push(row?);
        }
    }

    detail.is_r18 = detail
        .tags
        .iter()
        .any(|tag| tag.name == "R-18" || tag.name == "R-18G");
    detail.is_gif = detail
        .images
        .iter()
        .any(|image| image.path.to_ascii_lowercase().ends_with(".gif"));

    Ok(detail)
}

pub fn delete_artwork_with(
    conn: &Connection,
    data_dir: &Path,
    artwork_id: i64,
    delete_files: bool,
) -> AppResult<DeletedResult> {
    let images: Vec<String> = {
        let mut stmt = conn.prepare("SELECT path FROM images WHERE artwork_id = ?1")?;
        let rows = stmt.query_map([artwork_id], |row| row.get::<_, String>(0))?;
        let mut collected = Vec::new();
        for row in rows {
            collected.push(row?);
        }
        collected
    };

    let pixiv_id: Option<i64> = conn
        .query_row(
            "SELECT pixiv_id FROM artworks WHERE id = ?1",
            [artwork_id],
            |row| row.get(0),
        )
        .optional()?;

    let Some(pixiv_id) = pixiv_id else {
        return Err(AppError::not_found(format!("作品 {artwork_id} 不存在")));
    };

    let tx = conn.unchecked_transaction()?;
    tx.execute("DELETE FROM artwork_tags WHERE artwork_id = ?1", [artwork_id])?;
    tx.execute("DELETE FROM images WHERE artwork_id = ?1", [artwork_id])?;
    tx.execute("DELETE FROM artworks WHERE id = ?1", [artwork_id])?;
    tx.commit()?;

    let mut files_failed = Vec::new();
    if delete_files {
        for path in images {
            // 源文件本来就不在了不算失败，与「删除」语义一致
            if let Err(error) = std::fs::remove_file(&path) {
                if error.kind() != std::io::ErrorKind::NotFound {
                    files_failed.push(format!("{path}: {error}"));
                }
            }
        }
    }

    // 缩略图与元数据是派生物，作品记录没了就永远不再被引用，无条件清掉
    paths::remove_side_files(data_dir, pixiv_id);

    Ok(DeletedResult {
        deleted: true,
        files_failed,
    })
}

pub fn batch_delete_with(
    conn: &Connection,
    data_dir: &Path,
    artwork_ids: &[i64],
    delete_files: bool,
) -> AppResult<BatchResult> {
    let mut ok = 0i64;
    let mut failed = 0i64;
    let mut errors = Vec::new();

    for artwork_id in artwork_ids {
        match delete_artwork_with(conn, data_dir, *artwork_id, delete_files) {
            Ok(result) => {
                ok += 1;
                errors.extend(result.files_failed);
            }
            Err(error) => {
                failed += 1;
                errors.push(format!("#{artwork_id}: {}", error.message));
            }
        }
    }

    Ok(BatchResult { ok, failed, errors })
}

#[tauri::command]
pub fn get_artwork(artwork_id: i64) -> AppResult<ArtworkDetail> {
    let conn = open_library()?;
    get_artwork_with(&conn, artwork_id)
}

#[tauri::command]
pub fn delete_artwork(artwork_id: i64, delete_files: bool) -> AppResult<DeletedResult> {
    let conn = open_library()?;
    let data_dir = paths::data_dir()?;
    delete_artwork_with(&conn, &data_dir, artwork_id, delete_files)
}

#[tauri::command]
pub fn batch_delete_artworks(
    artwork_ids: Vec<i64>,
    delete_files: bool,
) -> AppResult<BatchResult> {
    let conn = open_library()?;
    let data_dir = paths::data_dir()?;
    batch_delete_with(&conn, &data_dir, &artwork_ids, delete_files)
}

/// 卡片封面优先用 400px 缩略图。
///
/// `images.path` 存的是原图路径（旧库语义），两三千像素的 JPEG 一屏几十张同时解码
/// 会把滚动拖垮；缩略图在 T07 的生成流程之前就已随旧库导入进来了，这里直接利用。
/// 缩略图缺失时保留原图路径，不影响可用性。
fn apply_thumbnail_paths(page: &mut Page<ArtworkCard>) {
    let Ok(dir) = paths::thumbnails_dir() else {
        return;
    };
    for card in &mut page.items {
        let candidate = dir.join(format!("{}.jpg", card.pixiv_id));
        if candidate.is_file() {
            card.cover_path = Some(candidate.to_string_lossy().to_string());
        }
    }
}

#[tauri::command]
pub fn list_artworks(query: ListQuery) -> AppResult<Page<ArtworkCard>> {
    let conn = open_library()?;
    let mut page = list_page_with(&conn, &query)?;
    apply_thumbnail_paths(&mut page);
    Ok(page)
}

#[tauri::command]
pub fn search_artworks(query: ListQuery) -> AppResult<Page<ArtworkCard>> {
    let conn = open_library()?;
    let mut page = list_page_with(&conn, &query)?;
    apply_thumbnail_paths(&mut page);
    Ok(page)
}

#[tauri::command]
pub fn get_stats() -> AppResult<Stats> {
    let conn = open_library()?;
    stats_with(&conn)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 造一张内存库：6 个作品覆盖 R18 / AI / GIF 的各种组合。
    fn fixture() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(crate::db::schema::SCHEMA_SQL).unwrap();

        conn.execute("INSERT INTO authors (pixiv_user_id, name) VALUES (1, '作者甲')", []).unwrap();
        conn.execute("INSERT INTO tags (name) VALUES ('R-18')", []).unwrap();
        conn.execute("INSERT INTO tags (name) VALUES ('风景')", []).unwrap();
        conn.execute("INSERT INTO tags (name) VALUES ('R-18G')", []).unwrap();
        let r18_id: i64 = conn.query_row("SELECT id FROM tags WHERE name='R-18'", [], |r| r.get(0)).unwrap();
        let scape_id: i64 = conn.query_row("SELECT id FROM tags WHERE name='风景'", [], |r| r.get(0)).unwrap();
        let r18g_id: i64 = conn.query_row("SELECT id FROM tags WHERE name='R-18G'", [], |r| r.get(0)).unwrap();

        // (pixiv_id, title, ai_type, 有R18, 有GIF)
        let rows: [(i64, &str, i64, bool, bool); 6] = [
            (101, "普通作品", 1, false, false),
            (102, "R18 作品", 1, true, false),
            (103, "AI 作品", 2, false, false),
            (104, "R18 且 AI", 2, true, false),
            (105, "GIF 作品", 1, false, true),
            (106, "R18G 且 GIF", 1, true, true),
        ];

        for (pixiv_id, title, ai_type, is_r18, is_gif) in rows {
            conn.execute(
                "INSERT INTO artworks (pixiv_id, title, author_id, author_name, ai_type)
                 VALUES (?1, ?2, 1, '作者甲', ?3)",
                rusqlite::params![pixiv_id, title, ai_type],
            )
            .unwrap();
            let artwork_id = conn.last_insert_rowid();
            if is_r18 {
                let tag = if pixiv_id == 106 { r18g_id } else { r18_id };
                conn.execute(
                    "INSERT INTO artwork_tags (artwork_id, tag_id) VALUES (?1, ?2)",
                    rusqlite::params![artwork_id, tag],
                )
                .unwrap();
            }
            if pixiv_id == 101 {
                conn.execute(
                    "INSERT INTO artwork_tags (artwork_id, tag_id) VALUES (?1, ?2)",
                    rusqlite::params![artwork_id, scape_id],
                )
                .unwrap();
            }
            let path = if is_gif {
                format!(r"D:\pics\{pixiv_id}_p0.gif")
            } else {
                format!(r"D:\pics\{pixiv_id}_p0.jpg")
            };
            conn.execute(
                "INSERT INTO images (artwork_id, page, path) VALUES (?1, 0, ?2)",
                rusqlite::params![artwork_id, path],
            )
            .unwrap();
        }

        conn
    }

    fn count(conn: &Connection, query: ListQuery) -> i64 {
        list_page_with(conn, &query).unwrap().total
    }

    #[test]
    fn local_artworks_stay_out_of_gallery() {
        let conn = fixture();
        conn.execute(
            "INSERT INTO artworks (pixiv_id, title) VALUES (-9001, '本地文件')",
            [],
        )
        .unwrap();

        // 默认查询（画廊、作者、标签、收藏夹都走它）看不到本地文件
        assert_eq!(count(&conn, ListQuery::default()), 6);

        // 本地作品视图只返回负数 ID
        let local = list_page_with(
            &conn,
            &ListQuery {
                scope: Some("local".to_string()),
                ..Default::default()
            },
        )
        .unwrap();
        assert_eq!(local.total, 1);
        assert_eq!(local.items[0].pixiv_id, -9001);
    }

    #[test]
    fn tri_state_counts_are_exact() {
        let conn = fixture();
        let tri = |r18: &str, ai: &str, gif: &str| ListQuery {
            r18: Some(r18.to_string()),
            ai: Some(ai.to_string()),
            gif: Some(gif.to_string()),
            ..Default::default()
        };

        // 全库 6 条
        assert_eq!(count(&conn, tri("all", "all", "all")), 6);
        // R18：102 / 104 / 106 三条
        assert_eq!(count(&conn, tri("hide", "all", "all")), 3);
        assert_eq!(count(&conn, tri("only", "all", "all")), 3);
        // AI：103 / 104 两条
        assert_eq!(count(&conn, tri("all", "hide", "all")), 4);
        assert_eq!(count(&conn, tri("all", "only", "all")), 2);
        // GIF：105 / 106 两条
        assert_eq!(count(&conn, tri("all", "all", "hide")), 4);
        assert_eq!(count(&conn, tri("all", "all", "only")), 2);

        // 两两组合
        assert_eq!(count(&conn, tri("only", "only", "all")), 1); // 104
        assert_eq!(count(&conn, tri("only", "all", "only")), 1); // 106
        assert_eq!(count(&conn, tri("all", "only", "only")), 0);
        assert_eq!(count(&conn, tri("hide", "hide", "hide")), 1); // 101
    }

    #[test]
    fn search_hits_title_author_and_tag() {
        let conn = fixture();
        let by_title = ListQuery { q: Some("AI".into()), ..Default::default() };
        assert_eq!(count(&conn, by_title), 2); // "AI 作品" + "R18 且 AI"

        let by_author = ListQuery { q: Some("作者甲".into()), ..Default::default() };
        assert_eq!(count(&conn, by_author), 6);

        let by_tag = ListQuery { q: Some("R-18".into()), ..Default::default() };
        assert_eq!(count(&conn, by_tag), 3); // 标签命中 R-18 与 R-18G

        let by_pixiv_id = ListQuery { q: Some("105".into()), ..Default::default() };
        assert_eq!(count(&conn, by_pixiv_id), 1);
    }

    #[test]
    fn pagination_slices_and_counts_total() {
        let conn = fixture();
        let page1 = list_page_with(
            &conn,
            &ListQuery { page: Some(1), page_size: Some(4), ..Default::default() },
        )
        .unwrap();
        assert_eq!(page1.items.len(), 4);
        assert_eq!(page1.total, 6);
        assert_eq!(page1.total_pages, 2);

        let page2 = list_page_with(
            &conn,
            &ListQuery { page: Some(2), page_size: Some(4), ..Default::default() },
        )
        .unwrap();
        assert_eq!(page2.items.len(), 2);

        let page3 = list_page_with(
            &conn,
            &ListQuery { page: Some(3), page_size: Some(4), ..Default::default() },
        )
        .unwrap();
        assert!(page3.items.is_empty());
    }

    #[test]
    fn random_order_keeps_same_sequence_across_pages() {
        let conn = fixture();
        let q = |page: u32| ListQuery {
            sort: Some("random".into()),
            seed: Some(7),
            page: Some(page),
            page_size: Some(3),
            ..Default::default()
        };

        let first = list_page_with(&conn, &q(1)).unwrap();
        let second = list_page_with(&conn, &q(2)).unwrap();

        let mut seen: Vec<i64> = first.items.iter().map(|c| c.id).collect();
        seen.extend(second.items.iter().map(|c| c.id));

        assert_eq!(seen.len(), 6);
        let mut unique = seen.clone();
        unique.sort_unstable();
        unique.dedup();
        assert_eq!(unique.len(), 6, "两页之间不应重复或漏项");
    }

    #[test]
    fn cards_carry_cover_and_badges() {
        let conn = fixture();
        let page = list_page_with(
            &conn,
            &ListQuery { q: Some("106".into()), ..Default::default() },
        )
        .unwrap();
        let card = &page.items[0];
        assert_eq!(card.pixiv_id, 106);
        assert!(card.is_r18, "R-18G 也算 R18");
        assert!(card.is_gif);
        assert_eq!(card.page_count, 1);
        assert_eq!(card.pixiv_status, "active");
        assert!(!card.is_favorited, "未加入收藏夹时应为 false");
        assert!(card.cover_path.as_deref().unwrap().ends_with(".gif"));
    }

    #[test]
    fn stats_are_consistent() {
        let conn = fixture();

        // 本地文件连同它的图片都不能进统计
        conn.execute(
            "INSERT INTO artworks (pixiv_id, title) VALUES (-9002, '本地文件')",
            [],
        )
        .unwrap();
        let local_id = conn.last_insert_rowid();
        conn.execute(
            "INSERT INTO images (artwork_id, page, path) VALUES (?1, 0, ?2)",
            rusqlite::params![local_id, r"D:\pics\local.png"],
        )
        .unwrap();

        let stats = stats_with(&conn).unwrap();
        assert_eq!(stats.artworks, 6);
        assert_eq!(stats.images, 6);
        assert_eq!(stats.authors, 1);
        assert_eq!(stats.tags, 3);
        assert_eq!(stats.r18, 3);
        assert_eq!(stats.ai, 2);
        assert_eq!(stats.gif, 2);
        assert_eq!(stats.deleted, 0);
    }

    /// 用真实旧库的**临时副本**压首屏，验证 `01` 的「1900 条数据首屏 < 300ms」。
    /// 依赖本机样本，默认不跑：`cargo test -- --ignored --nocapture`
    #[test]
    #[ignore = "依赖本机旧库样本"]
    fn legacy_corpus_first_page_is_fast() {
        let source = std::path::Path::new(r"D:\Little Tool\Pixiv-Archive\archive.db");
        if !source.is_file() {
            eprintln!("跳过：本机没有旧库样本");
            return;
        }

        let dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("test-tmp")
            .join(format!(
                "perf-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
        std::fs::create_dir_all(&dir).unwrap();
        let db_path = dir.join("library.db");
        std::fs::copy(source, &db_path).unwrap();

        let conn = crate::db::init(&db_path).unwrap();

        let mut worst = std::time::Duration::ZERO;
        let mut total_artworks = 0i64;

        for label in ["all", "r18-only", "search"] {
            let query = match label {
                "r18-only" => ListQuery {
                    r18: Some("only".into()),
                    page_size: Some(60),
                    ..Default::default()
                },
                "search" => ListQuery {
                    q: Some("R-18".into()),
                    page_size: Some(60),
                    ..Default::default()
                },
                _ => ListQuery {
                    page_size: Some(60),
                    ..Default::default()
                },
            };

            let started = std::time::Instant::now();
            let page = list_page_with(&conn, &query).unwrap();
            let elapsed = started.elapsed();
            worst = worst.max(elapsed);
            total_artworks = total_artworks.max(page.total);
            eprintln!("[{label}] total={} items={} 用时={elapsed:?}", page.total, page.items.len());
        }

        eprintln!("worst={worst:?} artworks={total_artworks}");
        assert!(
            total_artworks > 500,
            "样本太小（{total_artworks}），性能结论无意义"
        );
        assert!(
            worst.as_millis() < 300,
            "首屏查询超过 300ms：{worst:?}"
        );
    }

    /// 造一个带真实文件的作品，用于验证删除语义。
    fn delete_fixture(tag: &str) -> (Connection, std::path::PathBuf, i64) {
        let dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("test-tmp")
            .join(format!(
                "{tag}-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
        std::fs::create_dir_all(dir.join("thumbnails")).unwrap();
        std::fs::create_dir_all(dir.join("metadata")).unwrap();

        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(crate::db::schema::SCHEMA_SQL).unwrap();
        conn.execute(
            "INSERT INTO artworks (pixiv_id, title) VALUES (500, '待删除')",
            [],
        )
        .unwrap();
        let artwork_id = conn.last_insert_rowid();

        let source = dir.join("500_p0.jpg");
        std::fs::write(&source, b"image-bytes").unwrap();
        conn.execute(
            "INSERT INTO images (artwork_id, page, path) VALUES (?1, 0, ?2)",
            rusqlite::params![artwork_id, source.to_string_lossy().to_string()],
        )
        .unwrap();

        std::fs::write(dir.join("thumbnails").join("500.jpg"), b"thumb").unwrap();
        std::fs::write(dir.join("metadata").join("500.json"), b"{}").unwrap();

        (conn, dir, artwork_id)
    }

    #[test]
    fn delete_with_files_removes_source_and_sidecars() {
        let (conn, dir, artwork_id) = delete_fixture("del-files");

        let result = delete_artwork_with(&conn, &dir, artwork_id, true).unwrap();
        assert!(result.deleted);
        assert!(result.files_failed.is_empty(), "{:?}", result.files_failed);

        assert!(!dir.join("500_p0.jpg").exists(), "源文件应被删除");
        assert!(!dir.join("thumbnails").join("500.jpg").exists(), "缩略图应被清理");
        assert!(!dir.join("metadata").join("500.json").exists(), "元数据应被清理");

        let left: i64 = conn
            .query_row("SELECT COUNT(*) FROM artworks", [], |r| r.get(0))
            .unwrap();
        assert_eq!(left, 0);
        let images: i64 = conn
            .query_row("SELECT COUNT(*) FROM images", [], |r| r.get(0))
            .unwrap();
        assert_eq!(images, 0);
    }

    #[test]
    fn delete_without_files_keeps_source_but_drops_sidecars() {
        let (conn, dir, artwork_id) = delete_fixture("del-keep");

        delete_artwork_with(&conn, &dir, artwork_id, false).unwrap();

        assert!(dir.join("500_p0.jpg").exists(), "未勾选时不应删源文件");
        // 派生物永远清：作品记录没了，它们不再被引用
        assert!(!dir.join("thumbnails").join("500.jpg").exists());
        assert!(!dir.join("metadata").join("500.json").exists());
    }

    #[test]
    fn delete_missing_artwork_is_not_found() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(crate::db::schema::SCHEMA_SQL).unwrap();
        let err = delete_artwork_with(&conn, std::path::Path::new("."), 424242, false).unwrap_err();
        assert_eq!(err.code, "NOT_FOUND");
    }
}